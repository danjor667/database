# Restaurant Reservation & Review System — Backend

Database Fundamentals lab. PostgreSQL for the transactional core, Redis for
availability caching, MongoDB for long-form review prose.

**Status:** complete. Schema, transactional booking, Redis + MongoDB, and the
analytical queries with measured tuning.

---

## Quick start

Requires Docker.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

./scripts/db.sh up                                # postgres + redis + mongo
./scripts/db.sh reset                             # schema, then seed data
.venv/bin/python scripts/backfill_feedback.py     # give the reviews their prose
```

**The backfill is part of setup, not an extra.** `06_seed.sql` fabricates
`reviews.body_ref` with `substr(md5(...), 1, 24)` — valid ObjectId *format*
pointing at nothing — so a database that has only been `reset` carries ~2,650
dangling cross-store references, and `reconcile()` will report every one. It is a
separate step because `db.sh` is pure Docker and psql, with no dependency on
Python or on MongoDB being reachable.

Then:

```bash
.venv/bin/python -m pytest                        # 89 tests
./scripts/db.sh psql                              # interactive shell
```

Verify the schema's invariants (16 checks, runs in a rolled-back transaction):

```bash
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U rr_app -d restaurant_reservations -f /sql/99_checks.sql
```

### Connection details

| Service | Host port | Credentials |
|---|---|---|
| PostgreSQL 16 + PostGIS 3.5 | `5433` | `rr_app` / `rr_password`, db `restaurant_reservations` |
| Redis 7 | `6379` | none |
| MongoDB 7 | `27017` | `rr_app` / `rr_password`, db `restaurant_feedback` |

Postgres is on **5433**, not 5432, to avoid clashing with a local install.

```
postgresql://rr_app:rr_password@localhost:5433/restaurant_reservations
```

> **Apple Silicon note.** The compose file already pins
> `imresamu/postgis:16-3.5`, the community multi-arch build, for this reason:
> the upstream `postgis/postgis` publishes amd64 only, so on arm64 it runs under
> emulation. Correctness is unaffected either way, but the wall-clock numbers
> `EXPLAIN ANALYZE` reports on Day 4 would be inflated and noisier than native.
> If you switch back to the upstream image, expect exactly that.

### `scripts/db.sh`

| Command | Effect |
|---|---|
| `up` | start containers, wait for health |
| `apply` | run `sql/01`–`05` + `07` (schema, triggers, indexes, views) |
| `seed` | run `sql/06_seed.sql` |
| `reset` | `apply` + `seed` |
| `psql` | interactive shell; extra args pass through |
| `redis` / `mongo` | `redis-cli` and `mongosh` shells |
| `down` / `nuke` | stop containers / stop and delete volumes |

`psql` runs inside the container, so no local client is needed and client/server
versions cannot drift.

---

## Layout

```
docker-compose.yml          postgres + redis + mongo
scripts/db.sh               stack and migration wrapper
scripts/demo_booking.py     end-to-end walkthrough of the booking layer
scripts/demo_day3.py        walkthrough of the cache + feedback store
scripts/demo_analytics.py   walkthrough of the Day 4 analytical queries
scripts/explain.py          EXPLAIN ANALYZE harness + index crossover study
scripts/backfill_feedback.py  gives the seeded reviews real Mongo documents
docs/er-diagram.md          ER diagram + normalization argument
docs/code-walkthrough.md    file-by-file explanation of the whole repo
docs/performance.md         measured query timings and what changed
sql/
  01_extensions.sql         postgis, btree_gist, citext, pg_trgm
  02_tables.sql             3NF schema, constraints, exclusion constraint
  03_triggers.sql           cross-table invariants, updated_at
  04_indexes.sql            access paths, each justified by a query
  05_views.sql              read models for the analytics
  06_seed.sql               reproducible demo dataset (setseed)
  07_analytics.sql          window-function read models (rankings, demand)
  99_checks.sql             self-verifying invariant suite
app/
  config.py                 env-driven settings
  db.py                     connection pool + transaction scoping
  errors.py                 domain exceptions
  models.py                 row -> dataclass mapping
  availability.py           the availability CTE
  reservations.py           book / cancel / get / list
  cache.py                  Redis availability cache (Day 3)
  feedback.py               MongoDB feedback store (Day 3)
  analytics.py              rankings, location search (Day 4)
tests/                      89 tests incl. deterministic race proofs
```

## Python layer (Day 2)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest        # 89 tests
.venv/bin/python scripts/demo_booking.py
```

The Redis and MongoDB tests skip themselves if those services are down, so the
Day 1–2 suite still runs against Postgres alone.

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

## Redis + MongoDB (Day 3)

```bash
.venv/bin/python scripts/backfill_feedback.py   # seeded reviews -> Mongo documents
.venv/bin/python scripts/demo_day3.py           # cache + feedback walkthrough
```

Both are opt-in at the call site. Nothing in the Day 1–2 core imports `redis` or
`pymongo`, so the booking layer still runs with only psycopg2 installed:

```python
from app import Database, book_reservation, find_available_tables
from app.cache import AvailabilityCache
from app.feedback import FeedbackStore, add_review

with Database.from_settings() as db, AvailabilityCache.from_settings() as cache:
    tables = find_available_tables(db, restaurant_id=1, starts_at=slot,
                                   party_size=4, cache=cache)   # read-through
    book_reservation(db, restaurant_id=1, customer_id=7, starts_at=slot,
                     party_size=4, cache=cache)                 # invalidates
```

### The cache changes latency, not semantics

Availability was *already* advisory — another transaction may take the table
between the listing and the booking, which is why `book_reservation` re-derives
candidates inside its `INSERT` and lets the exclusion constraint arbitrate. A
cached answer is therefore no less trustworthy than a fresh one, and
`book_reservation` never reads the cache at all. A cache that could veto a
booking would be a cache that can corrupt the schedule;
`test_booking_never_reads_the_cache` poisons Redis with "nothing is free" and
asserts the booking still succeeds.

### Invalidation is a generation counter

The obvious design — `DEL avail:*:<restaurant>:*` on every write — needs a `SCAN`
and a variable number of deletes. Instead each restaurant carries a counter that
is baked into every entry key:

```
avail:v1:gen:<restaurant_id>          ->  integer, INCR on every write
avail:v1:<restaurant_id>:g<N>:<slot>:<party>:<zone>:<duration>  ->  the listing
```

One `INCR` makes an entire generation unreachable in O(1). Superseded entries are
never deleted; they expire. Two ordering rules make it correct, and both are easy
to get backwards:

1. **Read the generation before querying Postgres.** Reading it afterwards lets a
   write that lands mid-query bump the counter first, so the stale rows get
   stored under the *new* generation — exactly where the next reader looks.
2. **Invalidate strictly after the transaction commits.** Bumping early lets a
   concurrent reader pick up the new generation, query Postgres, still see
   pre-commit state, and cache it under a generation nothing will supersede.

`CACHE_TTL_SECONDS` (default 60) is not the invalidation mechanism — it is the
backstop for writes that never go through this package at all, like a `psql`
session or a cron job.

An outage degrades to a miss. Redis holds a rebuildable copy of something
Postgres already knows, so `redis.RedisError` is caught, counted, and reported as
"not cached"; socket timeouts are short, because a cache that blocks longer than
the query it replaces is worse than no cache.

### MongoDB holds the prose, Postgres holds the numbers

Ratings stay in `reviews` because `v_restaurant_ratings` aggregates over them.
Long-form text goes to `restaurant_feedback.reviews`, and `reviews.body_ref` is
the only link. The document denormalizes `restaurant_id` — which
`docs/er-diagram.md` argues *against* doing inside Postgres — because the
reasoning differs across stores: within one relational store a derived column can
contradict its source with no constraint to catch it, whereas MongoDB cannot
enforce a foreign key into Postgres under any schema, so nothing is lost that was
being relied on. Postgres wins every disagreement.

A review is written across two stores that share no transaction, so the order is
forced by what each failure leaves behind:

1. Postgres: upsert the ratings row (`body_ref` still NULL)
2. MongoDB: upsert the document, keyed on `reservation_id`
3. Postgres: set `body_ref`

**The pointer is written last because the pointer is the commitment.** A crash
between 1 and 2 leaves ratings without prose — a legal state. Between 2 and 3 it
leaves an unlinked document, which `reconcile()` detects and re-links, and which
re-running `add_review` heals on its own since both upserts are idempotent. The
reverse order would produce a dangling pointer, where a reader cannot tell "not
written yet" from "lost".

Unlike the cache, this layer does **not** degrade silently: MongoDB holds the
only copy of the prose, so `pymongo` errors propagate.

### `reconcile()` stands in for the missing constraint

No database can enforce a foreign key into MongoDB, so the invariant has to be
checked by running something. It reports three kinds of divergence — `dangling`
(Postgres points at a missing document), `unlinked` (document exists, pointer is
NULL), and `orphaned` (document whose review row is gone). Orphans are
**reported, never deleted**: automatically destroying a diner's prose because a
pointer went missing is not a repair, and it is not reversible.

Pass `reservation_ids=` to scope it. Unscoped, `repair=True` is a whole-database
write that clears every `body_ref` it cannot resolve — correct only when `store`
is genuinely the collection those reviews point into.

> `sql/06_seed.sql` fabricates `body_ref` with `substr(md5(...), 1, 24)` — valid
> ObjectId *format* pointing at nothing, so a fresh seed leaves ~2,650 dangling
> references. `scripts/backfill_feedback.py` writes the documents and repoints
> them; `reconcile` reports zero afterwards.

---

## Analytics and tuning (Day 4)

```bash
.venv/bin/python scripts/demo_analytics.py      # the queries
.venv/bin/python scripts/explain.py -n 15 --scale   # the measurements
```

Full write-up with numbers: [`docs/performance.md`](docs/performance.md).

### Per-city ranking

`v_restaurant_rankings` ranks restaurants within their city. `PARTITION BY` is
what makes `max_rank=3` mean *each city's* top three — an `ORDER BY … LIMIT 3`
would return three rows overall.

```python
from app.analytics import rank_restaurants_by_city
rank_restaurants_by_city(db, max_rank=1)   # the best restaurant in every city
```

`RANK`, `DENSE_RANK` and `ROW_NUMBER` are all exposed because they answer
different questions, and they differ only on ties: RANK gives tied rows the same
number and then skips, DENSE_RANK does not skip, ROW_NUMBER forces a distinct
sequence. No two restaurants in a city share a rounded rating in the seed, so
they coincide there — but `v_slot_demand` does contain real ties, and the tests
assert the behaviour on both.

Restaurants with no reviews survive with `review_count = 0` and sort last:
`NULLS LAST` is not the default for `DESC`, and without it every unrated
restaurant would head its city.

### Location search

`geography(Point, 4326)` means distances are metres on a spheroid. Two forms,
one indexable:

```sql
ST_DWithin(location, point, 5000)            -- 0.09 ms, GiST index scan
ST_Distance(location, point) <= 5000         -- 64 ms, sequential scan
```

Identical answers, ~700× apart at 50k rows. The planner cannot bound a
function's output with a GiST index, so the second form measures every row. For
"nearest N", `ORDER BY location <-> point LIMIT 5` walks the index in distance
order and stops early.

### What the measurements changed

- **The availability query needed no new index.** It is already served by the
  GiST index Postgres builds to enforce the exclusion constraint — 16–18× faster
  and 7× fewer buffers than a forced sequential scan. One structure, two jobs.
- **The ratings rollup was rewritten and a bug fell out.** Aggregating reviews
  first instead of joining the whole chain and grouping at the end is 2.1×
  faster — and fixed `last_reviewed_visit`, which had been reporting the last
  *reservation* (often a future booking) because un-reviewed rows still fed the
  `max()`.
- **Planning costs more than execution on the hot path** (0.218 ms vs 0.082 ms).
  Recorded, not yet acted on; prepared statements are the fix but a generic plan
  could regress this query, so it needs measuring first.
- **Most indexes here do not pay at seed scale, and that is correct.** Eight
  restaurants fit in two pages. `scripts/explain.py --scale` builds a 50,000-row
  table to show where the crossover actually is.

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
- [x] **Day 3** — Redis availability cache with generation-counter invalidation
      on write; MongoDB feedback documents linked by `reviews.body_ref`, with
      `reconcile()` for the cross-store invariant no constraint can express
- [x] **Day 4** — per-city `RANK()` and window functions over aggregates; GiST
      radius and KNN location search; `EXPLAIN ANALYZE` harness with A/B plan
      forcing and an index-crossover study. Findings in
      [`docs/performance.md`](docs/performance.md)

Open gaps from Day 1 are listed in
[`docs/er-diagram.md` §6](docs/er-diagram.md#6-known-gaps) — most notably that
restaurant-local time zones are unmodelled and `opening_hours` is not yet
enforced against booking times.