"""Concurrency proof: the reservation transaction cannot double-book.

These are the tests that justify the design. A `SELECT`-then-`INSERT` booking
function passes every test in test_booking.py and still corrupts the schedule
under load, because nothing serializes the gap between the two statements. The
cases here open that gap deliberately — many threads released simultaneously by a
barrier, all competing for the same slot — and assert on the outcome.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest

from app import (
    BookingConflict,
    Database,
    NoTableAvailable,
    book_reservation,
    find_available_tables,
)


def _race(worker, thread_count: int) -> list:
    """Run `worker(i)` in `thread_count` threads released at the same instant."""
    barrier = Barrier(thread_count)

    def runner(i: int):
        barrier.wait()  # maximize overlap — no thread starts before the others
        return worker(i)

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        return list(pool.map(runner, range(thread_count)))


def test_only_one_thread_wins_a_single_table(
    db: Database,
    restaurant_id: int,
    customer_ids: list[int],
    slot: datetime,
    two_seat_table: int,
):
    """12 threads, one table, one slot: exactly one reservation may exist."""
    thread_count = 12

    def worker(i: int):
        try:
            res = book_reservation(
                db,
                restaurant_id=restaurant_id,
                customer_id=customer_ids[i],
                starts_at=slot,
                party_size=2,
                table_id=two_seat_table,
                max_attempts=3,
            )
            return ("booked", res.reservation_id)
        except (NoTableAvailable, BookingConflict) as exc:
            return ("rejected", type(exc).__name__)

    results = _race(worker, thread_count)
    booked = [r for r in results if r[0] == "booked"]

    assert len(booked) == 1, f"expected exactly one winner, got {results}"
    assert len(results) == thread_count

    # And the database agrees: one live row for that table and slot.
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT count(*) AS n
              FROM reservations
             WHERE table_id = %s
               AND during && tstzrange(%s::timestamptz,
                                       %s::timestamptz + interval '105 min', '[)')
               AND status IN ('pending', 'confirmed', 'seated', 'completed')
            """,
            (two_seat_table, slot, slot),
        )
        assert cur.fetchone()["n"] == 1


def test_contending_threads_fill_every_table_exactly_once(
    db: Database, restaurant_id: int, customer_ids: list[int], slot: datetime
):
    """More threads than tables: each free table is claimed exactly once.

    This is the strongest statement of the invariant — no table is lost to a
    spurious conflict, and none is handed to two customers.
    """
    capacity = len(
        find_available_tables(
            db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
        )
    )
    assert capacity >= 4, "seed data should leave several two-seatable tables free"

    thread_count = capacity + 8

    def worker(i: int):
        try:
            res = book_reservation(
                db,
                restaurant_id=restaurant_id,
                customer_id=customer_ids[i % len(customer_ids)],
                starts_at=slot,
                party_size=2,
                # Budget must exceed `capacity`: a losing thread has to walk past
                # every table it loses before it can conclude the house is full.
                max_attempts=thread_count + capacity,
            )
            return res.table_id
        except NoTableAvailable:
            return None

    results = _race(worker, thread_count)
    won = [t for t in results if t is not None]

    assert len(won) == capacity, f"expected {capacity} winners, got {len(won)}"
    assert len(set(won)) == capacity, "a table was booked twice"
    assert results.count(None) == thread_count - capacity


def test_no_overlapping_live_reservations_anywhere(
    db: Database, restaurant_id: int, customer_ids: list[int], slot: datetime
):
    """After a contended run, assert the global invariant with a self-join."""

    def worker(i: int):
        try:
            book_reservation(
                db,
                restaurant_id=restaurant_id,
                customer_id=customer_ids[i % len(customer_ids)],
                starts_at=slot,
                party_size=2 if i % 2 == 0 else 4,
                max_attempts=20,
            )
        except NoTableAvailable:
            pass

    _race(worker, 16)

    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT count(*) AS n
              FROM reservations a
              JOIN reservations b
                ON a.table_id = b.table_id
               AND a.reservation_id < b.reservation_id
               AND a.during && b.during
             WHERE a.status IN ('pending', 'confirmed', 'seated', 'completed')
               AND b.status IN ('pending', 'confirmed', 'seated', 'completed')
            """
        )
        assert cur.fetchone()["n"] == 0


def test_exhausted_retry_budget_raises_conflict(
    db: Database,
    restaurant_id: int,
    customer_ids: list[int],
    slot: datetime,
    two_seat_table: int,
):
    """With max_attempts=1 and heavy contention, losers report BookingConflict.

    Distinguishing this from NoTableAvailable matters operationally: it means
    "try again", not "the restaurant is full".
    """
    thread_count = 10

    def worker(i: int):
        try:
            book_reservation(
                db,
                restaurant_id=restaurant_id,
                customer_id=customer_ids[i],
                starts_at=slot,
                party_size=2,
                table_id=two_seat_table,
                max_attempts=1,
            )
            return "booked"
        except BookingConflict:
            return "conflict"
        except NoTableAvailable:
            return "unavailable"

    results = _race(worker, thread_count)

    assert results.count("booked") == 1
    # The rest either lost the race inside the statement (conflict) or found the
    # table already taken when they looked (unavailable). Both are correct.
    assert results.count("booked") + results.count("conflict") + results.count(
        "unavailable"
    ) == thread_count


def test_pool_is_not_leaked_under_load(
    db: Database, restaurant_id: int, customer_ids: list[int], slot: datetime
):
    """Run many more bookings than the pool has connections.

    If `Database.connection` failed to return connections on the error path, this
    would starve once the pool drained rather than fail an assertion.

    Each worker books a *different* slot on purpose. An earlier version pointed
    them all at one slot, which made this a contention stress test wearing a
    pooling test's name: past capacity every thread burned its full retry budget,
    each mutual conflict costing a deadlock_timeout, and workers held connections
    long enough to starve their peers into PoolExhausted. Contention belongs in
    the tests above; this one isolates connection lifecycle.
    """
    attempts = db.pool_max * 4

    def worker(i: int):
        # One hour apart, so no two workers compete for the same table.
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_ids[i % len(customer_ids)],
            starts_at=slot + timedelta(hours=i),
            party_size=2,
        )
        return True

    with ThreadPoolExecutor(max_workers=db.pool_max) as pool:
        assert all(pool.map(worker, range(attempts)))

    # The pool still works afterwards.
    assert find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=6
    ) is not None


def test_more_concurrent_callers_than_connections(
    db: Database, restaurant_id: int, slot: datetime
):
    """Callers beyond pool_max must wait for a connection, not get an error.

    psycopg2's ThreadedConnectionPool raises PoolError the moment maxconn
    connections are checked out. Database.connection() gates admission with a
    semaphore so the surplus callers block instead. Regression test for exactly
    that: the thread count deliberately exceeds the pool.
    """
    thread_count = db.pool_max + 8

    def worker(i: int):
        # Every caller holds a connection for a beat, guaranteeing overlap.
        with db.transaction(readonly=True) as cur:
            cur.execute("SELECT pg_sleep(0.05), %s::int AS i", (i,))
            return cur.fetchone()["i"]

    results = _race(worker, thread_count)
    assert sorted(results) == list(range(thread_count))