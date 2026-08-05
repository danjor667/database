#!/usr/bin/env python3
"""Give the seeded reviews real MongoDB documents.

    .venv/bin/python scripts/backfill_feedback.py [--drop] [--limit N]

`sql/06_seed.sql` fabricates `reviews.body_ref` with `substr(md5(...), 1, 24)`:
the values satisfy the ObjectId format CHECK but point at nothing, so every
seeded review is a dangling cross-store reference. This script writes the
documents those pointers should have resolved to and repoints `body_ref` at
them, leaving the development dataset internally consistent.

Prose is generated from the rating with a PRNG seeded per reservation, so
re-running produces identical text — the same reproducibility `setseed(0.42)`
gives the SQL seed.

Both halves are bulk operations: one `bulk_write` into MongoDB and one
`execute_values` UPDATE into Postgres, rather than ~2,650 round trips each.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import Database, load_settings  # noqa: E402
from app.feedback import FeedbackStore, reconcile  # noqa: E402

# Indexed by rating (1-5). Each entry is a pool of openings, so two 5-star
# reviews of the same restaurant do not read identically.
#
# Rating 1 is present for completeness but the seed never produces it: the
# per-restaurant baseline floor is 3.10 and the noise term is +/-0.8, so the
# lowest reachable rating is 2.
_OPENINGS = {
    1: ["A genuinely poor evening.", "We left early.", "Not what was promised."],
    2: ["Some real problems.", "Mixed, leaning wrong.", "It did not come together."],
    3: ["Perfectly decent.", "Solid without being memorable.", "No complaints, no fireworks."],
    4: ["A very good night.", "We enjoyed ourselves a lot.", "Close to excellent."],
    5: ["One of the best meals of the year.", "Faultless.", "We are already planning to return."],
}

_FOOD = {
    1: ["The food arrived cold and under-seasoned."],
    2: ["The kitchen was inconsistent — one dish sang, the rest did not."],
    3: ["The cooking was competent and the portions fair."],
    4: ["Most plates were excellent, and the produce was obviously good."],
    5: ["Every plate was precise, and the seasoning was exactly right."],
}

_SERVICE = {
    1: ["Service was inattentive and we waited far too long."],
    2: ["Service meant well but was clearly stretched."],
    3: ["Service was efficient if a little impersonal."],
    4: ["The floor staff were warm and well-informed."],
    5: ["Service was unhurried, generous, and read the table perfectly."],
}

_ROOM = {
    1: ["The room was deafening.", "The lighting was unforgiving."],
    2: ["The room felt cramped at the edges."],
    3: ["The room is comfortable enough."],
    4: ["The room is handsome and the acoustics are kind."],
    5: ["The room is the rare kind you linger in."],
}

_TAGS = {
    1: ["disappointing"], 2: ["mixed"], 3: ["reliable"],
    4: ["recommended", "good-value"], 5: ["outstanding", "special-occasion"],
}


def _compose(rating: int, restaurant: str, seed: int) -> tuple[str, list[str]]:
    rng = random.Random(seed)
    opening = rng.choice(_OPENINGS[rating])
    # Shuffle a real list — `rng.shuffle(parts[1:])` would shuffle a throwaway
    # copy and leave the order fixed.
    detail = [
        rng.choice(_FOOD[rating]),
        rng.choice(_SERVICE[rating]),
        rng.choice(_ROOM[rating]),
    ]
    rng.shuffle(detail)
    body = f"{opening} {' '.join(detail)} ({restaurant})"
    return body, list(_TAGS[rating])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop", action="store_true", help="empty the collection first")
    parser.add_argument("--limit", type=int, default=None, help="only N reviews")
    args = parser.parse_args()

    from bson import ObjectId
    from psycopg2.extras import execute_values
    from pymongo import UpdateOne

    settings = load_settings()
    print(f"postgres: {settings.dsn}")
    print(f"mongodb:  {settings.mongo_url.split('@')[-1]}")

    with Database.from_settings(settings) as db, FeedbackStore.from_settings(settings) as store:
        if not store.is_available():
            print("mongodb is not reachable — run ./scripts/db.sh up", file=sys.stderr)
            return 1

        if args.drop:
            removed = store.collection.delete_many({}).deleted_count
            print(f"dropped {removed} existing document(s)")

        store.ensure_indexes()

        sql = """
            SELECT reservation_id, rating, title, restaurant_id, restaurant_name,
                   customer_id, visited_at
              FROM v_restaurant_reviews
             ORDER BY reservation_id
        """
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"

        with db.transaction(readonly=True) as cur:
            cur.execute(sql)
            rows = cur.fetchall()

        if not rows:
            print("no reviews found — run ./scripts/db.sh reset")
            return 1
        print(f"composing prose for {len(rows)} review(s)")

        operations, links = [], []
        for row in rows:
            # Generated client-side so the Postgres pointer is known before the
            # document is written, letting both halves run as single bulk calls.
            oid = ObjectId()
            body, tags = _compose(
                row["rating"], row["restaurant_name"], row["reservation_id"]
            )
            operations.append(
                UpdateOne(
                    {"reservation_id": row["reservation_id"]},
                    {
                        "$set": {
                            "restaurant_id": row["restaurant_id"],
                            "customer_id": row["customer_id"],
                            "rating": row["rating"],
                            "title": row["title"],
                            "body": body,
                            "tags": tags,
                            "language": "en",
                            "visited_at": row["visited_at"],
                        },
                        "$setOnInsert": {"_id": oid, "created_at": row["visited_at"]},
                    },
                    upsert=True,
                )
            )
            links.append((row["reservation_id"], str(oid)))

        result = store.collection.bulk_write(operations, ordered=False)
        print(f"mongodb: {result.upserted_count} inserted, {result.modified_count} updated")

        # A document that already existed kept its original _id, so re-read the
        # authoritative ids rather than trusting the ones just generated.
        actual = {
            d["reservation_id"]: str(d["_id"])
            for d in store.collection.find({}, {"reservation_id": 1})
        }
        links = [(rid, actual[rid]) for rid, _ in links if rid in actual]

        with db.transaction() as cur:
            execute_values(
                cur,
                """
                UPDATE reviews SET body_ref = v.ref
                  FROM (VALUES %s) AS v(reservation_id, ref)
                 WHERE reviews.reservation_id = v.reservation_id
                """,
                links,
                template="(%s::bigint, %s::text)",
                # One statement, so `rowcount` covers every row. At the default
                # page_size execute_values splits into batches and rowcount
                # reports only the last one.
                page_size=max(len(links), 1),
            )
            print(f"postgres: {cur.rowcount} body_ref(s) repointed")

        report = reconcile(db, store)
        print(
            "\nreconcile: "
            f"{len(report['dangling'])} dangling, "
            f"{len(report['unlinked'])} unlinked, "
            f"{len(report['orphaned'])} orphaned"
        )
        if any(report[k] for k in ("dangling", "unlinked")):
            print("  (re-run without --limit to link the remainder)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())