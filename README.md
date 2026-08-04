# Restaurant Reservation & Review System — Backend

Database Fundamentals lab. PostgreSQL for the transactional core, Redis for
availability caching, MongoDB for long-form review prose.

**Status:** Days 1–2 complete (schema + transactional booking). Days 3–4 pending.

---

## Quick start

Requires Docker.

```bash
./scripts/db.sh up      # start postgres + redis + mongo
./scripts/db.sh reset   # create schema, then load seed data
./scripts/db.sh psql    # interactive shell
```

Verify the schema's invariants (16 checks, runs in a rolled-back transaction):

```bash
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U rr_app -d restaurant_reservations -f /sql/99_checks.sql
```

### Connection details

| Service | Host port | Credentials |
|---|---|---|
| PostgreSQL 16 + PostGIS 3.4 | `5433` | `rr_app` / `rr_password`, db `restaurant_reservations` |
| Redis 7 | `6379` | none |
| MongoDB 7 | `27017` | `rr_app` / `rr_password`, db `restaurant_feedback` |

Postgres is on **5433**, not 5432, to avoid clashing with a local install.

```
postgresql://rr_app:rr_password@localhost:5433/restaurant_reservations
```

> **Apple Silicon note.** `postgis/postgis` publishes amd64 only, so on arm64 it
> runs under emulation. Correctness is unaffected, but the wall-clock numbers
> `EXPLAIN ANALYZE` reports on Day 4 will be inflated and noisier than native.
> For timing-sensitive work, switch the image to `imresamu/postgis:16-3.5` (the
> community multi-arch build of the same thing) and `./scripts/db.sh nuke && up
> && reset`.

### `scripts/db.sh`

| Command | Effect |
|---|---|
| `up` | start containers, wait for health |
| `apply` | run `sql/01`–`05` (schema, triggers, indexes, views) |
| `seed` | run `sql/06_seed.sql` |
| `reset` | `apply` + `seed` |
| `psql` | interactive shell; extra args pass through |
| `down` / `nuke` | stop containers / stop and delete volumes |

`psql` runs inside the container, so no local client is needed and client/server
versions cannot drift.

---

## Layout

```
docker-compose.yml          postgres + redis + mongo
scripts/db.sh               stack and migration wrapper
scripts/demo_booking.py     end-to-end walkthrough of the booking layer
docs/er-diagram.md          ER diagram + normalization argument
sql/
  01_extensions.sql         postgis, btree_gist, citext, pg_trgm
  02_tables.sql             3NF schema, constraints, exclusion constraint
  03_triggers.sql           cross-table invariants, updated_at
  04_indexes.sql            access paths, each justified by a query
  05_views.sql              read models for the analytics
  06_seed.sql               reproducible demo dataset (setseed)
  99_checks.sql             self-verifying invariant suite
app/
  config.py                 env-driven settings
  db.py                     connection pool + transaction scoping
  errors.py                 domain exceptions
  models.py                 row -> dataclass mapping
  availability.py           the availability CTE
  reservations.py           book / cancel / get / list
tests/                      38 tests incl. deterministic race proofs
```

## Python layer (Day 2)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest        # 38 tests
.venv/bin/python scripts/demo_booking.py
```

```python
from app import Database, book_reservation

with Database.from_settings() as db:
    res = book_reservation(
        db, restaurant_id=1, customer_id=7,
        starts_at=datetime(2027, 6, 15, 19, 0, tzinfo=timezone.utc),
        party_size=4, zone="patio",
    )
```

### Booking is one statement

The candidate search is folded into the `INSERT`, so selection and insertion
cannot be separated by a concurrent commit:

```sql
WITH req AS (...), free AS (...),
candidate AS (SELECT ... FROM free ORDER BY seats, random() LIMIT 1)
INSERT INTO reservations (...) SELECT ... FROM candidate RETURNING ...
```

Correctness comes from the exclusion constraint, not from this SQL — the corrupt
outcome is unreachable however the client is written. What the single statement
buys is behaviour under contention: one round-trip, no `FOR UPDATE` or
SERIALIZABLE, and a `23P01` that the retry loop converts into a booking on a
*different* table rather than an error the user sees. `max_attempts` therefore
bounds absorbable contention, not latency.

Errors are domain-typed, so callers never inspect a SQLSTATE — and the
distinctions are operational, not cosmetic: `NoTableAvailable` means don't
retry, `BookingConflict` means do.

**Contention does not always arrive as `23P01`.** An `EXCLUDE` constraint inserts
its index tuple *before* scanning for conflicts, so two bookers racing for the
same table have each already written the entry the other must wait on. They wait
on each other, and Postgres breaks the cycle with a deadlock — `40P01` — rather
than cleanly failing one side. The victim's booking is valid and simply needs
reattempting, so `40P01` is retried alongside `23P01`. This surfaced as an
intermittent test failure; `tests/test_race_deterministic.py` now stages it
deterministically.

That correctness fix leaves a cost worth knowing about: a deadlock is only
discovered after `deadlock_timeout` (1s by default), whereas a `23P01` returns
immediately. So retries are ~1000× more expensive when they arrive as deadlocks,
and heavy contention *for the same table* degrades sharply — enough that one
unlucky test run spent 14 minutes retrying and starved its own connection pool.
Ordinary traffic does not look like this (the `random()` tiebreak spreads bookers
across free tables), but if same-slot contention were ever real, the fix is a
`pg_advisory_xact_lock` keyed on the contended slot, which serializes those
bookings and removes the mutual-conflict window entirely. Not implemented —
optimistic retry is the right default — but it is the escalation path.

### Pooling

`Database` wraps `ThreadedConnectionPool` and closes a sharp edge in it:
`getconn()` raises `PoolError` as soon as `maxconn` connections are checked out
instead of waiting for one. A semaphore gates admission so surplus callers block
until a slot frees, and `PoolExhausted` is raised only after
`POOL_ACQUIRE_TIMEOUT`. Connections are returned on every path, and broken ones
are discarded rather than recycled.

---

## Schema at a glance

Eight relations: `cities`, `restaurants`, `restaurant_tables`, `cuisines`,
`restaurant_cuisines`, `customers`, `reservations`, `reviews`.

The design rationale is in [`docs/er-diagram.md`](docs/er-diagram.md). Three
decisions carry most of the weight:

1. **`reservations.during` is a single `tstzrange`, not a start/end pair.** That
   turns "no double-booking" into a declarative constraint:

   ```sql
   EXCLUDE USING gist (table_id WITH =, during WITH &&)
       WHERE (status IN ('pending','confirmed','seated','completed'))
   ```

   The check-then-insert race in application code disappears — concurrent
   conflicting bookings get `SQLSTATE 23P01` rather than both succeeding. Bounds
   are forced half-open so consecutive bookings may abut.

2. **No transitive foreign keys.** `reservations` carries no `restaurant_id`
   (determined by `table_id`); `reviews` carries neither `customer_id` nor
   `restaurant_id` (both determined by `reservation_id`). This removes whole
   classes of contradictory row rather than merely saving bytes. Views pay the
   join cost once.

3. **JSONB where the data is genuinely semi-structured.** `menu` and
   `opening_hours` are documents with structural CHECKs and GIN indexes;
   everything with relational shape stays relational.

### Seed dataset

`setseed(0.42)` makes it reproducible.

| Relation | Rows |
|---|---|
| cities | 4 |
| restaurants | 8 |
| restaurant_tables | 96 |
| customers | 60 |
| reservations | ~9,500 |
| reviews | ~2,650 |

Spans 45 days past to 14 days future across three slots per table per day, so
`EXPLAIN ANALYZE` has real data to work with. Ratings are skewed per restaurant
(averages 3.10–4.45) so the per-city `RANK()` yields a strict ordering.

---

## Roadmap

- [x] **Day 1** — schema design + database setup
- [x] **Day 2** — psycopg2 connection pooling; atomic `book_reservation()`
      handling `23P01` as "slot taken"; parameterized queries throughout
- [ ] **Day 3** — Redis availability cache with invalidation on write; MongoDB
      feedback documents linked by `reviews.body_ref`
- [ ] **Day 4** — per-city `RANK()`, availability CTE, `EXPLAIN ANALYZE` tuning,
      GiST location search

Open gaps from Day 1 are listed in
[`docs/er-diagram.md` §6](docs/er-diagram.md#6-known-gaps) — most notably that
restaurant-local time zones are unmodelled and `opening_hours` is not yet
enforced against booking times.