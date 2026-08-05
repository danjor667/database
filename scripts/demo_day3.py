#!/usr/bin/env python3
"""End-to-end walkthrough of the Day 3 integrations.

    .venv/bin/python scripts/demo_day3.py

Shows the Redis availability cache (hit/miss, invalidation on write, behaviour
under an outage) and the MongoDB feedback store (search, cross-store resolution,
reconciliation). Run `scripts/backfill_feedback.py` first so the seeded reviews
have documents to find.

Books against a slot far outside the seeded range and cleans up after itself.
"""

from __future__ import annotations

import sys
import time
from datetime import date, datetime, time as time_of_day, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (  # noqa: E402
    Database,
    book_reservation,
    cancel_reservation,
    find_available_tables,
    load_settings,
)
from app.cache import AvailabilityCache  # noqa: E402
from app.feedback import FeedbackStore, get_review_feedback, reconcile  # noqa: E402

DEMO_DAYS_OUT = 330


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - start) * 1000


def main() -> int:
    settings = load_settings()

    with Database.from_settings(settings) as db:
        with db.transaction(readonly=True) as cur:
            cur.execute(
                "SELECT restaurant_id, name FROM restaurants WHERE name = 'Vero Nord'"
            )
            restaurant = cur.fetchone()
            cur.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 3")
            customers = [r["customer_id"] for r in cur.fetchall()]

        if restaurant is None:
            print("seed data missing — run ./scripts/db.sh reset")
            return 1
        rid = restaurant["restaurant_id"]

        slot = datetime.combine(
            date.today() + timedelta(days=DEMO_DAYS_OUT),
            time_of_day(19, 0),
            tzinfo=timezone.utc,
        )

        cache = AvailabilityCache.from_settings(settings)
        store = FeedbackStore.from_settings(settings)

        if not cache.is_available():
            print("redis is not reachable — run ./scripts/db.sh up")
            return 1
        if not store.is_available():
            print("mongodb is not reachable — run ./scripts/db.sh up")
            return 1

        _purge(db, slot)
        cache.invalidate(rid)  # start from a known generation

        try:
            # ---------------------------------------------------------------
            heading("1. Redis: cold lookup, then warm")
            cold, cold_ms = timed(
                lambda: find_available_tables(
                    db, restaurant_id=rid, starts_at=slot, party_size=4, cache=cache
                )
            )
            warm, warm_ms = timed(
                lambda: find_available_tables(
                    db, restaurant_id=rid, starts_at=slot, party_size=4, cache=cache
                )
            )
            print(f"   {len(cold)} tables free")
            print(f"   cold (postgres): {cold_ms:6.2f} ms")
            print(f"   warm (redis):    {warm_ms:6.2f} ms")
            print(f"   identical result: {cold == warm}")
            print(f"   stats: {cache.stats}")

            # ---------------------------------------------------------------
            heading("2. A booking invalidates the cached listing")
            generation = cache.generation(rid)
            reservation = book_reservation(
                db,
                restaurant_id=rid,
                customer_id=customers[0],
                starts_at=slot,
                party_size=4,
                cache=cache,
            )
            print(f"   booked reservation {reservation.reservation_id} "
                  f"on table {reservation.table_id}")
            print(f"   generation {generation} → {cache.generation(rid)} "
                  f"(every entry for this restaurant is now unreachable)")
            after = find_available_tables(
                db, restaurant_id=rid, starts_at=slot, party_size=4, cache=cache
            )
            print(f"   listing refreshed: {len(cold)} → {len(after)} tables")

            # ---------------------------------------------------------------
            heading("3. Cancelling puts the table back")
            cancel_reservation(db, reservation.reservation_id, cache=cache)
            restored = find_available_tables(
                db, restaurant_id=rid, starts_at=slot, party_size=4, cache=cache
            )
            print(f"   listing refreshed: {len(after)} → {len(restored)} tables")

            # ---------------------------------------------------------------
            heading("4. A Redis outage degrades to a miss, never an error")
            import logging

            import redis as redis_lib

            # The cache logs the outage with a traceback, which is right in a
            # real system and only noise here — the point of the section is that
            # the *caller* sees nothing.
            logging.getLogger("app.cache").setLevel(logging.CRITICAL)

            dead = AvailabilityCache(
                redis_lib.Redis(
                    host="127.0.0.1", port=1,
                    socket_timeout=0.1, socket_connect_timeout=0.1,
                ),
                ttl_seconds=30,
            )
            result, ms = timed(
                lambda: find_available_tables(
                    db, restaurant_id=rid, starts_at=slot, party_size=4, cache=dead
                )
            )
            print(f"   {len(result)} tables returned in {ms:.2f} ms with redis down")
            print(f"   errors recorded: {dead.stats['errors']} — the caller saw none")
            dead.close()

            # ---------------------------------------------------------------
            heading("5. MongoDB: full-text search over the prose")
            total = store.count()
            print(f"   {total} feedback document(s) in the collection")
            if total == 0:
                print("   run scripts/backfill_feedback.py to populate them")
            else:
                # Terms drawn from the backfill's own vocabulary, one per band:
                # 5-star, 2-star, and 5-star service respectively.
                for term in ("faultless", "inconsistent", "unhurried"):
                    hits = store.search(term, limit=3)
                    print(f"   '{term}': {len(hits)} hit(s)")
                    for f in hits[:2]:
                        print(f"      [{f.rating}★] {f.body[:68]}…")

            # ---------------------------------------------------------------
            heading("6. Following reviews.body_ref across the two stores")
            with db.transaction(readonly=True) as cur:
                cur.execute(
                    """
                    SELECT reservation_id FROM reviews
                     WHERE body_ref IS NOT NULL
                     ORDER BY reservation_id LIMIT 1
                    """
                )
                row = cur.fetchone()
            if row is None:
                print("   no linked reviews — run scripts/backfill_feedback.py")
            else:
                feedback = get_review_feedback(db, store, row["reservation_id"])
                print(f"   reservation {feedback.reservation_id} → "
                      f"ObjectId {feedback.feedback_id}")
                print(f"   rating {feedback.rating}★ (postgres), "
                      f"{len(feedback.body)} chars of prose (mongodb)")
                print(f"   tags: {', '.join(feedback.tags) or '—'}")

            # ---------------------------------------------------------------
            heading("7. Cross-store consistency check")
            report = reconcile(db, store)
            for kind in ("dangling", "unlinked", "orphaned"):
                print(f"   {kind:<9} {len(report[kind])}")
            print("   (no database can enforce a foreign key into MongoDB —")
            print("    running this is what stands in for the constraint)")

        finally:
            removed = _purge(db, slot)
            cache.invalidate(rid)
            cache.close()
            store.close()
            print(f"\ncleaned up {removed} demo reservation(s)")

    return 0


def _purge(db: Database, slot: datetime) -> int:
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM reservations WHERE lower(during) >= %s::timestamptz",
            (slot - timedelta(days=1),),
        )
        return cur.rowcount


if __name__ == "__main__":
    raise SystemExit(main())