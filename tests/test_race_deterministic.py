"""Deterministic tests for the conflict-and-retry path.

The thread races in test_concurrency.py assert the right outcomes but rarely
exercise the retry branch: the window between the candidate search and the insert
is narrow enough that most losing threads simply find the table already taken.

These tests remove the timing dependency. They exploit the fact that an EXCLUDE
constraint behaves like a unique constraint with respect to uncommitted writes —
a conflicting inserter *blocks* until the first transaction resolves, and only
then learns its fate. Holding a transaction open pins the interleaving exactly,
which lets us prove:

  1. a conflicting insert waits rather than proceeding;
  2. it raises 23P01 if the holder commits;
  3. it succeeds if the holder rolls back;
  4. book_reservation turns (2) into a successful booking on another table.
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest
from psycopg2 import errors as pg_errors

from app import Database, NoTableAvailable, book_reservation, find_available_tables

_RAW_INSERT = """
INSERT INTO reservations (table_id, customer_id, during, party_size, status)
VALUES (%(table_id)s, %(customer_id)s,
        tstzrange(%(starts_at)s::timestamptz,
                  %(starts_at)s::timestamptz + interval '105 min', '[)'),
        2, 'confirmed')
"""

# Generous enough to be reliable on a loaded machine, short enough to keep the
# suite quick. Used to assert "still blocked", so a false pass needs the second
# inserter to be starved for this long, not merely slow.
_BLOCK_PROBE_SECONDS = 0.75


class _Holder:
    """Holds an uncommitted reservation open until told to commit or roll back."""

    def __init__(self, db: Database, table_id: int, customer_id: int, slot: datetime):
        self._db = db
        self._params = {
            "table_id": table_id,
            "customer_id": customer_id,
            "starts_at": slot,
        }
        self.inserted = threading.Event()
        self._release = threading.Event()
        self._commit = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with self._db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_RAW_INSERT, self._params)
            self.inserted.set()
            self._release.wait(timeout=15)
            if self._commit:
                conn.commit()
            else:
                conn.rollback()

    def __enter__(self) -> "_Holder":
        self._thread.start()
        assert self.inserted.wait(timeout=10), "holder never inserted"
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._release.set()
        self._thread.join(timeout=10)

    def commit(self) -> None:
        self._commit = True
        self._release.set()
        self._thread.join(timeout=10)

    def rollback(self) -> None:
        self._commit = False
        self._release.set()
        self._thread.join(timeout=10)


def _start(fn):
    """Run `fn` in a daemon thread; returns (thread, result-dict)."""
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — recorded for assertions
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _book_out_all_but(
    db: Database,
    *,
    restaurant_id: int,
    slot: datetime,
    customer_ids: list[int],
    keep_table_id: int,
    seats: int,
) -> None:
    """Occupy every free table of exactly `seats` size except `keep_table_id`."""
    free = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    targets = [t for t in free if t.seats == seats and t.table_id != keep_table_id]
    for i, table in enumerate(targets):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_ids[i % len(customer_ids)],
            starts_at=slot,
            party_size=2,
            table_id=table.table_id,
        )


def test_conflicting_insert_blocks_until_holder_resolves(
    db: Database, customer_ids: list[int], slot: datetime, two_seat_table: int
):
    """The second inserter must wait, not proceed on stale information."""
    with _Holder(db, two_seat_table, customer_ids[0], slot) as holder:

        def contend():
            with db.transaction() as cur:
                cur.execute(
                    _RAW_INSERT,
                    {
                        "table_id": two_seat_table,
                        "customer_id": customer_ids[1],
                        "starts_at": slot,
                    },
                )
            return "inserted"

        thread, result = _start(contend)
        thread.join(timeout=_BLOCK_PROBE_SECONDS)

        assert thread.is_alive(), "second inserter should still be blocked"
        assert result == {}, f"second inserter finished early: {result}"

        holder.commit()
        thread.join(timeout=10)

    assert "error" in result, f"expected a conflict, got {result}"
    assert isinstance(result["error"], pg_errors.ExclusionViolation)


def test_conflicting_insert_succeeds_when_holder_rolls_back(
    db: Database, customer_ids: list[int], slot: datetime, two_seat_table: int
):
    """The blocked inserter proceeds once the slot is genuinely released."""
    with _Holder(db, two_seat_table, customer_ids[0], slot) as holder:

        def contend():
            with db.transaction() as cur:
                cur.execute(
                    _RAW_INSERT,
                    {
                        "table_id": two_seat_table,
                        "customer_id": customer_ids[1],
                        "starts_at": slot,
                    },
                )
            return "inserted"

        thread, result = _start(contend)
        thread.join(timeout=_BLOCK_PROBE_SECONDS)
        assert thread.is_alive(), "second inserter should be blocked"

        holder.rollback()
        thread.join(timeout=10)

    assert result.get("value") == "inserted", f"expected success, got {result}"


def test_book_reservation_retries_onto_another_table(
    db: Database, restaurant_id: int, customer_ids: list[int], slot: datetime,
    two_seat_table: int,
):
    """The retry loop, forced deterministically.

    Every other two-top is occupied, so `book_reservation` must choose
    `two_seat_table` — which a holder has already claimed but not committed. The
    call blocks, receives 23P01 when the holder commits, retries, and lands on a
    four-top instead. Without the retry the caller would see a spurious failure.
    """
    _book_out_all_but(
        db,
        restaurant_id=restaurant_id,
        slot=slot,
        customer_ids=customer_ids,
        keep_table_id=two_seat_table,
        seats=2,
    )

    free = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    assert [t.table_id for t in free if t.seats == 2] == [two_seat_table]
    assert any(t.seats == 4 for t in free), "need a larger table to fall back to"

    with _Holder(db, two_seat_table, customer_ids[0], slot) as holder:

        def book():
            return book_reservation(
                db,
                restaurant_id=restaurant_id,
                customer_id=customer_ids[1],
                starts_at=slot,
                party_size=2,
                max_attempts=5,
            )

        thread, result = _start(book)
        thread.join(timeout=_BLOCK_PROBE_SECONDS)
        assert thread.is_alive(), "booking should be blocked on the held table"

        holder.commit()
        thread.join(timeout=15)

    assert "error" not in result, f"booking failed instead of retrying: {result}"
    reservation = result["value"]
    assert reservation.table_id != two_seat_table

    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT seats FROM restaurant_tables WHERE table_id = %s",
            (reservation.table_id,),
        )
        assert cur.fetchone()["seats"] == 4, "should have fallen back to a four-top"


def test_pinned_booking_reports_unavailable_after_losing(
    db: Database, restaurant_id: int, customer_ids: list[int], slot: datetime,
    two_seat_table: int,
):
    """Losing a race for a *pinned* table has no fallback, so it is unavailable.

    The distinction from BookingConflict matters: retrying will not help, because
    the table is now legitimately occupied.
    """
    with _Holder(db, two_seat_table, customer_ids[0], slot) as holder:

        def book():
            return book_reservation(
                db,
                restaurant_id=restaurant_id,
                customer_id=customer_ids[1],
                starts_at=slot,
                party_size=2,
                table_id=two_seat_table,
                max_attempts=3,
            )

        thread, result = _start(book)
        thread.join(timeout=_BLOCK_PROBE_SECONDS)
        assert thread.is_alive(), "booking should be blocked on the held table"

        holder.commit()
        thread.join(timeout=15)

    assert isinstance(result.get("error"), NoTableAvailable), f"got {result}"


def test_mutual_conflict_deadlocks_and_is_retried(
    db: Database, restaurant_id: int, customer_ids: list[int], slot: datetime,
    two_seat_table: int,
):
    """Two bookers racing for one table deadlock — and must still be served.

    An EXCLUDE constraint inserts its index tuple before scanning for conflicts,
    so each side has already written the entry the other must wait on. Neither
    gets a clean 23P01: Postgres detects the cycle and aborts one with 40P01.

    Staged deterministically here — both transactions insert, then each waits on
    the other — because in a thread race it appears only intermittently. It was
    found by exactly such an intermittent failure, and treating 40P01 as fatal
    would fail a booking that should simply have been retried.
    """
    other_two_top = _second_two_seat_table(db, restaurant_id, exclude=two_seat_table)

    ready = threading.Barrier(2)

    def booker(table_first: int, table_second: int, customer_id: int):
        """Insert on one table, then contend for the other — opposite orders."""

        def run():
            with db.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        _RAW_INSERT,
                        {
                            "table_id": table_first,
                            "customer_id": customer_id,
                            "starts_at": slot,
                        },
                    )
                ready.wait(timeout=10)  # both sides now hold an uncommitted row
                with conn.cursor() as cur:
                    cur.execute(
                        _RAW_INSERT,
                        {
                            "table_id": table_second,
                            "customer_id": customer_id,
                            "starts_at": slot,
                        },
                    )
                conn.commit()
            return "committed"

        return run

    thread_a, result_a = _start(booker(two_seat_table, other_two_top, customer_ids[0]))
    thread_b, result_b = _start(booker(other_two_top, two_seat_table, customer_ids[1]))
    thread_a.join(timeout=20)
    thread_b.join(timeout=20)

    outcomes = [result_a, result_b]
    errors = [r["error"] for r in outcomes if "error" in r]
    assert len(errors) == 1, f"expected exactly one victim, got {outcomes}"
    assert isinstance(errors[0], pg_errors.DeadlockDetected)

    # The point of the test: book_reservation classes 40P01 as retryable, so a
    # caller hitting this path still gets a table rather than an exception.
    from app.reservations import _RETRYABLE

    assert isinstance(errors[0], _RETRYABLE)


def _second_two_seat_table(db: Database, restaurant_id: int, *, exclude: int) -> int:
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT table_id FROM restaurant_tables
             WHERE restaurant_id = %s AND seats = 2 AND is_active AND table_id <> %s
             ORDER BY table_id LIMIT 1
            """,
            (restaurant_id, exclude),
        )
        row = cur.fetchone()
    assert row is not None, "need two two-seat tables for the deadlock test"
    return row["table_id"]


def test_exclusion_constraint_is_the_backstop(
    db: Database, customer_ids: list[int], slot: datetime, two_seat_table: int
):
    """Bypass the application entirely: raw double insert must still be refused.

    Guards against someone "optimizing" the constraint away on the grounds that
    the Python layer already checks availability.
    """
    params = {
        "table_id": two_seat_table,
        "customer_id": customer_ids[0],
        "starts_at": slot,
    }
    with db.transaction() as cur:
        cur.execute(_RAW_INSERT, params)

    with pytest.raises(pg_errors.ExclusionViolation):
        with db.transaction() as cur:
            cur.execute(_RAW_INSERT, params)