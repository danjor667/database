"""Transactional reservation booking.

## Where correctness actually comes from

It comes from the schema, not from this module. The naive shape is two
round-trips — "SELECT a free table", then "INSERT a reservation for it" — and
between those statements another transaction can commit a booking for the same
table. Under READ COMMITTED neither session sees a problem, because plain reads
take no lock that would make the second writer wait, so both read "free" and both
insert. Absent a database-level rule, that is a double-booking.

`reservations_no_double_booking` is that rule, so the corrupt outcome is
unreachable however this code is written: the second insert gets SQLSTATE 23P01
instead. `SELECT ... FOR UPDATE` on the candidate rows or SERIALIZABLE isolation
would also work, but both make every booking pay for contention that is usually
absent.

## Why this is one statement

Given the constraint, what remains is a question of behaviour under contention
rather than of integrity. Folding the candidate search into `INSERT ... SELECT`
buys three things:

* one network round-trip per attempt, and a conflict window narrow enough that
  retries are rare in practice;
* no dependence on isolation level, since the index checks the constraint at
  insert time;
* contention that reads as a signal rather than a failure — a losing booker gets
  23P01 (or 40P01; see `_RETRYABLE`), which this module *retries onto a different
  table*. A two-statement version would be equally safe but would surface the
  conflict to the caller as an error, when the honest answer is usually "you got
  table 7 instead of 4".

`max_attempts` therefore bounds absorbable contention, not latency: each retry
re-runs the candidate search and lands on a different table. A caller racing N-1
others for the last few tables needs enough attempts to walk past the tables it
loses.

Every query here is parameterized; no SQL is built by string interpolation.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import TYPE_CHECKING

from psycopg2 import errors as pg_errors

if TYPE_CHECKING:  # avoids importing redis at runtime for callers with no cache
    from .cache import AvailabilityCache

from .availability import _FREE_TABLES, _REQUEST_CTE, build_params, validate_request
from .config import load_settings
from .db import Database
from .errors import (
    BookingConflict,
    CustomerNotFound,
    NoTableAvailable,
    ReservationNotFound,
    RestaurantNotFound,
    TableNotBookable,
)
from .models import Reservation

log = logging.getLogger(__name__)

# Contention signals, all meaning "the transaction was rolled back, nothing was
# written, try again" — never "the request was invalid".
#
# 23P01 ExclusionViolation is the obvious one. 40P01 DeadlockDetected is the
# subtle one, and it is not a pathology: an EXCLUDE constraint inserts its index
# tuple *first* and only then scans for conflicts, waiting on any conflicting
# transaction it finds. Two bookers racing for the same table have therefore each
# already inserted an entry the other must wait on, so they wait on each other
# and Postgres breaks the cycle by aborting one. That victim's booking is
# perfectly valid; it simply needs to be reattempted, exactly like a 23P01 loser.
# 40001 is included for the same reason should the isolation level ever be
# raised.
_RETRYABLE = (
    pg_errors.ExclusionViolation,
    pg_errors.DeadlockDetected,
    pg_errors.SerializationFailure,
)

# Selection and insertion in one statement. `ORDER BY seats` is the capacity
# policy (smallest table that fits); the `random()` tiebreak spreads concurrent
# bookers across equally-sized tables instead of stacking them all onto the
# lowest table_id, which measurably cuts retries under load.
BOOK_SQL = f"""
WITH {_REQUEST_CTE},
free AS (
{_FREE_TABLES}
),
candidate AS (
    SELECT table_id, during, party_size
      FROM free
     ORDER BY seats, random()
     LIMIT 1
)
INSERT INTO reservations
        (table_id, customer_id, during, party_size, status, special_requests)
SELECT c.table_id,
       %(customer_id)s::int,
       c.during,
       c.party_size,
       %(status)s::reservation_status,
       %(special_requests)s::text
  FROM candidate c
RETURNING reservation_id, table_id, customer_id, during, party_size,
          status, special_requests
"""

# RETURNING can only see the modified row, but cache invalidation needs the
# restaurant. Wrapping the UPDATE in a CTE lets one round-trip produce both.
_CANCEL_SQL = """
WITH updated AS (
    UPDATE reservations
       SET status = 'cancelled'
     WHERE reservation_id = %(reservation_id)s
       AND status NOT IN ('completed', 'cancelled')
    RETURNING reservation_id, table_id, customer_id, during, party_size,
              status, special_requests
)
SELECT u.*, rt.restaurant_id
  FROM updated u
  JOIN restaurant_tables rt USING (table_id)
"""

_GET_SQL = """
SELECT reservation_id, table_id, customer_id, during, party_size,
       status, special_requests
  FROM reservations
 WHERE reservation_id = %(reservation_id)s
"""

_LIST_SQL = """
SELECT reservation_id, table_id, customer_id, during, party_size,
       status, special_requests
  FROM reservations
 WHERE customer_id = %(customer_id)s
 ORDER BY lower(during) DESC
 LIMIT %(limit)s
"""


def book_reservation(
    db: Database,
    *,
    restaurant_id: int,
    customer_id: int,
    starts_at: datetime,
    party_size: int,
    zone: str | None = None,
    table_id: int | None = None,
    duration_minutes: int | None = None,
    special_requests: str | None = None,
    status: str = "confirmed",
    max_attempts: int | None = None,
    cache: "AvailabilityCache | None" = None,
) -> Reservation:
    """Book the smallest suitable free table, atomically.

    Args:
        restaurant_id: must be active.
        customer_id: must exist.
        starts_at: timezone-aware; the end is derived from the restaurant's
            `slot_minutes` unless `duration_minutes` overrides it.
        party_size: must fit the table's `[min_party_size, seats]` range.
        zone: optional 'indoor' | 'patio' | 'bar' | 'private' preference.
        table_id: pin a specific table instead of letting the query choose.
        max_attempts: contention budget; defaults to BOOKING_MAX_ATTEMPTS.
        cache: if given, its availability entries for this restaurant are
            invalidated once the booking commits.

    Raises:
        InvalidBookingRequest: naive `starts_at`, or `party_size` < 1.
        RestaurantNotFound / CustomerNotFound: unknown or inactive referent.
        TableNotBookable: `table_id` given but unusable for this booking.
        NoTableAvailable: valid request, nothing free in that slot.
        BookingConflict: repeatedly lost the race; retrying later may succeed.
    """
    validate_request(starts_at, party_size)
    if max_attempts is None:
        max_attempts = load_settings().booking_max_attempts
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    params = build_params(
        restaurant_id=restaurant_id,
        starts_at=starts_at,
        party_size=party_size,
        zone=zone,
        duration_minutes=duration_minutes,
        table_id=table_id,
    )
    params.update(
        customer_id=customer_id,
        status=status,
        special_requests=special_requests,
    )

    for attempt in range(1, max_attempts + 1):
        try:
            with db.transaction() as cur:
                cur.execute(BOOK_SQL, params)
                row = cur.fetchone()

            if row is not None:
                # Strictly after the commit above. Bumping the generation while
                # the transaction was still open would let a concurrent reader
                # cache pre-commit availability under the new generation, where
                # nothing would supersede it. See app/cache.py.
                if cache is not None:
                    cache.invalidate(restaurant_id)
                return Reservation.from_row(row)

            # Zero rows inserted: the candidate subquery found nothing. That is
            # a real "unavailable", not a race — diagnose why and raise.
            _raise_unavailable(db, params)

        except _RETRYABLE as exc:
            # Lost a race with a concurrent booking. Postgres has already rolled
            # this transaction back, so retrying is safe: the next attempt
            # re-runs the candidate search and lands on a different table.
            log.debug(
                "booking attempt %d/%d for restaurant %s at %s lost a race (%s)",
                attempt,
                max_attempts,
                restaurant_id,
                starts_at,
                exc.pgcode,
            )
            if attempt < max_attempts:
                # Brief randomized backoff to de-synchronize contending writers.
                time.sleep(random.uniform(0.0, 0.02) * attempt)
            continue

        except pg_errors.ForeignKeyViolation as exc:
            raise CustomerNotFound(f"customer_id {customer_id} does not exist") from exc

        except pg_errors.CheckViolation as exc:
            # The cross-table trigger refused it — e.g. the pinned table is
            # inactive, or the party does not fit.
            raise TableNotBookable(str(exc).strip()) from exc

    raise BookingConflict(
        f"could not secure a table at restaurant {restaurant_id} for "
        f"{starts_at.isoformat()} after {max_attempts} attempts; "
        "contention is high — retry later"
    )


def _raise_unavailable(db: Database, params: dict[str, object]) -> None:
    """Turn an empty candidate set into a specific, actionable error.

    Only runs on the failure path, so the extra queries cost nothing in the
    common case. Distinguishing these cases matters: "no such restaurant" is a
    caller bug, while "fully booked" is a normal answer to a valid question.
    """
    restaurant_id = params["restaurant_id"]
    customer_id = params.get("customer_id")
    table_id = params.get("table_id")
    party_size = params["party_size"]

    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT is_active FROM restaurants WHERE restaurant_id = %s",
            (restaurant_id,),
        )
        restaurant = cur.fetchone()
        if restaurant is None:
            raise RestaurantNotFound(f"restaurant_id {restaurant_id} does not exist")
        if not restaurant["is_active"]:
            raise RestaurantNotFound(f"restaurant_id {restaurant_id} is not active")

        if customer_id is not None:
            cur.execute(
                "SELECT 1 FROM customers WHERE customer_id = %s", (customer_id,)
            )
            if cur.fetchone() is None:
                raise CustomerNotFound(f"customer_id {customer_id} does not exist")

        if table_id is not None:
            cur.execute(
                """
                SELECT rt.is_active, rt.seats, rt.min_party_size, rt.restaurant_id
                  FROM restaurant_tables rt
                 WHERE rt.table_id = %s
                """,
                (table_id,),
            )
            table = cur.fetchone()
            if table is None:
                raise TableNotBookable(f"table_id {table_id} does not exist")
            if table["restaurant_id"] != restaurant_id:
                raise TableNotBookable(
                    f"table_id {table_id} belongs to restaurant "
                    f"{table['restaurant_id']}, not {restaurant_id}"
                )
            if not table["is_active"]:
                raise TableNotBookable(f"table_id {table_id} is not active")
            if not table["min_party_size"] <= party_size <= table["seats"]:  # type: ignore[operator]
                raise TableNotBookable(
                    f"table_id {table_id} seats {table['min_party_size']}-"
                    f"{table['seats']}, not {party_size}"
                )
            raise NoTableAvailable(
                f"table_id {table_id} is already booked for that slot"
            )

        # Is the party even seatable here, ignoring time? Separates "we have no
        # table that size" from "they are all taken tonight".
        cur.execute(
            """
            SELECT count(*) AS n
              FROM restaurant_tables
             WHERE restaurant_id = %s
               AND is_active
               AND %s BETWEEN min_party_size AND seats
            """,
            (restaurant_id, party_size),
        )
        seatable = cur.fetchone()["n"]  # type: ignore[index]

    if seatable == 0:
        raise NoTableAvailable(
            f"restaurant {restaurant_id} has no table that seats a party of "
            f"{party_size}"
        )
    raise NoTableAvailable(
        f"all {seatable} table(s) seating a party of {party_size} at restaurant "
        f"{restaurant_id} are booked for that slot"
    )


def cancel_reservation(
    db: Database, reservation_id: int, *, cache: "AvailabilityCache | None" = None
) -> Reservation:
    """Cancel a reservation, releasing its slot for rebooking.

    Cancelling sets a status rather than deleting the row: the partial exclusion
    constraint stops counting it, so the table frees up while history survives.

    A cancellation *adds* availability, so failing to invalidate here would hide
    a newly free table until the entry's TTL expired.
    """
    existing = None
    with db.transaction() as cur:
        cur.execute(_CANCEL_SQL, {"reservation_id": reservation_id})
        row = cur.fetchone()
        if row is None:
            # Nothing updated: either it does not exist, or it is already terminal.
            cur.execute(_GET_SQL, {"reservation_id": reservation_id})
            existing = cur.fetchone()

    # Outside the block, so the transaction has committed — invalidating earlier
    # would let a concurrent reader cache pre-commit availability under the new
    # generation. See app/cache.py.
    if row is not None:
        if cache is not None:
            cache.invalidate(row["restaurant_id"])
        return Reservation.from_row(row)

    if existing is None:
        raise ReservationNotFound(f"reservation_id {reservation_id} does not exist")
    raise BookingConflict(
        f"reservation {reservation_id} is {existing['status']} and cannot be cancelled"
    )


def get_reservation(db: Database, reservation_id: int) -> Reservation:
    with db.transaction(readonly=True) as cur:
        cur.execute(_GET_SQL, {"reservation_id": reservation_id})
        row = cur.fetchone()
    if row is None:
        raise ReservationNotFound(f"reservation_id {reservation_id} does not exist")
    return Reservation.from_row(row)


def list_customer_reservations(
    db: Database, customer_id: int, *, limit: int = 20
) -> list[Reservation]:
    with db.transaction(readonly=True) as cur:
        cur.execute(_LIST_SQL, {"customer_id": customer_id, "limit": limit})
        return [Reservation.from_row(row) for row in cur.fetchall()]


__all__ = [
    "book_reservation",
    "cancel_reservation",
    "get_reservation",
    "list_customer_reservations",
]