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


# -- Day 3: Redis + MongoDB -------------------------------------------------
# Both fixtures skip rather than fail when the store is down, so the Day 1-2
# suite still runs against Postgres alone.

# Separate namespace and database so a test run cannot disturb development data.
TEST_CACHE_NAMESPACE = "avail:test"
TEST_MONGO_DATABASE = "restaurant_feedback_test"


@pytest.fixture(scope="session")
def cache_namespace() -> str:
    """Exposed as a fixture because `tests/` is not an importable package."""
    return TEST_CACHE_NAMESPACE


@pytest.fixture(scope="session")
def cache():
    """A cache in its own key namespace, skipped if Redis is unreachable."""
    redis = pytest.importorskip("redis", reason="redis not installed")
    from app.cache import AvailabilityCache

    settings = load_settings()
    client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
    )
    instance = AvailabilityCache(
        client, ttl_seconds=30, namespace=TEST_CACHE_NAMESPACE
    )
    if not instance.is_available():
        instance.close()
        pytest.skip("redis not reachable — run ./scripts/db.sh up")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(autouse=True)
def clean_cache_namespace(request):
    """Drop every test key before and after each test that uses the cache.

    Requested lazily via `request` so tests that never touch Redis do not pay
    for it — and do not fail when Redis is absent.
    """
    if "cache" not in request.fixturenames:
        yield
        return

    instance = request.getfixturevalue("cache")

    def purge() -> None:
        client = instance._client  # noqa: SLF001 — test-only teardown
        keys = list(client.scan_iter(match=f"{TEST_CACHE_NAMESPACE}:*", count=500))
        if keys:
            client.delete(*keys)
        instance.reset_stats()

    purge()
    yield
    purge()


@pytest.fixture(scope="session")
def store():
    """A FeedbackStore against a dedicated test database."""
    pymongo = pytest.importorskip("pymongo", reason="pymongo not installed")
    from app.feedback import FeedbackStore

    settings = load_settings()
    client = pymongo.MongoClient(
        settings.mongo_url, serverSelectionTimeoutMS=2000, tz_aware=True
    )
    instance = FeedbackStore(client, TEST_MONGO_DATABASE)
    if not instance.is_available():
        instance.close()
        pytest.skip("mongodb not reachable — run ./scripts/db.sh up")
    instance.ensure_indexes()
    try:
        yield instance
    finally:
        client.drop_database(TEST_MONGO_DATABASE)
        instance.close()


@pytest.fixture
def unreviewed(db: Database) -> list[int]:
    """Completed, past visits that carry no review yet.

    The `reviews_validate` trigger only accepts a review whose reservation is
    `completed` and whose end time has passed, so reviews cannot be tested
    against the far-future slots the booking tests use.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT res.reservation_id
              FROM reservations res
             WHERE res.status = 'completed'
               AND upper(res.during) <= now()
               AND NOT EXISTS (SELECT 1 FROM reviews rv
                                WHERE rv.reservation_id = res.reservation_id)
             ORDER BY res.reservation_id
             LIMIT 10
            """
        )
        rows = cur.fetchall()
    assert len(rows) >= 5, "seed data missing — run ./scripts/db.sh reset"
    return [r["reservation_id"] for r in rows]


@pytest.fixture(autouse=True)
def clean_reviews(request, db: Database):
    """Remove review rows written by a test, restoring the seeded state."""
    if "unreviewed" not in request.fixturenames:
        yield
        return

    ids = request.getfixturevalue("unreviewed")

    def purge() -> None:
        with db.transaction() as cur:
            cur.execute(
                "DELETE FROM reviews WHERE reservation_id = ANY(%s)", (ids,)
            )
        if "store" in request.fixturenames:
            instance = request.getfixturevalue("store")
            instance.collection.delete_many({"reservation_id": {"$in": ids}})

    purge()
    yield
    purge()


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