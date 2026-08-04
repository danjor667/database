"""Connection pooling and transaction scoping.

Everything in this package acquires connections through `Database`. Two rules it
exists to enforce:

* a connection is always returned to the pool, including on exception — a leaked
  connection is invisible until the pool is exhausted, at which point every
  request fails;
* a broken connection is discarded rather than recycled, since handing a dead
  socket back to the next caller turns one transient failure into many.

It also fixes a sharp edge in `psycopg2.pool.ThreadedConnectionPool`: `getconn()`
raises `PoolError` immediately once `maxconn` connections are checked out, rather
than waiting for one to be returned. That turns a brief traffic spike into hard
failures for callers that would happily have waited 50ms. A semaphore gates
acquisition so that at most `pool_max` callers reach `getconn()` at once, and the
rest block until a slot frees or `acquire_timeout` expires.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Iterator

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extensions import connection as PgConnection
from psycopg2.extensions import cursor as PgCursor
from psycopg2.extras import RealDictCursor

from .config import Settings, load_settings
from .errors import PoolExhausted

log = logging.getLogger(__name__)


class Database:
    """A thread-safe psycopg2 connection pool with transaction helpers."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 1,
        pool_max: int = 10,
        app_name: str = "rr-backend",
        acquire_timeout: float = 10.0,
        options: str = "",
    ) -> None:
        self._acquire_timeout = acquire_timeout
        # Admission control. Without it, caller number `pool_max + 1` gets a
        # PoolError from psycopg2 instead of waiting its turn.
        self._slots = threading.Semaphore(pool_max)
        self._pool = pg_pool.ThreadedConnectionPool(
            pool_min,
            pool_max,
            dsn=dsn,
            cursor_factory=RealDictCursor,
            application_name=app_name,
            # Pin the session time zone so that `timestamptz` rendering does not
            # depend on the server's locale or the client's host. `options` lets
            # a caller append further per-session GUCs (see deadlock_timeout in
            # tests/conftest.py).
            options=f"-c timezone=UTC {options}".strip(),
        )
        self._closed = False

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Database":
        settings = settings or load_settings()
        return cls(
            settings.dsn,
            pool_min=settings.pool_min,
            pool_max=settings.pool_max,
            app_name=settings.app_name,
            acquire_timeout=settings.pool_acquire_timeout,
        )

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if not self._closed:
            self._pool.closeall()
            self._closed = True

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- acquisition --------------------------------------------------------

    @contextlib.contextmanager
    def connection(self) -> Iterator[PgConnection]:
        """Borrow a connection and guarantee its return.

        Blocks until a connection is free rather than failing outright. Any
        in-flight transaction is rolled back before the connection goes back, so
        the next borrower never inherits a dirty session.

        Raises:
            PoolExhausted: no slot freed within `acquire_timeout`.
        """
        if not self._slots.acquire(timeout=self._acquire_timeout):
            raise PoolExhausted(
                f"no connection available within {self._acquire_timeout}s "
                f"(pool_max={self.pool_max}); the pool is saturated"
            )

        conn = None
        broken = False
        try:
            conn = self._pool.getconn()
            yield conn
        except psycopg2.Error:
            # OperationalError et al. can leave the socket unusable.
            broken = conn is not None and conn.closed != 0
            raise
        finally:
            if conn is not None:
                try:
                    if not broken and conn.closed == 0:
                        conn.rollback()
                except psycopg2.Error:
                    broken = True
                    log.warning(
                        "rollback failed while returning connection", exc_info=True
                    )
                self._pool.putconn(conn, close=broken or conn.closed != 0)
            self._slots.release()

    @contextlib.contextmanager
    def transaction(self, *, readonly: bool = False) -> Iterator[PgCursor]:
        """Run a block in one transaction: commit on success, roll back on error.

        `readonly=True` issues SET TRANSACTION READ ONLY, which scopes itself to
        this transaction — unlike `set_session`, it cannot leak into whatever the
        next borrower of this pooled connection does.
        """
        with self.connection() as conn:
            try:
                with conn.cursor() as cur:
                    if readonly:
                        cur.execute("SET TRANSACTION READ ONLY")
                    yield cur
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # -- introspection (used by the tests and the demo) ---------------------

    @property
    def pool_max(self) -> int:
        return self._pool.maxconn