#!/usr/bin/env python3
"""End-to-end walkthrough of the Day 4 analytical queries.

    .venv/bin/python scripts/demo_analytics.py

Per-city RANK(), location search over the GiST index, window functions over
aggregates, and the combined query that uses all of them. For the timings behind
these queries, see docs/performance.md or run scripts/explain.py.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time as time_of_day, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import Database, load_settings  # noqa: E402
from app.analytics import (  # noqa: E402
    busiest_slots,
    find_nearby_available,
    nearby_restaurants,
    nearest_restaurants,
    rank_restaurants_by_city,
    rating_trend,
)

# Times Square — longitude first, the order ST_MakePoint takes.
TIMES_SQUARE = {"longitude": -73.9855, "latitude": 40.7580}
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def main() -> int:
    settings = load_settings()

    with Database.from_settings(settings) as db:
        with db.transaction(readonly=True) as cur:
            cur.execute("SELECT count(*) AS n FROM reviews")
            if cur.fetchone()["n"] == 0:
                print("seed data missing — run ./scripts/db.sh reset")
                return 1

        # -------------------------------------------------------------------
        heading("1. Restaurants ranked by average review score, per city")
        print("   RANK() OVER (PARTITION BY city_id ORDER BY avg_rating DESC)")
        current = None
        for row in rank_restaurants_by_city(db, min_reviews=1):
            if row.city != current:
                current = row.city
                print(f"\n   {row.city} ({row.city_restaurant_count} restaurants, "
                      f"avg {row.city_avg_rating})")
            print(f"     #{row.city_rank}  {row.restaurant_name:<20} "
                  f"{row.avg_rating:>5}  {row.vs_city_avg:+5}  "
                  f"{row.review_count:>4} reviews ({row.pct_of_city_reviews}%)")

        # -------------------------------------------------------------------
        heading("2. Top restaurant in every city")
        print("   max_rank=1 is a per-group top-N — ORDER BY ... LIMIT 1 would")
        print("   return one row overall, not one per city.")
        for row in rank_restaurants_by_city(db, max_rank=1, min_reviews=1):
            print(f"   {row.city:<15} {row.restaurant_name:<20} {row.avg_rating}")

        # -------------------------------------------------------------------
        heading("3. Location search — within 5 km of Times Square")
        print("   ST_DWithin over the GiST index; geography means real metres.")
        for row in nearby_restaurants(db, **TIMES_SQUARE, radius_metres=5000):
            rating = row.avg_rating if row.avg_rating is not None else "—"
            print(f"   {row.name:<20} {row.metres:>7.0f} m   {'$' * row.price_tier:<4} "
                  f"rating {rating}")

        heading("4. Nearest five, with no radius at all")
        print("   ORDER BY location <-> point LIMIT 5 — the index is walked in")
        print("   distance order and stops early instead of measuring everything.")
        for row in nearest_restaurants(db, **TIMES_SQUARE, limit=5):
            print(f"   {row.name:<20} {row.metres:>8.0f} m   {row.city}")

        # -------------------------------------------------------------------
        heading("5. Well-rated, nearby, and actually bookable")
        slot = datetime.combine(
            date.today() + timedelta(days=340), time_of_day(19, 0), tzinfo=timezone.utc
        )
        print(f"   party of 4 at {slot:%Y-%m-%d %H:%M} UTC, within 500 km")
        print("   geography narrows -> ratings rank -> availability anti-join decides")
        rows = find_nearby_available(
            db, **TIMES_SQUARE, starts_at=slot, party_size=4,
            radius_metres=500_000, limit=10,
        )
        for row in rows:
            print(f"   #{row['rating_rank']} {row['name']:<20} "
                  f"{row['metres']:>8} m  rating {row['avg_rating']}  "
                  f"{row['free_tables']} tables free (smallest fits {row['smallest_fit']})")
        if not rows:
            print("   nothing available in that window")

        # -------------------------------------------------------------------
        heading("6. Busiest slots per restaurant")
        print("   RANK() over an aggregate — count(*) ranked within each restaurant")
        for row in busiest_slots(db, top_n=2):
            print(f"   {row['restaurant_name']:<20} #{row['demand_rank']}  "
                  f"{WEEKDAYS[row['weekday']]} {row['hour_of_day']:02d}:00   "
                  f"{row['bookings']:>3} bookings, {row['covers']:>4} covers "
                  f"({row['pct_of_bookings']}% of its total)")

        # -------------------------------------------------------------------
        heading("7. Rating trend — LAG for month-over-month movement")
        with db.transaction(readonly=True) as cur:
            cur.execute("SELECT restaurant_id, name FROM restaurants ORDER BY restaurant_id LIMIT 1")
            first = cur.fetchone()
        print(f"   {first['name']}: avg, change vs previous month, 3-month rolling")
        for row in rating_trend(db, restaurant_id=first["restaurant_id"]):
            delta = f"{row['delta']:+}" if row["delta"] is not None else "   —"
            print(f"   {row['month']}   {row['avg_rating']:>5}   {delta:>6}   "
                  f"{row['rolling_3mo']:>5}   ({row['reviews']} reviews)")

        print("\nTimings for all of the above: docs/performance.md")
        print("or run: .venv/bin/python scripts/explain.py --scale")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())