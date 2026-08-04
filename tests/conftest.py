"""Shared fixtures.

Tests run against the seeded development database. To stay clear of seed data
(which spans 45 days back to 14 days forward), every test books slots ~300 days
out, and an autouse fixture removes anything in that window afterwards.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import Database, load_settings

# Far enough past the seeded range that test bookings cannot collide with it.
TEST_HORIZON_DAYS = 300


@pytest.fixture(scope="session")
def db() -> Database:
    # Pool must be wide enough for the concurrency tests to actually contend
    # rather than queueing on connection acquisition.
    settings = load_settings()
    database = Database(
        settings.dsn,
        pool_min=2,
        pool_max=24,
        app_name="rr-tests",
        # Mutual EXCLUDE conflicts resolve via deadlock detection, which costs a
        # full deadlock_timeout (1s by default) per occurrence. The contention
        # tests provoke these deliberately, so at the default a single unlucky
        # run took 14 minutes. 100ms keeps the suite quick without changing any
        # behaviour under test — the retry path is identical, just cheaper.
        options="-c deadlock_timeout=100ms",
    )
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(autouse=True)
def clean_test_window(db: Database):
    """Delete test-window reservations before and after each test."""

    def purge() -> None:
        with db.transaction() as cur:
            cur.execute(
                """
                DELETE FROM reservations
                 WHERE lower(during) >= %s::timestamptz
                """,
                (_horizon_start(),),
            )

    purge()
    yield
    purge()


def _horizon_start() -> datetime:
    return datetime.combine(
        date.today() + timedelta(days=TEST_HORIZON_DAYS - 5),
        time(0, 0),
        tzinfo=timezone.utc,
    )


@pytest.fixture
def slot() -> datetime:
    """A dinner slot far in the future, timezone-aware."""
    return datetime.combine(
        date.today() + timedelta(days=TEST_HORIZON_DAYS),
        time(19, 0),
        tzinfo=timezone.utc,
    )


@pytest.fixture(scope="session")
def restaurant_id(db: Database) -> int:
    with db.transaction(readonly=True) as cur:
        cur.execute("SELECT restaurant_id FROM restaurants WHERE name = %s", ("Vero Nord",))
        row = cur.fetchone()
    assert row is not None, "seed data missing — run ./scripts/db.sh reset"
    return row["restaurant_id"]


@pytest.fixture(scope="session")
def customer_ids(db: Database) -> list[int]:
    with db.transaction(readonly=True) as cur:
        cur.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 30")
        rows = cur.fetchall()
    assert len(rows) >= 30, "seed data missing — run ./scripts/db.sh reset"
    return [r["customer_id"] for r in rows]


@pytest.fixture
def customer_id(customer_ids: list[int]) -> int:
    return customer_ids[0]


@pytest.fixture(scope="session")
def two_seat_table(db: Database, restaurant_id: int) -> int:
    """A specific two-top, for tests that pin the table."""
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT table_id
              FROM restaurant_tables
             WHERE restaurant_id = %s AND seats = 2 AND is_active
             ORDER BY table_id
             LIMIT 1
            """,
            (restaurant_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row["table_id"]