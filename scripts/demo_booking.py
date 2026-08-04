#!/usr/bin/env python3
"""End-to-end walkthrough of the Day 2 booking layer.

    .venv/bin/python scripts/demo_booking.py

Books against a slot far outside the seeded range, then cleans up after itself.
The last section fires 12 threads at a single table to show the exclusion
constraint arbitrating a real race.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Barrier

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (  # noqa: E402
    BookingConflict,
    Database,
    NoTableAvailable,
    book_reservation,
    cancel_reservation,
    find_available_tables,
    list_customer_reservations,
    load_settings,
)

DEMO_DAYS_OUT = 320


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def main() -> int:
    settings = load_settings()
    print(f"connecting to {settings.dsn}")

    with Database.from_settings(settings) as db:
        with db.transaction(readonly=True) as cur:
            cur.execute(
                """
                SELECT r.restaurant_id, r.name, r.slot_minutes, c.name AS city
                  FROM restaurants r JOIN cities c USING (city_id)
                 WHERE r.name = 'Vero Nord'
                """
            )
            restaurant = cur.fetchone()
            cur.execute("SELECT customer_id, first_name, last_name FROM customers ORDER BY customer_id LIMIT 14")
            customers = cur.fetchall()

        if restaurant is None:
            print("seed data missing — run ./scripts/db.sh reset")
            return 1

        rid = restaurant["restaurant_id"]
        slot = datetime.combine(
            date.today() + timedelta(days=DEMO_DAYS_OUT), time(19, 0), tzinfo=timezone.utc
        )
        print(
            f"{restaurant['name']} ({restaurant['city']}), "
            f"{restaurant['slot_minutes']}-minute seatings"
        )

        _purge(db, slot)

        try:
            heading("1. Availability for a party of 4")
            for t in find_available_tables(
                db, restaurant_id=rid, starts_at=slot, party_size=4
            ):
                print(f"   {t.table_code:<5} {t.seats} seats  {t.zone:<8} "
                      f"{t.starts_at:%Y-%m-%d %H:%M} → {t.ends_at:%H:%M}")

            heading("2. Book one")
            res = book_reservation(
                db,
                restaurant_id=rid,
                customer_id=customers[0]["customer_id"],
                starts_at=slot,
                party_size=4,
                special_requests="birthday",
            )
            print(f"   reservation {res.reservation_id} on table {res.table_id}, "
                  f"{res.starts_at:%H:%M}–{res.ends_at:%H:%M}, status={res.status}")

            heading("3. The smallest fitting table wins")
            small = book_reservation(
                db, restaurant_id=rid, customer_id=customers[1]["customer_id"],
                starts_at=slot, party_size=2,
            )
            with db.transaction(readonly=True) as cur:
                cur.execute(
                    "SELECT table_code, seats FROM restaurant_tables WHERE table_id = %s",
                    (small.table_id,),
                )
                row = cur.fetchone()
            print(f"   party of 2 → {row['table_code']} ({row['seats']} seats), "
                  f"leaving the larger tables free")

            heading("4. Cancelling frees the slot")
            cancel_reservation(db, res.reservation_id)
            print(f"   reservation {res.reservation_id} cancelled")
            again = book_reservation(
                db, restaurant_id=rid, customer_id=customers[2]["customer_id"],
                starts_at=slot, party_size=4, table_id=res.table_id,
            )
            print(f"   table {res.table_id} rebooked as reservation {again.reservation_id}")

            heading("5. Twelve threads, one table")
            target = again.table_id
            cancel_reservation(db, again.reservation_id)
            barrier = Barrier(12)

            def attempt(i: int) -> str:
                barrier.wait()
                try:
                    r = book_reservation(
                        db, restaurant_id=rid,
                        customer_id=customers[i]["customer_id"],
                        starts_at=slot, party_size=4, table_id=target,
                        max_attempts=3,
                    )
                    return f"thread {i:>2}: booked reservation {r.reservation_id}"
                except NoTableAvailable:
                    return f"thread {i:>2}: table taken"
                except BookingConflict:
                    return f"thread {i:>2}: lost the race, retry later"

            with ThreadPoolExecutor(max_workers=12) as pool:
                outcomes = list(pool.map(attempt, range(12)))
            for line in outcomes:
                print(f"   {line}")
            winners = sum(1 for o in outcomes if "booked" in o)
            print(f"   → exactly {winners} winner; the constraint refused the rest")

            heading("6. Recent bookings for one customer")
            for r in list_customer_reservations(db, customers[1]["customer_id"], limit=3):
                print(f"   {r.reservation_id:>6}  {r.starts_at:%Y-%m-%d %H:%M}  "
                      f"party {r.party_size}  {r.status}")

        finally:
            removed = _purge(db, slot)
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