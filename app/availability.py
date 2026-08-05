"""Availability lookup.

The CTE below is the read-only half of the booking feature and the query Day 4
profiles with EXPLAIN ANALYZE. Its shape matters: the `NOT EXISTS` anti-join is
served by the GiST index that backs `reservations_no_double_booking`, so no
separate index is needed to answer "is this table free?".

Note that the answer is advisory only. By the time a caller acts on it another
transaction may have taken the table — which is exactly why `book_reservation`
does not trust it and lets the exclusion constraint arbitrate.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Sequence

from .db import Database
from .errors import InvalidBookingRequest
from .models import AvailableTable

if TYPE_CHECKING:  # avoids importing redis at runtime for callers with no cache
    from .cache import AvailabilityCache

# The requested window is derived inside SQL from the restaurant's own
# slot_minutes, so a caller cannot accidentally book a 90-minute slot at a
# restaurant that seats for 120.
_REQUEST_CTE = """
    req AS (
        SELECT r.restaurant_id,
               %(party_size)s::smallint AS party_size,
               tstzrange(
                   %(starts_at)s::timestamptz,
                   %(starts_at)s::timestamptz
                       + make_interval(mins => COALESCE(%(duration_minutes)s::int,
                                                        r.slot_minutes)),
                   '[)'
               ) AS during
          FROM restaurants r
         WHERE r.restaurant_id = %(restaurant_id)s
           AND r.is_active
    )
"""

# Shared by the availability listing and the booking INSERT so the two can never
# disagree about what "available" means.
_FREE_TABLES = """
        SELECT rt.table_id, rt.table_code, rt.seats, rt.min_party_size, rt.zone,
               req.during, req.party_size
          FROM req
          JOIN restaurant_tables rt
            ON rt.restaurant_id = req.restaurant_id
           AND rt.is_active
           AND req.party_size BETWEEN rt.min_party_size AND rt.seats
         WHERE (%(zone)s::table_zone IS NULL OR rt.zone = %(zone)s::table_zone)
           AND (%(table_id)s::int IS NULL OR rt.table_id = %(table_id)s::int)
           AND NOT EXISTS (
                   SELECT 1
                     FROM reservations res
                    WHERE res.table_id = rt.table_id
                      AND res.during && req.during
                      AND res.status IN ('pending', 'confirmed', 'seated', 'completed')
               )
"""

AVAILABILITY_SQL = f"""
WITH {_REQUEST_CTE},
free AS (
{_FREE_TABLES}
)
SELECT table_id, table_code, seats, min_party_size, zone,
       lower(during) AS starts_at,
       upper(during) AS ends_at
  FROM free
 ORDER BY seats, table_id
"""


def validate_request(starts_at: datetime, party_size: int) -> None:
    """Reject requests the database would reject, but with a clearer message."""
    if starts_at.tzinfo is None or starts_at.tzinfo.utcoffset(starts_at) is None:
        raise InvalidBookingRequest(
            "starts_at must be timezone-aware; a naive datetime is ambiguous "
            "across the server and client time zones"
        )
    if party_size < 1:
        raise InvalidBookingRequest(f"party_size must be >= 1, got {party_size}")


def build_params(
    *,
    restaurant_id: int,
    starts_at: datetime,
    party_size: int,
    zone: str | None = None,
    duration_minutes: int | None = None,
    table_id: int | None = None,
) -> dict[str, object]:
    """Assemble the bind parameters shared by the availability and booking SQL."""
    return {
        "restaurant_id": restaurant_id,
        "starts_at": starts_at,
        "party_size": party_size,
        "zone": zone,
        "duration_minutes": duration_minutes,
        "table_id": table_id,
    }


def find_available_tables(
    db: Database,
    *,
    restaurant_id: int,
    starts_at: datetime,
    party_size: int,
    zone: str | None = None,
    duration_minutes: int | None = None,
    cache: "AvailabilityCache | None" = None,
) -> Sequence[AvailableTable]:
    """List tables that can seat `party_size` at `starts_at`, smallest first.

    Ordering by seats ascending is a capacity policy: filling a party of two into
    a two-top keeps the six-tops free for parties that need them.

    Passing `cache` serves repeat lookups from Redis. It changes latency, not
    semantics: the result was already advisory (another transaction may take the
    table before the caller acts on it), so a cached answer is no less trustworthy
    than a fresh one. `book_reservation` never reads this cache.
    """
    validate_request(starts_at, party_size)
    params = build_params(
        restaurant_id=restaurant_id,
        starts_at=starts_at,
        party_size=party_size,
        zone=zone,
        duration_minutes=duration_minutes,
    )

    # Read the generation *before* querying Postgres. Reading it afterwards would
    # let a write that lands mid-query bump the counter first, and this stale
    # result would then be stored under the new generation, where the next reader
    # would find it. See app/cache.py.
    generation = cache.generation(restaurant_id) if cache is not None else None

    if cache is not None and generation is not None:
        cached = cache.get(
            restaurant_id,
            generation,
            starts_at=starts_at,
            party_size=party_size,
            zone=zone,
            duration_minutes=duration_minutes,
        )
        if cached is not None:
            return cached

    with db.transaction(readonly=True) as cur:
        cur.execute(AVAILABILITY_SQL, params)
        tables = [AvailableTable.from_row(row) for row in cur.fetchall()]

    if cache is not None and generation is not None:
        cache.set(
            restaurant_id,
            generation,
            tables,
            starts_at=starts_at,
            party_size=party_size,
            zone=zone,
            duration_minutes=duration_minutes,
        )
    return tables