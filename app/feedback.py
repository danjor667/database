"""MongoDB store for long-form customer feedback.

## The split, and why the ratings stay in Postgres

Numeric ratings live in `reviews` because `v_restaurant_ratings` aggregates over
them and the per-city `RANK()` needs them joined to `cities`. Prose lives here
because it has no relational shape, varies wildly in length, and is never the
subject of a join or an aggregate. `reviews.body_ref` holds the stringified
ObjectId of the document — the only link between the two stores.

## Why the document denormalizes what Postgres normalizes

`docs/er-diagram.md` argues at length against storing `restaurant_id` on
`reviews`, and this module then stores it on every feedback document. That is not
a contradiction, it is the same reasoning applied to a store with different
mechanics:

* *Within* Postgres a derived column is a liability, because `reservation_id`
  already determines the restaurant and no constraint could stop the two
  disagreeing. Removing the column removes the bug class.
* *Across* stores there is no shared constraint mechanism to lose — MongoDB
  cannot enforce a foreign key into Postgres under any schema. The copy buys the
  thing the split exists for: "show me the prose for restaurant X" is answered
  without a round trip to Postgres.

The copy is a read optimization with a single documented owner: **Postgres wins
every disagreement**, and `reconcile()` is what finds them.

## Write ordering

A review is created across two stores that share no transaction. The order is
forced by what each failure leaves behind:

1. **Postgres**: upsert the `reviews` row (ratings, title), `body_ref` still NULL.
   The row must exist first — the `reviews_validate` trigger checks the visit is
   completed, and there is no point writing prose for a review the database will
   refuse.
2. **MongoDB**: upsert the document, keyed on `reservation_id`.
3. **Postgres**: set `body_ref` to the document's `_id`.

**The pointer is written last because the pointer is the commitment.** Crashing
between 1 and 2 leaves a review with no prose, which is a legal state (`body_ref`
is nullable — a diner may leave ratings only). Crashing between 2 and 3 leaves an
unlinked document, which `reconcile()` detects and re-links, and which re-running
`add_review` heals on its own because both upserts are idempotent.

The reverse order — pointer first — would produce a *dangling* reference, where a
reader following `body_ref` finds nothing and cannot distinguish "not written
yet" from "lost". That is the one outcome worth designing around.

## Failure policy

Unlike `app/cache.py`, this module does **not** degrade silently. Redis holds a
rebuildable copy of something Postgres already knows, so an outage there is
correctly absorbed as a cache miss. MongoDB holds the only copy of the prose, so
an outage is a real failure and `pymongo` errors propagate to the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .db import Database
from .errors import FeedbackNotFound, ReviewNotFound
from .models import Feedback

log = logging.getLogger(__name__)

COLLECTION = "reviews"

# Upserts the ratings row, then resolves the identifiers the document needs.
# ON CONFLICT makes it idempotent, which is what lets a half-finished
# cross-store write be healed by simply running it again.
_UPSERT_REVIEW_SQL = """
WITH ins AS (
    INSERT INTO reviews (reservation_id, rating, food_rating, service_rating,
                         ambience_rating, title)
    VALUES (%(reservation_id)s, %(rating)s, %(food_rating)s, %(service_rating)s,
            %(ambience_rating)s, %(title)s)
    ON CONFLICT (reservation_id) DO UPDATE
        SET rating          = EXCLUDED.rating,
            food_rating     = EXCLUDED.food_rating,
            service_rating  = EXCLUDED.service_rating,
            ambience_rating = EXCLUDED.ambience_rating,
            title           = EXCLUDED.title
    RETURNING review_id, reservation_id
)
SELECT ins.review_id,
       ins.reservation_id,
       d.restaurant_id,
       d.customer_id,
       d.starts_at
  FROM ins
  JOIN v_reservation_details d USING (reservation_id)
"""

_SET_BODY_REF_SQL = """
UPDATE reviews
   SET body_ref = %(body_ref)s
 WHERE reservation_id = %(reservation_id)s
RETURNING review_id
"""

_GET_REVIEW_SQL = """
SELECT r.review_id, r.reservation_id, r.rating, r.title, r.body_ref,
       d.restaurant_id, d.customer_id
  FROM reviews r
  JOIN v_reservation_details d USING (reservation_id)
 WHERE r.reservation_id = %(reservation_id)s
"""


class FeedbackStore:
    """Thin wrapper over the `restaurant_feedback.reviews` collection."""

    def __init__(self, client: Any, database: str = "restaurant_feedback") -> None:
        self._client = client
        self._db = client[database]
        self._collection = self._db[COLLECTION]

    @classmethod
    def from_settings(cls, settings: Any = None, *, timeout_ms: int = 2000) -> "FeedbackStore":
        from pymongo import MongoClient

        from .config import load_settings

        settings = settings or load_settings()
        client = MongoClient(
            settings.mongo_url,
            serverSelectionTimeoutMS=timeout_ms,
            tz_aware=True,
        )
        return cls(client, settings.mongo_database)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FeedbackStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def is_available(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except Exception:  # noqa: BLE001 — any failure means "not usable"
            return False

    @property
    def collection(self) -> Any:
        return self._collection

    def ensure_indexes(self) -> None:
        """Create the collection's indexes. Idempotent — safe to call at startup.

        * `reservation_id` unique — the cross-store key, and the guard that makes
          the document upsert idempotent. It mirrors the `UNIQUE` on
          `reviews.reservation_id`, so "one review per visit" holds in both
          stores rather than only in the one that can express it declaratively.
        * `(restaurant_id, created_at desc)` — "latest prose for this restaurant",
          the query the denormalized `restaurant_id` exists to serve.
        * text over `title` + `body` — MongoDB's full-text search. This is the
          capability that justifies the store: Postgres could hold the text, but
          the point of the exercise is unstructured search over it.
        """
        self._collection.create_index("reservation_id", unique=True, name="reservation_unique")
        self._collection.create_index(
            [("restaurant_id", 1), ("created_at", -1)], name="restaurant_recent"
        )
        self._collection.create_index(
            [("title", "text"), ("body", "text")],
            name="prose_text",
            weights={"title": 3, "body": 1},
        )

    # -- writes -------------------------------------------------------------

    def upsert_document(
        self,
        *,
        reservation_id: int,
        restaurant_id: int,
        customer_id: int,
        rating: int,
        body: str,
        title: str | None = None,
        tags: Sequence[str] = (),
        language: str = "en",
        visited_at: datetime | None = None,
    ) -> str:
        """Insert or replace the document for a visit; returns its ObjectId as str.

        Keyed on `reservation_id` rather than `_id` so the caller does not have to
        know the ObjectId in advance — that is what makes step 2 of the write
        sequence retryable.
        """
        now = datetime.now(timezone.utc)
        result = self._collection.update_one(
            {"reservation_id": reservation_id},
            {
                "$set": {
                    "restaurant_id": restaurant_id,
                    "customer_id": customer_id,
                    "rating": rating,
                    "title": title,
                    "body": body,
                    "tags": list(tags),
                    "language": language,
                    "visited_at": visited_at,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            return str(result.upserted_id)

        doc = self._collection.find_one({"reservation_id": reservation_id}, {"_id": 1})
        if doc is None:  # pragma: no cover — would mean a concurrent delete
            raise FeedbackNotFound(
                f"document for reservation {reservation_id} vanished during upsert"
            )
        return str(doc["_id"])

    def delete_by_reservation(self, reservation_id: int) -> int:
        return self._collection.delete_one({"reservation_id": reservation_id}).deleted_count

    # -- reads --------------------------------------------------------------

    def get_by_reservation(self, reservation_id: int) -> Feedback | None:
        doc = self._collection.find_one({"reservation_id": reservation_id})
        return Feedback.from_document(doc) if doc else None

    def get_by_ref(self, body_ref: str) -> Feedback | None:
        """Resolve a `reviews.body_ref` pointer.

        A malformed ref returns None rather than raising: `body_ref` is
        format-checked by Postgres, so a value that bson rejects means the two
        stores have already diverged, which is `reconcile`'s problem to report.
        """
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(body_ref)
        except (InvalidId, TypeError):
            log.warning("malformed body_ref %r", body_ref)
            return None
        doc = self._collection.find_one({"_id": oid})
        return Feedback.from_document(doc) if doc else None

    def for_restaurant(self, restaurant_id: int, *, limit: int = 20) -> list[Feedback]:
        cursor = (
            self._collection.find({"restaurant_id": restaurant_id})
            .sort("created_at", -1)
            .limit(limit)
        )
        return [Feedback.from_document(d) for d in cursor]

    def search(
        self, query: str, *, restaurant_id: int | None = None, limit: int = 20
    ) -> list[Feedback]:
        """Full-text search over title and body, best matches first.

        Requires the `prose_text` index from `ensure_indexes()`.
        """
        criteria: dict[str, Any] = {"$text": {"$search": query}}
        if restaurant_id is not None:
            criteria["restaurant_id"] = restaurant_id
        cursor = (
            self._collection.find(criteria, {"score": {"$meta": "textScore"}})
            .sort([("score", {"$meta": "textScore"})])
            .limit(limit)
        )
        return [Feedback.from_document(d) for d in cursor]

    def count(self) -> int:
        return self._collection.count_documents({})


# -- cross-store operations -------------------------------------------------
# Free functions rather than methods: these span Postgres and MongoDB, and
# neither store owns them.


def add_review(
    db: Database,
    store: FeedbackStore,
    *,
    reservation_id: int,
    rating: int,
    body: str,
    title: str | None = None,
    food_rating: int | None = None,
    service_rating: int | None = None,
    ambience_rating: int | None = None,
    tags: Sequence[str] = (),
    language: str = "en",
) -> Feedback:
    """Write a review across both stores, in the order the module docstring sets out.

    Idempotent: re-running with the same `reservation_id` updates both sides and
    re-links them, which is how a crash between steps 2 and 3 is healed.

    Raises:
        ReviewNotFound: the reservation does not exist.
        psycopg2.errors.CheckViolation: the visit is not completed yet — the
            `reviews_validate` trigger refused it.
    """
    # 1. Ratings row in Postgres. body_ref stays NULL for now.
    with db.transaction() as cur:
        cur.execute(
            _UPSERT_REVIEW_SQL,
            {
                "reservation_id": reservation_id,
                "rating": rating,
                "food_rating": food_rating,
                "service_rating": service_rating,
                "ambience_rating": ambience_rating,
                "title": title,
            },
        )
        row = cur.fetchone()

    if row is None:
        raise ReviewNotFound(f"reservation_id {reservation_id} does not exist")

    # 2. Prose document in MongoDB.
    body_ref = store.upsert_document(
        reservation_id=reservation_id,
        restaurant_id=row["restaurant_id"],
        customer_id=row["customer_id"],
        rating=rating,
        body=body,
        title=title,
        tags=tags,
        language=language,
        visited_at=row["starts_at"],
    )

    # 3. Commit the link. Until this lands the document is unreachable from
    #    Postgres, which reconcile() reports as "unlinked".
    with db.transaction() as cur:
        cur.execute(
            _SET_BODY_REF_SQL, {"body_ref": body_ref, "reservation_id": reservation_id}
        )
        if cur.fetchone() is None:  # pragma: no cover — row existed a moment ago
            raise ReviewNotFound(
                f"review for reservation {reservation_id} disappeared before linking"
            )

    feedback = store.get_by_reservation(reservation_id)
    if feedback is None:  # pragma: no cover
        raise FeedbackNotFound(f"document for reservation {reservation_id} not found")
    return feedback


def get_review_feedback(
    db: Database, store: FeedbackStore, reservation_id: int
) -> Feedback:
    """Resolve a review's prose by following `body_ref`.

    Raises:
        ReviewNotFound: no review row for that reservation.
        FeedbackNotFound: `body_ref` is NULL, or points at a missing document.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(_GET_REVIEW_SQL, {"reservation_id": reservation_id})
        row = cur.fetchone()

    if row is None:
        raise ReviewNotFound(f"no review for reservation_id {reservation_id}")
    if row["body_ref"] is None:
        raise FeedbackNotFound(
            f"review for reservation {reservation_id} has ratings but no prose"
        )

    feedback = store.get_by_ref(row["body_ref"])
    if feedback is None:
        raise FeedbackNotFound(
            f"reviews.body_ref {row['body_ref']} for reservation "
            f"{reservation_id} resolves to no document"
        )
    return feedback


def reconcile(
    db: Database,
    store: FeedbackStore,
    *,
    repair: bool = False,
    reservation_ids: Iterable[int] | None = None,
) -> dict[str, list[int]]:
    """Find — and optionally repair — divergence between the two stores.

    No database can enforce a foreign key into MongoDB (`docs/er-diagram.md` §6),
    so the invariant "every non-null `body_ref` resolves to a document" has to be
    checked by running this. Three ways it can break:

    * **dangling** — Postgres points at a document that is not there. Repairable
      only if a document for the same reservation exists under a different
      `_id`; otherwise the prose is genuinely lost and the ref is cleared.
    * **unlinked** — a document exists but `body_ref` is NULL. This is the
      crash-between-steps-2-and-3 case. Always repairable: set the pointer.
    * **orphaned** — a document whose reservation has no review row.
      **Reported, never deleted.** Automatically destroying a diner's prose
      because a pointer went missing is not a repair, and it is not reversible.

    `reservation_ids` narrows the comparison to those visits. Without it this is a
    whole-database operation, and with `repair=True` a whole-database *write* —
    which is only correct when `store` is the collection those reviews actually
    point into. Pointing a repair at the wrong collection would clear every
    `body_ref` it could not resolve, so scope it whenever the scope is known.

    Loads both key sets into memory; fine at this project's scale (thousands of
    reviews), and the point where it stops being fine is the point to switch to a
    batched scan.
    """
    scope = list(reservation_ids) if reservation_ids is not None else None

    with db.transaction(readonly=True) as cur:
        if scope is None:
            cur.execute("SELECT reservation_id, body_ref FROM reviews")
        else:
            cur.execute(
                "SELECT reservation_id, body_ref FROM reviews "
                " WHERE reservation_id = ANY(%s)",
                (scope,),
            )
        pg_rows = {r["reservation_id"]: r["body_ref"] for r in cur.fetchall()}

    criteria: dict[str, Any] = {} if scope is None else {"reservation_id": {"$in": scope}}
    mongo_rows = {
        d["reservation_id"]: str(d["_id"])
        for d in store.collection.find(criteria, {"reservation_id": 1})
    }

    dangling: list[int] = []
    unlinked: list[int] = []
    orphaned: list[int] = []
    repaired: list[int] = []

    for reservation_id, body_ref in pg_rows.items():
        actual = mongo_rows.get(reservation_id)
        if body_ref is None:
            if actual is not None:
                unlinked.append(reservation_id)
        elif actual is None or actual != body_ref:
            dangling.append(reservation_id)

    for reservation_id in mongo_rows:
        if reservation_id not in pg_rows:
            orphaned.append(reservation_id)

    if repair:
        for reservation_id in unlinked + dangling:
            actual = mongo_rows.get(reservation_id)
            with db.transaction() as cur:
                cur.execute(
                    _SET_BODY_REF_SQL,
                    {"body_ref": actual, "reservation_id": reservation_id},
                )
            repaired.append(reservation_id)
            if actual is None:
                log.warning(
                    "reservation %s had a dangling body_ref and no document; "
                    "pointer cleared, prose is lost",
                    reservation_id,
                )

    return {
        "dangling": sorted(dangling),
        "unlinked": sorted(unlinked),
        "orphaned": sorted(orphaned),
        "repaired": sorted(repaired),
    }


__all__ = [
    "COLLECTION",
    "FeedbackStore",
    "add_review",
    "get_review_feedback",
    "reconcile",
]