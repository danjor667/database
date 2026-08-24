"""Command-line interface to the reservation backend.

Everything the demo scripts walk through, as addressable commands:

    python -m app restaurants
    python -m app availability 1 --at 2027-06-15T19:00 --party 4
    python -m app book 1 --customer 7 --at 2027-06-15T19:00 --party 4 --zone patio
    python -m app cancel 9501
    python -m app rank --top 3
    python -m app nearby --lon -73.99 --lat 40.735 --radius 3000
    python -m app review add 42 --rating 5 --body "Superb tasting menu"
    python -m app reconcile

Three decisions worth knowing about:

* **argparse, not click.** The package's dependency policy (see app/__init__.py)
  is that the core runs with psycopg2 alone; a CLI framework would be the first
  import that exists for ergonomics rather than function. argparse is enough.

* **redis / pymongo are imported only by the commands that need them.** Passing
  `--cache` pulls in redis; the `review` and `reconcile` commands pull in
  pymongo. Every other command works with neither installed, preserving the
  same layering as the package itself.

* **Naive datetimes are read as UTC.** The library refuses naive datetimes
  because a library cannot know what the caller meant. A terminal is different:
  someone typing `--at 2027-06-15T19:00` should not need to spell `+00:00`, so
  the CLI resolves the ambiguity itself, in one documented place, before the
  library sees it. An explicit offset is honoured as given.

Domain errors (`BookingError` and subclasses) are printed as one-line messages
and exit 1 — a full stack trace for "restaurant 99 does not exist" would bury
the answer. Genuine bugs still traceback.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .analytics import (
    busiest_slots,
    find_nearby_available,
    nearby_restaurants,
    nearest_restaurants,
    rank_restaurants_by_city,
    rating_trend,
)
from .availability import find_available_tables
from .db import Database
from .errors import BookingError
from .reservations import (
    book_reservation,
    cancel_reservation,
    get_reservation,
    list_customer_reservations,
)

ZONES = ("indoor", "patio", "bar", "private")


# -- argument parsing --------------------------------------------------------


def _parse_when(value: str) -> datetime:
    """ISO 8601 datetime; naive input is taken as UTC (see module docstring)."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO 8601 datetime (e.g. 2027-06-15T19:00)"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _add_cache_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cache",
        action="store_true",
        help="use the Redis availability cache (read-through on availability, "
        "invalidation on book/cancel); degrades to uncached if Redis is down",
    )


def _open_cache(args: argparse.Namespace):
    """The AvailabilityCache when --cache was given, else None.

    Imported here so that every command without --cache runs with redis
    uninstalled — the same rule app/__init__.py applies to the package.
    """
    if not getattr(args, "cache", False):
        return None
    from .cache import AvailabilityCache

    return AvailabilityCache.from_settings()


def _open_store():
    """A FeedbackStore, failing with a clear message when MongoDB is down.

    MongoDB holds the only copy of the prose (see app/feedback.py), so unlike
    the cache there is no degraded mode to fall back to.
    """
    from .feedback import FeedbackStore

    store = FeedbackStore.from_settings()
    if not store.is_available():
        store.close()
        raise BookingError(
            "MongoDB is not reachable — run ./scripts/db.sh up and retry"
        )
    return store


# -- output ------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return str(value)


def _emit(
    args: argparse.Namespace,
    rows: Sequence[Any],
    columns: Sequence[tuple[str, str]],
    *,
    empty: str = "nothing found",
) -> None:
    """Render rows as an aligned table, or as JSON under --json.

    `rows` may be dataclasses or mappings; `columns` is (key, header) pairs.
    """
    records: list[dict[str, Any]] = [
        dataclasses.asdict(r) if dataclasses.is_dataclass(r) else dict(r)
        for r in rows
    ]
    if args.json:
        print(json.dumps(records, indent=2, default=str))
        return
    if not records:
        print(empty)
        return

    headers = [header for _, header in columns]
    table = [[_cell(rec.get(key)) for key, _ in columns] for rec in records]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in table))
        for i in range(len(columns))
    ]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for row in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


_RESERVATION_COLUMNS = [
    ("reservation_id", "id"),
    ("table_id", "table"),
    ("customer_id", "customer"),
    ("starts_at", "starts (UTC)"),
    ("ends_at", "ends (UTC)"),
    ("party_size", "party"),
    ("status", "status"),
    ("special_requests", "requests"),
]


# -- commands: discovery -----------------------------------------------------


def fetch_restaurants(db: Database) -> list[Mapping[str, Any]]:
    """Restaurant roster with ratings. Shared with the web GUI (app/webapp.py)."""
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT r.restaurant_id, r.name, ci.name AS city, r.price_tier,
                   r.slot_minutes, ra.avg_rating, ra.review_count, r.is_active
              FROM restaurants r
              JOIN cities ci USING (city_id)
              LEFT JOIN v_restaurant_ratings ra USING (restaurant_id)
             ORDER BY r.restaurant_id
            """
        )
        return cur.fetchall()


def fetch_customers(db: Database, limit: int = 20) -> list[Mapping[str, Any]]:
    """Customer roster. Shared with the web GUI (app/webapp.py)."""
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT customer_id, first_name || ' ' || last_name AS name, email
              FROM customers
             ORDER BY customer_id
             LIMIT %(limit)s
            """,
            {"limit": limit},
        )
        return cur.fetchall()


def cmd_restaurants(db: Database, args: argparse.Namespace) -> int:
    rows = fetch_restaurants(db)
    _emit(
        args,
        rows,
        [
            ("restaurant_id", "id"),
            ("name", "name"),
            ("city", "city"),
            ("price_tier", "price"),
            ("slot_minutes", "slot min"),
            ("avg_rating", "rating"),
            ("review_count", "reviews"),
            ("is_active", "active"),
        ],
    )
    return 0


def cmd_customers(db: Database, args: argparse.Namespace) -> int:
    rows = fetch_customers(db, limit=args.limit)
    _emit(
        args,
        rows,
        [("customer_id", "id"), ("name", "name"), ("email", "email")],
    )
    return 0


# -- commands: booking -------------------------------------------------------


def cmd_availability(db: Database, args: argparse.Namespace) -> int:
    cache = _open_cache(args)
    try:
        tables = find_available_tables(
            db,
            restaurant_id=args.restaurant_id,
            starts_at=args.at,
            party_size=args.party,
            zone=args.zone,
            duration_minutes=args.duration,
            cache=cache,
        )
    finally:
        if cache is not None:
            cache.close()
    _emit(
        args,
        tables,
        [
            ("table_id", "table"),
            ("table_code", "code"),
            ("seats", "seats"),
            ("min_party_size", "min party"),
            ("zone", "zone"),
            ("starts_at", "starts (UTC)"),
            ("ends_at", "ends (UTC)"),
        ],
        empty="no tables free for that slot",
    )
    return 0


def cmd_book(db: Database, args: argparse.Namespace) -> int:
    cache = _open_cache(args)
    try:
        reservation = book_reservation(
            db,
            restaurant_id=args.restaurant_id,
            customer_id=args.customer,
            starts_at=args.at,
            party_size=args.party,
            zone=args.zone,
            table_id=args.table,
            duration_minutes=args.duration,
            special_requests=args.requests,
            cache=cache,
        )
    finally:
        if cache is not None:
            cache.close()
    if not args.json:
        print(f"booked reservation {reservation.reservation_id}")
    _emit(args, [reservation], _RESERVATION_COLUMNS)
    return 0


def cmd_cancel(db: Database, args: argparse.Namespace) -> int:
    cache = _open_cache(args)
    try:
        reservation = cancel_reservation(db, args.reservation_id, cache=cache)
    finally:
        if cache is not None:
            cache.close()
    if not args.json:
        print(f"cancelled reservation {reservation.reservation_id}")
    _emit(args, [reservation], _RESERVATION_COLUMNS)
    return 0


def cmd_reservation(db: Database, args: argparse.Namespace) -> int:
    _emit(args, [get_reservation(db, args.reservation_id)], _RESERVATION_COLUMNS)
    return 0


def cmd_reservations(db: Database, args: argparse.Namespace) -> int:
    rows = list_customer_reservations(db, args.customer_id, limit=args.limit)
    _emit(
        args,
        rows,
        _RESERVATION_COLUMNS,
        empty=f"customer {args.customer_id} has no reservations",
    )
    return 0


# -- commands: analytics -----------------------------------------------------


def cmd_rank(db: Database, args: argparse.Namespace) -> int:
    rows = rank_restaurants_by_city(
        db, city=args.city, max_rank=args.top, min_reviews=args.min_reviews
    )
    _emit(
        args,
        rows,
        [
            ("city", "city"),
            ("city_rank", "rank"),
            ("restaurant_name", "restaurant"),
            ("avg_rating", "rating"),
            ("review_count", "reviews"),
            ("city_avg_rating", "city avg"),
            ("vs_city_avg", "vs avg"),
            ("pct_of_city_reviews", "% of reviews"),
        ],
    )
    return 0


_NEARBY_COLUMNS = [
    ("restaurant_id", "id"),
    ("name", "name"),
    ("city", "city"),
    ("metres", "metres"),
    ("price_tier", "price"),
    ("avg_rating", "rating"),
    ("review_count", "reviews"),
]


def cmd_nearby(db: Database, args: argparse.Namespace) -> int:
    rows = nearby_restaurants(
        db,
        longitude=args.lon,
        latitude=args.lat,
        radius_metres=args.radius,
        min_rating=args.min_rating,
        price_tier=args.price_tier,
        limit=args.limit,
    )
    _emit(args, rows, _NEARBY_COLUMNS, empty="nothing within that radius")
    return 0


def cmd_nearest(db: Database, args: argparse.Namespace) -> int:
    rows = nearest_restaurants(
        db, longitude=args.lon, latitude=args.lat, limit=args.limit
    )
    _emit(args, rows, _NEARBY_COLUMNS)
    return 0


def cmd_nearby_available(db: Database, args: argparse.Namespace) -> int:
    rows = find_nearby_available(
        db,
        longitude=args.lon,
        latitude=args.lat,
        starts_at=args.at,
        party_size=args.party,
        radius_metres=args.radius,
        limit=args.limit,
    )
    _emit(
        args,
        rows,
        [
            ("restaurant_id", "id"),
            ("name", "name"),
            ("city", "city"),
            ("metres", "metres"),
            ("avg_rating", "rating"),
            ("rating_rank", "rank"),
            ("free_tables", "free"),
            ("smallest_fit", "smallest"),
        ],
        empty="nothing nearby can seat that party at that time",
    )
    return 0


def cmd_slots(db: Database, args: argparse.Namespace) -> int:
    rows = busiest_slots(db, restaurant_id=args.restaurant, top_n=args.top)
    _emit(
        args,
        rows,
        [
            ("restaurant_name", "restaurant"),
            ("demand_rank", "rank"),
            ("weekday", "weekday (0=Sun)"),
            ("hour_of_day", "hour"),
            ("bookings", "bookings"),
            ("covers", "covers"),
            ("avg_party_size", "avg party"),
            ("pct_of_bookings", "% of bookings"),
        ],
    )
    return 0


def cmd_trend(db: Database, args: argparse.Namespace) -> int:
    rows = rating_trend(db, restaurant_id=args.restaurant)
    _emit(
        args,
        rows,
        [
            ("restaurant_name", "restaurant"),
            ("month", "month"),
            ("reviews", "reviews"),
            ("avg_rating", "rating"),
            ("delta", "delta"),
            ("rolling_3mo", "3mo avg"),
        ],
    )
    return 0


# -- commands: reviews (cross-store) -----------------------------------------

_FEEDBACK_COLUMNS = [
    ("reservation_id", "reservation"),
    ("restaurant_id", "restaurant"),
    ("customer_id", "customer"),
    ("rating", "rating"),
    ("title", "title"),
    ("body", "body"),
]


def cmd_review_add(db: Database, args: argparse.Namespace) -> int:
    from .feedback import add_review

    body = sys.stdin.read() if args.body == "-" else args.body
    store = _open_store()
    try:
        feedback = add_review(
            db,
            store,
            reservation_id=args.reservation_id,
            rating=args.rating,
            body=body,
            title=args.title,
            food_rating=args.food,
            service_rating=args.service,
            ambience_rating=args.ambience,
            tags=args.tag,
        )
    finally:
        store.close()
    if not args.json:
        print(f"review saved for reservation {feedback.reservation_id}")
    _emit(args, [feedback], _FEEDBACK_COLUMNS)
    return 0


def cmd_review_show(db: Database, args: argparse.Namespace) -> int:
    from .feedback import get_review_feedback

    store = _open_store()
    try:
        feedback = get_review_feedback(db, store, args.reservation_id)
    finally:
        store.close()
    _emit(args, [feedback], _FEEDBACK_COLUMNS)
    return 0


def cmd_review_search(db: Database, args: argparse.Namespace) -> int:
    store = _open_store()
    try:
        rows = store.search(
            args.query, restaurant_id=args.restaurant, limit=args.limit
        )
    finally:
        store.close()
    _emit(args, rows, _FEEDBACK_COLUMNS, empty="no reviews match")
    return 0


def cmd_reconcile(db: Database, args: argparse.Namespace) -> int:
    from .feedback import reconcile

    store = _open_store()
    try:
        report = reconcile(
            db, store, repair=args.repair, reservation_ids=args.ids or None
        )
    finally:
        store.close()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for kind, ids in report.items():
            shown = ", ".join(map(str, ids[:10])) + (" …" if len(ids) > 10 else "")
            print(f"{kind:>9}: {len(ids):>5}  {shown}")
    # Divergence found and not repaired is a failure exit so this can run
    # from cron and page only when something is actually wrong.
    diverged = report["dangling"] or report["unlinked"] or report["orphaned"]
    return 1 if (diverged and not args.repair) else 0


# -- commands: web GUI -------------------------------------------------------


def cmd_serve(db: Database, args: argparse.Namespace) -> int:
    from .webapp import serve

    return serve(
        db, host=args.host, port=args.port, open_browser=not args.no_browser
    )


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Restaurant reservation backend CLI. Connection settings "
        "come from the environment (DATABASE_URL, REDIS_URL, MONGO_URL) with "
        "defaults matching docker-compose.yml.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table"
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    # --json is accepted both before and after the subcommand. The subparser
    # copy must default to SUPPRESS: with a real default it would write False
    # into the namespace *after* the main parser already recorded --json, and
    # `python -m app --json rank` would silently lose the flag.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit JSON instead of a table",
    )

    def command(
        name: str, func: Callable[..., int], help_: str
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, description=help_, parents=[common])
        p.set_defaults(func=func)
        return p

    # discovery
    command("restaurants", cmd_restaurants, "list restaurants with their ratings")
    p = command("customers", cmd_customers, "list customers")
    p.add_argument("--limit", type=int, default=20)

    # booking
    p = command("availability", cmd_availability, "list free tables for a slot")
    p.add_argument("restaurant_id", type=int)
    p.add_argument("--at", type=_parse_when, required=True,
                   help="ISO datetime; naive means UTC")
    p.add_argument("--party", type=int, required=True)
    p.add_argument("--zone", choices=ZONES)
    p.add_argument("--duration", type=int, metavar="MINUTES",
                   help="override the restaurant's slot_minutes")
    _add_cache_flag(p)

    p = command("book", cmd_book, "book the smallest suitable free table")
    p.add_argument("restaurant_id", type=int)
    p.add_argument("--customer", type=int, required=True)
    p.add_argument("--at", type=_parse_when, required=True,
                   help="ISO datetime; naive means UTC")
    p.add_argument("--party", type=int, required=True)
    p.add_argument("--zone", choices=ZONES)
    p.add_argument("--table", type=int, help="pin a specific table_id")
    p.add_argument("--duration", type=int, metavar="MINUTES")
    p.add_argument("--requests", help="special requests, free text")
    _add_cache_flag(p)

    p = command("cancel", cmd_cancel, "cancel a reservation, freeing its slot")
    p.add_argument("reservation_id", type=int)
    _add_cache_flag(p)

    p = command("reservation", cmd_reservation, "show one reservation")
    p.add_argument("reservation_id", type=int)

    p = command("reservations", cmd_reservations,
                "list a customer's reservations, newest first")
    p.add_argument("customer_id", type=int)
    p.add_argument("--limit", type=int, default=20)

    # analytics
    p = command("rank", cmd_rank, "per-city restaurant leaderboard")
    p.add_argument("--city")
    p.add_argument("--top", type=int, metavar="N", help="top N per city")
    p.add_argument("--min-reviews", type=int, default=0)

    p = command("nearby", cmd_nearby, "restaurants within a radius of a point")
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--radius", type=float, default=5000, metavar="METRES")
    p.add_argument("--min-rating", type=float)
    p.add_argument("--price-tier", type=int, choices=(1, 2, 3, 4))
    p.add_argument("--limit", type=int, default=20)

    p = command("nearest", cmd_nearest, "the N closest restaurants, unbounded")
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--limit", type=int, default=5)

    p = command("nearby-available", cmd_nearby_available,
                "nearby restaurants that can seat a party at a time")
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--at", type=_parse_when, required=True,
                   help="ISO datetime; naive means UTC")
    p.add_argument("--party", type=int, required=True)
    p.add_argument("--radius", type=float, default=5000, metavar="METRES")
    p.add_argument("--limit", type=int, default=10)

    p = command("slots", cmd_slots, "busiest (weekday, hour) slots per restaurant")
    p.add_argument("--restaurant", type=int)
    p.add_argument("--top", type=int, default=5)

    p = command("trend", cmd_trend, "month-over-month rating trend")
    p.add_argument("--restaurant", type=int)

    # reviews
    review = sub.add_parser("review", help="reviews: ratings in Postgres, prose in MongoDB")
    review_sub = review.add_subparsers(dest="review_command", metavar="action")

    p = review_sub.add_parser("add", help="add or update a review for a completed visit",
                              parents=[common])
    p.set_defaults(func=cmd_review_add)
    p.add_argument("reservation_id", type=int)
    p.add_argument("--rating", type=int, required=True, choices=range(1, 6))
    p.add_argument("--body", required=True,
                   help="long-form prose; '-' reads from stdin")
    p.add_argument("--title")
    p.add_argument("--food", type=int, choices=range(1, 6))
    p.add_argument("--service", type=int, choices=range(1, 6))
    p.add_argument("--ambience", type=int, choices=range(1, 6))
    p.add_argument("--tag", action="append", default=[],
                   help="repeatable: --tag date-night --tag vegan")

    p = review_sub.add_parser("show", help="show a review's prose via body_ref",
                              parents=[common])
    p.set_defaults(func=cmd_review_show)
    p.add_argument("reservation_id", type=int)

    p = review_sub.add_parser("search", help="full-text search over review prose",
                              parents=[common])
    p.set_defaults(func=cmd_review_search)
    p.add_argument("query")
    p.add_argument("--restaurant", type=int)
    p.add_argument("--limit", type=int, default=20)

    # web GUI
    p = command("serve", cmd_serve, "run the local web GUI (see app/webapp.py)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; keep it loopback — the GUI has no auth")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true",
                   help="don't open a browser tab on start")

    p = command("reconcile", cmd_reconcile,
                "check (and optionally repair) Postgres <-> MongoDB divergence; "
                "exits 1 if unrepaired divergence was found")
    p.add_argument("--repair", action="store_true",
                   help="re-link unlinked documents, clear unresolvable refs")
    p.add_argument("--ids", type=int, nargs="*", metavar="RESERVATION_ID",
                   help="limit the check to these reservations")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        with Database.from_settings() as db:
            return args.func(db, args)
    except BookingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
