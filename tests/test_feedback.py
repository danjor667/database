"""MongoDB feedback store and the cross-store review write.

The interesting cases are not "does it save" but the seams: what a half-finished
write leaves behind, and whether `reconcile` can tell the three kinds of
divergence apart. No database can enforce a foreign key into MongoDB, so these
tests are the only thing standing in for that constraint.
"""

from __future__ import annotations

import pytest
from psycopg2 import errors as pg_errors

from app import Database, FeedbackNotFound, ReviewNotFound, book_reservation
from app.feedback import FeedbackStore, add_review, get_review_feedback, reconcile


def _body_ref(db: Database, reservation_id: int) -> str | None:
    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT body_ref FROM reviews WHERE reservation_id = %s", (reservation_id,)
        )
        row = cur.fetchone()
    return row["body_ref"] if row else None


def test_add_review_writes_both_stores(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    reservation_id = unreviewed[0]
    feedback = add_review(
        db,
        store,
        reservation_id=reservation_id,
        rating=5,
        title="Superb",
        body="The agnolotti were the best thing I have eaten all year.",
        tags=["pasta", "date-night"],
    )

    # Postgres holds the ratings and the pointer.
    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT rating, title, body_ref FROM reviews WHERE reservation_id = %s",
            (reservation_id,),
        )
        row = cur.fetchone()
    assert row["rating"] == 5
    assert row["title"] == "Superb"

    # MongoDB holds the prose, and the pointer resolves to it.
    assert row["body_ref"] == feedback.feedback_id
    assert store.get_by_ref(row["body_ref"]).body == feedback.body
    assert feedback.tags == ("pasta", "date-night")


def test_get_review_feedback_follows_the_pointer(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    reservation_id = unreviewed[0]
    add_review(
        db, store, reservation_id=reservation_id, rating=4,
        body="Service was unhurried in the best way.",
    )

    feedback = get_review_feedback(db, store, reservation_id)
    assert feedback.reservation_id == reservation_id
    assert "unhurried" in feedback.body
    assert feedback.restaurant_id > 0


def test_add_review_is_idempotent(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    """Re-running heals a crash between the document write and the link."""
    reservation_id = unreviewed[0]
    first = add_review(
        db, store, reservation_id=reservation_id, rating=3, body="Fine.",
    )
    second = add_review(
        db, store, reservation_id=reservation_id, rating=5, body="Better on reflection.",
    )

    assert first.feedback_id == second.feedback_id, "a second document was created"
    assert store.collection.count_documents({"reservation_id": reservation_id}) == 1
    assert second.body == "Better on reflection."

    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT rating FROM reviews WHERE reservation_id = %s", (reservation_id,)
        )
        assert cur.fetchone()["rating"] == 5


def test_review_on_an_unfinished_visit_is_refused(
    db: Database, store: FeedbackStore, restaurant_id: int, customer_id: int, slot
):
    """The Postgres trigger runs first, so no orphan document is created."""
    reservation = book_reservation(
        db, restaurant_id=restaurant_id, customer_id=customer_id,
        starts_at=slot, party_size=2,
    )

    with pytest.raises(pg_errors.CheckViolation):
        add_review(
            db, store, reservation_id=reservation.reservation_id, rating=5,
            body="Reviewing a dinner that has not happened yet.",
        )

    assert store.get_by_reservation(reservation.reservation_id) is None


def test_unknown_reservation_is_rejected(db: Database, store: FeedbackStore):
    with pytest.raises((ReviewNotFound, pg_errors.ForeignKeyViolation)):
        add_review(
            db, store, reservation_id=10_000_000, rating=5, body="Nowhere.",
        )


def test_review_without_prose_reports_precisely(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    """Ratings-only is legal; following a null pointer is a distinct outcome."""
    reservation_id = unreviewed[0]
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO reviews (reservation_id, rating) VALUES (%s, 4)",
            (reservation_id,),
        )

    with pytest.raises(FeedbackNotFound, match="no prose"):
        get_review_feedback(db, store, reservation_id)


def test_missing_review_row_reports_precisely(db: Database, store: FeedbackStore):
    with pytest.raises(ReviewNotFound):
        get_review_feedback(db, store, 10_000_000)


def test_full_text_search(db: Database, store: FeedbackStore, unreviewed: list[int]):
    add_review(
        db, store, reservation_id=unreviewed[0], rating=5,
        body="The sommelier suggested an extraordinary orange wine.",
    )
    add_review(
        db, store, reservation_id=unreviewed[1], rating=2,
        body="Parking was impossible and the room was deafening.",
    )

    hits = store.search("sommelier wine")
    assert [f.reservation_id for f in hits] == [unreviewed[0]]

    assert store.search("deafening")[0].reservation_id == unreviewed[1]
    assert store.search("zzzzznotpresent") == []


def test_search_can_be_scoped_to_a_restaurant(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    added = [
        add_review(
            db, store, reservation_id=rid, rating=4,
            body="Consistently excellent bread service.",
        )
        for rid in unreviewed[:3]
    ]
    target = added[0].restaurant_id

    scoped = store.search("bread", restaurant_id=target)
    assert scoped, "expected at least one match"
    assert {f.restaurant_id for f in scoped} == {target}


def test_for_restaurant_returns_newest_first(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    added = [
        add_review(db, store, reservation_id=rid, rating=4, body=f"Visit {i}.")
        for i, rid in enumerate(unreviewed[:3])
    ]
    restaurant_id = added[0].restaurant_id
    mine = {a.reservation_id for a in added if a.restaurant_id == restaurant_id}

    listed = store.for_restaurant(restaurant_id, limit=50)
    seen = [f.created_at for f in listed if f.reservation_id in mine]
    assert seen == sorted(seen, reverse=True)


def test_ratings_remain_aggregatable_in_postgres(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    """The split must not take the numbers out of reach of the analytics."""
    feedback = add_review(
        db, store, reservation_id=unreviewed[0], rating=5,
        food_rating=5, service_rating=4, ambience_rating=5,
        body="Numbers stay in Postgres; this sentence does not.",
    )

    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT review_count, avg_rating FROM v_restaurant_ratings "
            " WHERE restaurant_id = %s",
            (feedback.restaurant_id,),
        )
        row = cur.fetchone()
    assert row["review_count"] > 0
    assert row["avg_rating"] is not None


# -- reconcile --------------------------------------------------------------


def test_reconcile_detects_and_repairs_an_unlinked_document(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    """The crash-between-step-2-and-3 case: prose written, pointer never set."""
    reservation_id = unreviewed[0]
    feedback = add_review(
        db, store, reservation_id=reservation_id, rating=4, body="Linked, for now.",
    )

    with db.transaction() as cur:
        cur.execute(
            "UPDATE reviews SET body_ref = NULL WHERE reservation_id = %s",
            (reservation_id,),
        )

    scope = [reservation_id]
    report = reconcile(db, store, reservation_ids=scope)
    assert reservation_id in report["unlinked"]
    assert reservation_id not in report["dangling"]

    repaired = reconcile(db, store, repair=True, reservation_ids=scope)
    assert reservation_id in repaired["repaired"]
    assert _body_ref(db, reservation_id) == feedback.feedback_id
    assert reservation_id not in reconcile(db, store, reservation_ids=scope)["unlinked"]


def test_reconcile_detects_a_dangling_pointer(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    """Postgres points at a document that is no longer there."""
    reservation_id = unreviewed[0]
    add_review(db, store, reservation_id=reservation_id, rating=4, body="Doomed prose.")
    store.delete_by_reservation(reservation_id)

    scope = [reservation_id]
    report = reconcile(db, store, reservation_ids=scope)
    assert reservation_id in report["dangling"]

    # No document survives, so repair can only clear the false pointer.
    reconcile(db, store, repair=True, reservation_ids=scope)
    assert _body_ref(db, reservation_id) is None


def test_reconcile_reports_orphans_without_deleting_them(
    db: Database, store: FeedbackStore
):
    """A document whose review row is gone is reported, never destroyed."""
    orphan_reservation = 9_999_999
    store.collection.insert_one(
        {
            "reservation_id": orphan_reservation,
            "restaurant_id": 1,
            "customer_id": 1,
            "rating": 5,
            "body": "Prose that outlived its review row.",
            "tags": [],
        }
    )
    try:
        report = reconcile(
            db, store, repair=True, reservation_ids=[orphan_reservation]
        )
        assert orphan_reservation in report["orphaned"]
        assert orphan_reservation not in report["repaired"]
        assert store.get_by_reservation(orphan_reservation) is not None, (
            "reconcile deleted a diner's prose"
        )
    finally:
        store.delete_by_reservation(orphan_reservation)


def test_a_healthy_pair_is_reported_clean(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    reservation_id = unreviewed[0]
    add_review(db, store, reservation_id=reservation_id, rating=5, body="All consistent.")

    report = reconcile(db, store, reservation_ids=[reservation_id])
    assert reservation_id not in report["dangling"]
    assert reservation_id not in report["unlinked"]
    assert reservation_id not in report["orphaned"]


def test_scoped_repair_leaves_other_reviews_alone(
    db: Database, store: FeedbackStore, unreviewed: list[int]
):
    """A scoped repair must not touch rows outside its scope.

    Regression test for a destructive bug in this suite: `reconcile(repair=True)`
    was called unscoped against the *test* collection while pointing at the
    *real* reviews table, so every seeded review resolved to nothing, was
    classified as dangling, and had its body_ref cleared. The tests passed and
    the development dataset was wrecked.
    """
    mine = unreviewed[0]
    add_review(db, store, reservation_id=mine, rating=4, body="In scope.")

    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT reservation_id, body_ref FROM reviews
             WHERE body_ref IS NOT NULL AND reservation_id <> %s
             ORDER BY reservation_id LIMIT 5
            """,
            (mine,),
        )
        bystanders = {r["reservation_id"]: r["body_ref"] for r in cur.fetchall()}

    if not bystanders:
        pytest.skip("no linked reviews outside the scope — run backfill_feedback.py")

    reconcile(db, store, repair=True, reservation_ids=[mine])

    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT reservation_id, body_ref FROM reviews "
            " WHERE reservation_id = ANY(%s)",
            (list(bystanders),),
        )
        after = {r["reservation_id"]: r["body_ref"] for r in cur.fetchall()}

    assert after == bystanders, "a scoped repair modified rows outside its scope"


def test_malformed_body_ref_resolves_to_nothing(db: Database, store: FeedbackStore):
    assert store.get_by_ref("not-a-valid-objectid") is None