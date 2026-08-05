"""Redis availability cache: correctness of the read-through and invalidation.

The cache is allowed to change latency and nothing else. Every test here is a
statement of that: the same call returns the same answer cached or not, a write
makes the stale answer unreachable, and a dead Redis is invisible to callers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import Database, book_reservation, cancel_reservation, find_available_tables
from app.cache import AvailabilityCache


def _listing(db, cache, restaurant_id, slot, party_size=2, **kwargs):
    return find_available_tables(
        db,
        restaurant_id=restaurant_id,
        starts_at=slot,
        party_size=party_size,
        cache=cache,
        **kwargs,
    )


def test_first_lookup_misses_and_second_hits(
    db: Database, cache: AvailabilityCache, restaurant_id: int, slot: datetime
):
    first = _listing(db, cache, restaurant_id, slot)
    assert cache.stats == {"hits": 0, "misses": 1, "errors": 0}

    second = _listing(db, cache, restaurant_id, slot)
    assert cache.stats["hits"] == 1

    assert second == first
    assert len(first) > 0


def test_cached_result_matches_uncached(
    db: Database, cache: AvailabilityCache, restaurant_id: int, slot: datetime
):
    """The cache must be invisible in the result, not merely 'close enough'."""
    uncached = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=4
    )
    _listing(db, cache, restaurant_id, slot, party_size=4)          # populate
    cached = _listing(db, cache, restaurant_id, slot, party_size=4)  # served hot

    assert cache.stats["hits"] == 1
    assert cached == uncached


def test_booking_invalidates_the_listing(
    db: Database, cache: AvailabilityCache, restaurant_id: int,
    customer_id: int, slot: datetime,
):
    before = _listing(db, cache, restaurant_id, slot)
    _listing(db, cache, restaurant_id, slot)  # now cached
    assert cache.stats["hits"] == 1

    book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
        cache=cache,
    )

    after = _listing(db, cache, restaurant_id, slot)
    assert len(after) == len(before) - 1, "stale listing served after a booking"
    assert cache.stats["hits"] == 1, "post-write lookup should have missed"


def test_cancelling_invalidates_the_listing(
    db: Database, cache: AvailabilityCache, restaurant_id: int,
    customer_id: int, slot: datetime, two_seat_table: int,
):
    """A cancellation *adds* availability — the riskier direction to miss."""
    reservation = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
        table_id=two_seat_table,
        cache=cache,
    )
    booked = _listing(db, cache, restaurant_id, slot)
    _listing(db, cache, restaurant_id, slot)  # cached
    assert two_seat_table not in [t.table_id for t in booked]

    cancel_reservation(db, reservation.reservation_id, cache=cache)

    after = _listing(db, cache, restaurant_id, slot)
    assert two_seat_table in [t.table_id for t in after], "freed table stayed hidden"
    assert len(after) == len(booked) + 1


def test_booking_never_reads_the_cache(
    db: Database, cache: AvailabilityCache, restaurant_id: int,
    customer_id: int, slot: datetime,
):
    """Poison the cache with 'nothing is free'; booking must still succeed.

    Correctness comes from the exclusion constraint, so a cache that could veto a
    booking would be a cache that can corrupt the schedule.
    """
    generation = cache.generation(restaurant_id)
    cache.set(
        restaurant_id, generation, [], starts_at=slot, party_size=2, zone=None,
        duration_minutes=None,
    )
    assert _listing(db, cache, restaurant_id, slot) == [], "poison did not take"

    reservation = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
    )
    assert reservation.reservation_id > 0


def test_invalidation_makes_the_old_generation_unreachable(
    db: Database, cache: AvailabilityCache, restaurant_id: int, slot: datetime
):
    generation = cache.generation(restaurant_id)
    _listing(db, cache, restaurant_id, slot)
    assert cache.get(
        restaurant_id, generation, starts_at=slot, party_size=2
    ) is not None

    cache.invalidate(restaurant_id)

    assert cache.generation(restaurant_id) == generation + 1
    # The entry still exists under the old generation, but nothing reads it.
    assert cache.get(
        restaurant_id, cache.generation(restaurant_id), starts_at=slot, party_size=2
    ) is None


def test_party_size_zone_and_duration_are_separate_entries(
    db: Database, cache: AvailabilityCache, restaurant_id: int, slot: datetime
):
    """Key components must actually discriminate, or one query poisons another."""
    two = _listing(db, cache, restaurant_id, slot, party_size=2)
    six = _listing(db, cache, restaurant_id, slot, party_size=6)
    patio = _listing(db, cache, restaurant_id, slot, party_size=4, zone="patio")
    short = _listing(db, cache, restaurant_id, slot, party_size=4, duration_minutes=60)
    long_ = _listing(db, cache, restaurant_id, slot, party_size=4, duration_minutes=240)

    assert cache.stats["hits"] == 0, "distinct queries must not collide"
    assert {t.seats for t in six} == {6, 8, 10} or all(t.seats >= 6 for t in six)
    assert all(t.zone == "patio" for t in patio)
    assert len(short) >= len(long_), "a longer hold cannot free up more tables"
    assert two != six


def test_equivalent_instants_share_one_entry(
    db: Database, cache: AvailabilityCache, restaurant_id: int, slot: datetime
):
    """19:00Z and 21:00+02:00 are the same moment and must share a key."""
    _listing(db, cache, restaurant_id, slot)
    shifted = slot.astimezone(timezone(timedelta(hours=2)))
    assert shifted != slot or shifted.tzinfo != slot.tzinfo

    _listing(db, cache, restaurant_id, shifted)
    assert cache.stats["hits"] == 1, "timezone spelling changed the cache key"


def test_a_dead_redis_degrades_to_a_miss(
    db: Database, restaurant_id: int, slot: datetime, cache_namespace: str
):
    """An outage must cost latency, never correctness."""
    redis = pytest.importorskip("redis")
    dead = AvailabilityCache(
        redis.Redis(
            host="127.0.0.1", port=1, socket_timeout=0.1, socket_connect_timeout=0.1
        ),
        ttl_seconds=30,
        namespace=cache_namespace,
    )

    expected = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    got = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2, cache=dead
    )

    assert got == expected
    assert dead.stats["errors"] > 0, "the outage should have been recorded"
    dead.close()


def test_a_write_survives_a_dead_redis(
    db: Database, restaurant_id: int, customer_id: int, slot: datetime,
    cache_namespace: str,
):
    """Invalidation failure must not fail the booking it follows."""
    redis = pytest.importorskip("redis")
    dead = AvailabilityCache(
        redis.Redis(
            host="127.0.0.1", port=1, socket_timeout=0.1, socket_connect_timeout=0.1
        ),
        ttl_seconds=30,
        namespace=cache_namespace,
    )

    reservation = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
        cache=dead,
    )
    assert reservation.reservation_id > 0
    dead.close()


def test_corrupt_entry_is_treated_as_a_miss(
    db: Database, cache: AvailabilityCache, restaurant_id: int, slot: datetime
):
    """A payload this version cannot parse must not fail the request."""
    generation = cache.generation(restaurant_id)
    key = cache._entry_key(  # noqa: SLF001 — asserting on the key format is the point
        restaurant_id,
        generation,
        starts_at=slot,
        party_size=2,
        zone=None,
        duration_minutes=None,
    )
    cache._client.set(key, "{not json at all")  # noqa: SLF001

    tables = _listing(db, cache, restaurant_id, slot)

    assert tables == find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    assert cache.stats["misses"] >= 1


def test_entries_carry_a_ttl(
    db: Database, cache: AvailabilityCache, restaurant_id: int, slot: datetime,
    cache_namespace: str,
):
    """The TTL is the backstop for writes that bypass this package entirely."""
    _listing(db, cache, restaurant_id, slot)
    keys = [
        k
        for k in cache._client.scan_iter(match=f"{cache_namespace}:*")  # noqa: SLF001
        if ":gen:" not in k
    ]
    assert keys, "nothing was cached"
    for key in keys:
        assert 0 < cache._client.ttl(key) <= 30  # noqa: SLF001


def test_unknown_restaurant_is_cached_as_empty(
    db: Database, cache: AvailabilityCache, slot: datetime
):
    assert _listing(db, cache, 10_000_000, slot) == []
    assert _listing(db, cache, 10_000_000, slot) == []
    assert cache.stats["hits"] == 1