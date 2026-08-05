"""Redis availability cache.

## What is cached, and what is deliberately not

Only `find_available_tables` reads through this cache. `book_reservation` never
consults it — booking correctness comes from the exclusion constraint, and a
cache that could veto a booking would be a cache that can corrupt the schedule.
The availability listing was already advisory (see `app/availability.py`), so
serving it from Redis weakens no guarantee that was being relied on.

## Invalidation: a generation counter, not key scanning

The obvious design — delete `avail:v1:<restaurant>:*` on every write — needs
`SCAN` plus a variable number of `DEL`s, and its cost grows with how many slots
have been queried. Instead each restaurant carries a counter:

    avail:v1:gen:<restaurant_id>     ->  integer, INCR on every write

The counter is baked into every entry key, so a single `INCR` makes the entire
generation unreachable at once, in O(1), with no scanning. Superseded entries
are never deleted; they simply expire.

Two ordering rules make this correct, and both are easy to get wrong:

1. **Read the generation _before_ querying Postgres.** Reading it afterwards
   would let a write that landed mid-query bump the counter first, and the stale
   rows would then be written under the *new* generation — where the next reader
   would find them.

2. **Invalidate strictly _after_ the transaction commits.** Bumping the counter
   before the commit has the same failure: a concurrent reader picks up the new
   generation, queries Postgres, still sees pre-commit state, and caches it under
   a generation nothing will supersede.

Within those rules a losing race is harmless — the reader writes its entry under
a generation that is already obsolete, so nobody reads it.

## Failure policy

Every operation degrades to a miss. Redis being down must never turn an
availability lookup into an error, so `redis.RedisError` is caught, logged, and
reported as "not cached". Socket timeouts are deliberately short: a cache that
blocks longer than the query it replaces is worse than no cache at all.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .config import Settings, load_settings
from .models import AvailableTable

log = logging.getLogger(__name__)

# Bump when the cached payload shape changes; old entries become unreachable
# rather than being mis-parsed by the new code.
NAMESPACE = "avail:v1"

# Long enough to be irrelevant next to the entry TTL, short enough that a
# restaurant deleted from the system does not leak a key forever. It must exceed
# the entry TTL by a wide margin: if a generation counter expired while entries
# from generation 0 were still live, the counter would restart at 0 and those
# entries would become reachable again.
_GENERATION_TTL_SECONDS = 30 * 24 * 3600


class AvailabilityCache:
    """Read-through cache for availability listings, keyed by restaurant."""

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: int = 60,
        namespace: str = NAMESPACE,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._ns = namespace
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._errors = 0

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, timeout: float = 0.25
    ) -> "AvailabilityCache":
        """Build a cache from configuration.

        Connecting is lazy — redis-py does not dial until the first command — so
        this never raises just because Redis is down. `ping()` is the way to find
        out, and callers that care should use `is_available()`.
        """
        import redis  # imported here so the package works without redis installed

        settings = settings or load_settings()
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            # A cache must not amplify a Redis outage into slow requests.
            retry_on_timeout=False,
        )
        return cls(client, ttl_seconds=settings.cache_ttl_seconds)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — closing must never raise
            log.debug("error closing redis client", exc_info=True)

    def __enter__(self) -> "AvailabilityCache":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def is_available(self) -> bool:
        """True if Redis answers a PING. Used by tests and the demo to skip."""
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001 — any connection failure means "no"
            return False

    # -- keys ---------------------------------------------------------------

    def _generation_key(self, restaurant_id: int) -> str:
        return f"{self._ns}:gen:{restaurant_id}"

    def _entry_key(
        self,
        restaurant_id: int,
        generation: int,
        *,
        starts_at: datetime,
        party_size: int,
        zone: str | None,
        duration_minutes: int | None,
    ) -> str:
        # Normalize to UTC so two spellings of the same instant share one entry.
        instant = starts_at.astimezone(timezone.utc).isoformat()
        return (
            f"{self._ns}:{restaurant_id}:g{generation}:"
            f"{instant}:{party_size}:{zone or '-'}:{duration_minutes or '-'}"
        )

    # -- generation ---------------------------------------------------------

    def generation(self, restaurant_id: int) -> int | None:
        """Current generation, or None if Redis is unreachable.

        A missing counter reads as 0 rather than being created: the read path
        should not write. `invalidate` creates it on the first write.
        """
        try:
            raw = self._client.get(self._generation_key(restaurant_id))
        except Exception:  # noqa: BLE001 — see module docstring
            self._record_error()
            log.warning("redis unavailable reading generation", exc_info=True)
            return None
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            # Someone wrote a non-integer under our key; treat as generation 0.
            log.warning("non-integer generation counter for restaurant %s", restaurant_id)
            return 0

    def invalidate(self, restaurant_id: int) -> None:
        """Make every cached listing for this restaurant unreachable.

        Call this only *after* the writing transaction has committed — see the
        module docstring for why doing it earlier can cache pre-commit state.
        """
        try:
            pipe = self._client.pipeline()
            pipe.incr(self._generation_key(restaurant_id))
            pipe.expire(self._generation_key(restaurant_id), _GENERATION_TTL_SECONDS)
            pipe.execute()
        except Exception:  # noqa: BLE001
            self._record_error()
            # The entry TTL is the backstop: stale listings self-heal within it.
            log.warning(
                "redis unavailable invalidating restaurant %s; entries will "
                "expire naturally within %ss",
                restaurant_id,
                self._ttl,
                exc_info=True,
            )

    # -- entries ------------------------------------------------------------

    def get(
        self,
        restaurant_id: int,
        generation: int,
        *,
        starts_at: datetime,
        party_size: int,
        zone: str | None = None,
        duration_minutes: int | None = None,
    ) -> list[AvailableTable] | None:
        """Cached listing, or None on a miss, a decode failure, or a Redis error."""
        key = self._entry_key(
            restaurant_id,
            generation,
            starts_at=starts_at,
            party_size=party_size,
            zone=zone,
            duration_minutes=duration_minutes,
        )
        try:
            raw = self._client.get(key)
        except Exception:  # noqa: BLE001
            self._record_error()
            log.warning("redis unavailable reading %s", key, exc_info=True)
            return None

        if raw is None:
            self._record_miss()
            return None

        try:
            tables = [_decode(item) for item in json.loads(raw)]
        except (ValueError, KeyError, TypeError):
            # Corrupt or superseded payload shape — treat as a miss rather than
            # failing the request. The bad entry is overwritten by the refill.
            log.warning("discarding undecodable cache entry %s", key, exc_info=True)
            self._record_miss()
            return None

        self._record_hit()
        return tables

    def set(
        self,
        restaurant_id: int,
        generation: int,
        tables: Sequence[AvailableTable],
        *,
        starts_at: datetime,
        party_size: int,
        zone: str | None = None,
        duration_minutes: int | None = None,
    ) -> None:
        key = self._entry_key(
            restaurant_id,
            generation,
            starts_at=starts_at,
            party_size=party_size,
            zone=zone,
            duration_minutes=duration_minutes,
        )
        try:
            self._client.set(key, json.dumps([_encode(t) for t in tables]), ex=self._ttl)
        except Exception:  # noqa: BLE001
            self._record_error()
            log.warning("redis unavailable writing %s", key, exc_info=True)

    # -- stats (used by the tests and the demo) -----------------------------

    def _record_hit(self) -> None:
        with self._lock:
            self._hits += 1

    def _record_miss(self) -> None:
        with self._lock:
            self._misses += 1

    def _record_error(self) -> None:
        with self._lock:
            self._errors += 1

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "errors": self._errors}

    def reset_stats(self) -> None:
        with self._lock:
            self._hits = self._misses = self._errors = 0


# -- payload codec ----------------------------------------------------------
# Kept here rather than on the dataclass: JSON shape is a cache concern, and
# `models.AvailableTable` should not grow a serialization format it never uses
# anywhere else.


def _encode(table: AvailableTable) -> dict[str, Any]:
    return {
        "table_id": table.table_id,
        "table_code": table.table_code,
        "seats": table.seats,
        "min_party_size": table.min_party_size,
        "zone": table.zone,
        "starts_at": table.starts_at.isoformat(),
        "ends_at": table.ends_at.isoformat(),
    }


def _decode(item: Mapping[str, Any]) -> AvailableTable:
    return AvailableTable(
        table_id=item["table_id"],
        table_code=item["table_code"],
        seats=item["seats"],
        min_party_size=item["min_party_size"],
        zone=item["zone"],
        starts_at=datetime.fromisoformat(item["starts_at"]),
        ends_at=datetime.fromisoformat(item["ends_at"]),
    )


def invalidate_all(cache: "AvailabilityCache | None", restaurant_ids: Iterable[int]) -> None:
    """Invalidate several restaurants, ignoring a None cache.

    Lets call sites write `invalidate_all(cache, ids)` instead of repeating the
    `if cache is not None` guard.
    """
    if cache is None:
        return
    for restaurant_id in restaurant_ids:
        cache.invalidate(restaurant_id)


__all__ = ["AvailabilityCache", "invalidate_all", "NAMESPACE"]
