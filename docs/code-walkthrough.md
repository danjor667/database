# Code Walkthrough — every file, every decision

A teaching companion to the codebase. `README.md` tells you how to *run* it;
`docs/er-diagram.md` argues the *schema design*. This file walks the whole
repository top to bottom and explains what each construct does, why it is there,
and which alternative it was chosen over.

Read it in order the first time — later sections assume the vocabulary built in
earlier ones.

**Contents**

1. [The one idea the whole system rests on](#1-the-one-idea-the-whole-system-rests-on)
2. [Infrastructure](#2-infrastructure)
3. [The SQL layer, file by file](#3-the-sql-layer-file-by-file)
4. [The Python layer, file by file](#4-the-python-layer-file-by-file)
5. [The tests](#5-the-tests)
6. [The demo and utility scripts](#6-the-demo-and-utility-scripts)
7. [Postgres feature glossary](#7-postgres-feature-glossary)
8. [How a single booking flows end to end](#8-how-a-single-booking-flows-end-to-end)
9. [Known gaps and doc drift](#9-known-gaps-and-doc-drift)

---

## 1. The one idea the whole system rests on

Everything else is detail around this.

**The problem.** Booking a restaurant table is a check-then-act operation:

```
1. SELECT a table that is free at 19:00
2. INSERT a reservation for that table at 19:00
```

Between step 1 and step 2 there is a gap. Under PostgreSQL's default isolation
level (`READ COMMITTED`), a plain `SELECT` takes no lock that would make another
writer wait. So two customers booking simultaneously can *both* run step 1, both
see "table 4 is free", and both run step 2. Neither transaction sees anything
wrong. The result is two parties arriving for the same table — a corrupted
schedule produced by two individually-correct-looking transactions.

This is the classic **race condition**, and no amount of careful Python fixes it,
because the flaw is in the *shape* of the operation, not the code quality.

**The fix used here.** Make the bad state unrepresentable in the database
itself:

```sql
CONSTRAINT reservations_no_double_booking
    EXCLUDE USING gist (table_id WITH =, during WITH &&)
    WHERE (status IN ('pending', 'confirmed', 'seated', 'completed'))
```

Read it as: *no two rows may exist where the `table_id`s are equal (`=`) **and**
the time ranges overlap (`&&`), considering only rows whose status still holds
the table.*

This is enforced by the storage engine at insert time. The second inserter
receives `SQLSTATE 23P01` (`exclusion_violation`) and its transaction is rolled
back. The corrupt outcome is now **unreachable**, no matter how the application
is written — including if someone connects with `psql` and types the INSERT by
hand.

**Why this matters for the rest of the code.** Because correctness is
guaranteed below the application, the Python layer is freed up to optimize for
something else entirely: *behaviour under contention*. Losing a race is no
longer an error to report — it becomes a signal to retry on a different table.
That single reframing explains the design of `app/reservations.py`.

Three consequences to hold onto:

| Consequence | Where you see it |
|---|---|
| Ranges must be **half-open** `[start, end)` so an 18:00–19:30 booking doesn't "overlap" a 19:30 one | `reservations_during_half_open` CHECK; checks 3–4 in `99_checks.sql` |
| Which statuses *hold* a table is a business rule, written into the constraint's `WHERE` | cancelling frees the slot without deleting history |
| A GiST index is created automatically to enforce it — and it doubles as the availability index | `04_indexes.sql` header comment |

---

## 2. Infrastructure

### `docker-compose.yml` — three data stores

Defines three services, all with healthchecks so `scripts/db.sh` can wait for
readiness rather than sleeping a fixed interval.

**`postgres`** — image `imresamu/postgis:16-3.5`. PostGIS is PostgreSQL plus
geospatial types. The image choice is deliberate and commented: the *official*
`postgis/postgis` image publishes amd64 binaries only, so on an Apple Silicon
Mac it runs under emulation. Correctness is unaffected but `EXPLAIN ANALYZE`
timings become inflated and noisy — useless for the Day 4 tuning work.
`imresamu/postgis` is a community multi-arch build of the same thing.

```yaml
ports:
  - "5433:5432"
```

Host port **5433**, container port 5432. A developer machine very often already
has a local Postgres on 5432; publishing on 5433 avoids the clash. This is why
the DSN everywhere says `localhost:5433`.

```yaml
TZ: UTC
PGTZ: UTC
```

Pins the container clock to UTC. The seed data generates timestamps relative to
`current_date`, so a fixed timezone makes reloads reproducible and keeps
`EXPLAIN` runs comparable.

```yaml
volumes:
  - pg_data:/var/lib/postgresql/data
  - ./sql:/sql:ro
```

Two very different mounts. `pg_data` is a named volume holding the actual
database files (survives `down`, deleted by `nuke`). `./sql` is bind-mounted
**read-only** at `/sql` so that `psql` running *inside* the container can read
your migration files. Note it is mounted, **not** placed in
`/docker-entrypoint-initdb.d/` — that directory auto-runs scripts exactly once on
first boot. Mounting instead lets `scripts/db.sh` re-apply the schema as often
as you like.

**`redis`** — `--appendonly yes --save ""`. Turns on the append-only log and
turns off RDB snapshotting. Backs the availability cache (`app/cache.py`).
Persistence is arguably wasted here: the cache is entirely rebuildable from
Postgres, so `--appendonly no` would be the more honest configuration.

**`mongo`** — root credentials plus `MONGO_INITDB_DATABASE`. Holds the long-form
review prose (`app/feedback.py`). Note `authSource=admin` in the DSN — required
because the root user is defined in the `admin` database, not in
`restaurant_feedback`; omit it and authentication fails with a confusing error.

> **Credentials note.** `rr_password` appears here, in `.env.example`, and as a
> default in `app/config.py`. It is a local-dev placeholder, deliberately
> consistent across all three so a fresh clone runs with zero configuration. It
> is committed to git — fine for a lab, never acceptable for anything real.

### `scripts/db.sh` — the stack + migration wrapper

A Bash script, ~76 lines, that hides the Docker verbosity.

```bash
set -euo pipefail
```

The standard safety preamble: `-e` exit on any command failure, `-u` error on
unset variables, `-o pipefail` make a pipeline fail if *any* stage fails (not
just the last). Without `pipefail`, `false | true` succeeds.

```bash
cd "$(dirname "${BASH_SOURCE[0]}")/.."
```

Makes the script location-independent — it `cd`s to the project root regardless
of where you invoke it from, so `docker compose` finds the compose file.

```bash
run_sql() {
    compose exec -T postgres \
        psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -f "/sql/$1.sql"
}
```

Three things worth learning here:

- `exec -T` runs a command in the *already running* container with no TTY
  allocation (needed for non-interactive use).
- `psql` runs **inside** the container, so you need no local Postgres client and
  client/server versions can never drift.
- `-v ON_ERROR_STOP=1` is critical. By default `psql` continues after an error,
  which would let a broken migration report success. This makes the first error
  fatal.
- `/sql/$1.sql` is the *container* path — the bind mount from compose.

`cmd_up` polls `pg_isready` up to 40 times at 1-second intervals, printing dots,
and dumps the last 40 log lines on timeout. `require_up` guards `apply`/`seed`
with a friendly message instead of a cryptic connection error.

The `*)` fallback case is a neat trick — it prints the script's own header
comment as usage text:

```bash
sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
```

Lines 2–20 of itself, with the leading `# ` stripped. The docs can't drift from
the help output because they *are* the help output.

### `.env.example`

A template you copy to `.env`. Note the header comment tells you how to load it:

```bash
set -a; source .env; set +a
```

`set -a` makes every subsequent assignment automatically exported to the
environment, which is what `os.environ` in Python reads. Without it, `source`
would create shell variables invisible to child processes.

Every variable is documented inline with *why it exists*, not just what it is —
e.g. `POOL_MAX` "should exceed the number of worker threads", and
`BOOKING_MAX_ATTEMPTS` points you at `app/reservations.py` for the reasoning.

### `.gitignore`

Seven lines: `.venv/`, `venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`,
`.env`, `.idea/`. The important one is `.env` — the *example* is committed, the
real file with real values is not. `*.py[cod]` is a glob matching `.pyc`,
`.pyo`, and `.pyd`.

### `pytest.ini`

```ini
testpaths = tests
pythonpath = .
addopts = -ra
filterwarnings = error
markers =
    slow: exercises contention with many threads
```

- `pythonpath = .` puts the project root on `sys.path` so `import app` resolves.
  The comment explains the subtlety: `python -m pytest` adds the cwd
  automatically, but the bare `pytest` console script does not.
- `addopts = -ra` shows a summary of all non-passing tests at the end.
- `filterwarnings = error` promotes every warning to a test failure. Aggressive,
  and correct for a project this size — it means a deprecation can't rot
  silently.
- The `slow` marker is declared but not currently applied to any test.

### `requirements.txt`

```
psycopg2-binary>=2.9.9,<3
pytest>=8.0,<9
```

Two dependencies. `psycopg2-binary` ships precompiled wheels (no local
`libpq`/compiler needed) — right for a lab, though production deployments
usually prefer source `psycopg2` to link against the system libpq. Both pins
cap the major version, so a breaking 3.x release can't silently arrive.

Note what is *absent*: no ORM. Every query in this project is hand-written SQL.
That is the point of the exercise.

---

## 3. The SQL layer, file by file

Files are numbered by execution order. `scripts/db.sh apply` runs 01–05;
`seed` runs 06; `99_checks.sql` is run manually.

### `01_extensions.sql` — four extensions

Extensions add types, functions, and index methods to a database. All four are
load-bearing:

| Extension | Why it is required |
|---|---|
| `postgis` | provides `geography(Point, 4326)`, `ST_MakePoint`, `ST_DWithin` — metre-accurate distance on a spheroid |
| `btree_gist` | **required by the exclusion constraint** |
| `citext` | case-insensitive text, used for emails and cuisine names |
| `pg_trgm` | trigram index support for `ILIKE '%…%'` fuzzy name search |

`btree_gist` deserves explanation because it is non-obvious. A GiST index
natively supports "overlaps" (`&&`) on ranges, but has **no operator class for
plain equality on a scalar** like `integer`. The exclusion constraint needs
*both* — `table_id WITH =` and `during WITH &&` — in one index. `btree_gist`
supplies the missing equality operator class. Without it, `02_tables.sql` fails.

`CREATE EXTENSION IF NOT EXISTS` makes the file idempotent (safe to re-run).

### `02_tables.sql` — the 3NF schema

Opens with `DROP TABLE ... CASCADE` for all eight tables in reverse-dependency
order, then drops the two enum types. This is what makes `apply` re-runnable:
the file rebuilds the schema from scratch every time. `CASCADE` also drops
dependent objects (views, constraints).

#### Enumerated types

```sql
CREATE TYPE table_zone AS ENUM ('indoor', 'patio', 'bar', 'private');

CREATE TYPE reservation_status AS ENUM (
    'pending', 'confirmed', 'seated', 'completed', 'cancelled', 'no_show'
);
```

An enum is a custom type restricted to a fixed value list. Compared to a lookup
table it costs no join and cannot hold an invalid value; compared to a `text`
column with a CHECK it is more compact and self-documenting. The file states the
rule for choosing: use an enum when the vocabulary is *small, stable, and
application-owned*; use a lookup table the moment a value needs user-facing
metadata (a description, a display order).

The status lifecycle is documented as
`pending → confirmed → seated → completed`, with `cancelled` / `no_show` as
terminal exits. **Only the first four hold the table** — that list is repeated
in the exclusion constraint, the availability query, and the partial index.

#### `cities`

```sql
city_id      integer  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
name         text     NOT NULL,
region       text     NOT NULL,
country_code char(2)  NOT NULL,

CONSTRAINT cities_natural_key    UNIQUE (name, region, country_code),
CONSTRAINT cities_country_iso    CHECK (country_code ~ '^[A-Z]{2}$')
```

`GENERATED ALWAYS AS IDENTITY` is the SQL-standard replacement for `serial`.
Unlike `serial` it forbids manual inserts into the column, which prevents an
application from accidentally colliding with the sequence.

The table exists for a **3NF** reason: `region` and `country_code` depend on the
*city*, not on the restaurant. Storing them on `restaurants` would repeat "NY,
US" for every New York restaurant — a transitive dependency. It also fixes a
practical analytics problem stated in the comment: grouping on a free-text city
name means `'NYC'` and `'New York'` become different groups.

Note the pattern used throughout: a **surrogate key** (`city_id`) for joins,
plus a **natural key** UNIQUE constraint for correctness. You get narrow foreign
keys *and* the database refuses duplicate cities.

`CHECK (btrim(name) <> '')` blocks whitespace-only strings — `NOT NULL` alone
would happily accept `'   '`. `~` is the regex-match operator.

#### `cuisines` / `restaurant_cuisines`

A restaurant has many cuisines, a cuisine has many restaurants: a **many-to-many
relationship**, which the relational model resolves with a junction table.

```sql
CREATE TABLE restaurant_cuisines (
    restaurant_id integer NOT NULL,
    cuisine_id    integer NOT NULL REFERENCES cuisines (cuisine_id) ON DELETE RESTRICT,
    is_primary    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (restaurant_id, cuisine_id)
);
```

The alternative — `cuisine_1` / `cuisine_2` columns, or a comma-joined
`'Italian,Vegetarian'` string — violates **1NF** (atomic values, no repeating
groups) and turns "find every Italian restaurant" into a text-scanning problem.

The composite `PRIMARY KEY (restaurant_id, cuisine_id)` also enforces that a
pairing can't be listed twice.

The FK to `restaurants` is added later with `ALTER TABLE`, because
`restaurants` doesn't exist yet at this point in the file — a forward-reference
you have to work around when tables are mutually referential.

```sql
CREATE UNIQUE INDEX restaurant_cuisines_one_primary
    ON restaurant_cuisines (restaurant_id)
    WHERE is_primary;
```

A **partial unique index** — this is an elegant trick worth internalizing.
Indexing `restaurant_id` alone would allow only one cuisine per restaurant. The
`WHERE is_primary` clause restricts the index to rows where the flag is true, so
the uniqueness rule becomes "at most one *primary* cuisine per restaurant" while
leaving non-primary rows unconstrained. Check 14 in `99_checks.sql` proves it.

#### `restaurants`

The widest table. Several decisions to unpack.

```sql
location geography(Point, 4326) NOT NULL
```

A PostGIS type. **SRID 4326** is WGS84 — ordinary GPS latitude/longitude.
`geography` (as opposed to `geometry`) computes distance on a spheroid, so
`ST_DWithin(location, point, 3000)` genuinely means "within 3000 **metres**"
rather than 3000 degrees-of-something.

Latitude and longitude are deliberately **not** stored as separate columns. They
are derivable via `ST_Y(location)` / `ST_X(location)`, and duplicating them
would create a second source of truth that no constraint could keep in sync —
someone updates `lat` and forgets `location`, and now the row lies.

```sql
CONSTRAINT restaurants_location_is_point
    CHECK (GeometryType(location::geometry) = 'POINT')
```

`geography(Point, 4326)` already constrains the type, but this rejects anything
cast in that isn't a single point (a polygon, say). Belt and braces.

```sql
slot_minutes smallint NOT NULL DEFAULT 90,
CONSTRAINT restaurants_slot_minutes_sane
    CHECK (slot_minutes BETWEEN 15 AND 480 AND slot_minutes % 15 = 0)
```

**This is one of the most important columns in the schema.** It is how long one
booking holds a table, and it lives on the *restaurant* rather than being passed
in per-query. That means a caller cannot accidentally book a 90-minute slot at a
restaurant that seats for 120. The availability CTE reads it directly
(`COALESCE(%(duration_minutes)s, r.slot_minutes)`).

The CHECK enforces a 15-minute floor, an 8-hour ceiling, and a whole number of
quarter-hours (`% 15 = 0`).

```sql
menu          jsonb NOT NULL DEFAULT '{"currency": "USD", "sections": []}',
opening_hours jsonb NOT NULL DEFAULT '{}',

CONSTRAINT restaurants_menu_shape
    CHECK (jsonb_typeof(menu) = 'object' AND jsonb_typeof(menu -> 'sections') = 'array'),
CONSTRAINT restaurants_opening_hours_shape
    CHECK (jsonb_typeof(opening_hours) = 'object')
```

The **deliberate exception to normalization**. Both fields are semi-structured,
read as whole documents, vary in shape per restaurant, and are never the subject
of a join. Decomposing the menu into `menu_sections` + `menu_items` +
`item_tags` would add three tables and a multi-way join to answer a question the
app always phrases as "give me this restaurant's menu".

But "JSONB" doesn't mean "unvalidated". The structural CHECKs establish a floor:
`menu` must be an object whose `sections` key is an array. `jsonb_typeof()`
returns the JSON type name as text. `->` extracts a value by key.

The documented shapes (in comments) are:

```
menu:          { "currency": "USD",
                 "sections": [ { "name": "Starters",
                                 "items": [ { "name": …, "price": …, "tags": […] } ] } ] }
opening_hours: { "mon": [], "tue": [ { "open": "17:00", "close": "22:30" } ], … }
```

An empty array means closed that day; **two entries model a split shift** (lunch
and dinner) — see `Vero Nord`'s Saturday in the seed data.

`email citext UNIQUE` — `citext` compares case-insensitively, so
`Book@X.com` and `book@x.com` are the same value and the UNIQUE constraint
catches the duplicate. Without `citext` you'd need `lower()` in every query and
a functional unique index.

#### `restaurant_tables`

The physical inventory being booked.

```sql
table_code     text     NOT NULL,      -- 'T01', 'PATIO-3'
seats          smallint NOT NULL,
min_party_size smallint NOT NULL DEFAULT 1,

CONSTRAINT restaurant_tables_code_key UNIQUE (restaurant_id, table_code),
CONSTRAINT restaurant_tables_party_range
    CHECK (min_party_size >= 1 AND min_party_size <= seats)
```

`min_party_size` is a **floor** on party size, and it exists to stop a party of 2
consuming a 10-seat table. Combined with the availability query's
`ORDER BY seats`, it produces sensible table allocation.

`UNIQUE (restaurant_id, table_code)` — table codes are unique *per restaurant*,
not globally. Two restaurants may both have a `T01`.

The CHECK is a two-column rule, which is fine: **a CHECK constraint may
reference any column of its own row**. It may *not* reference other tables —
that limitation is what forces the triggers in `03_triggers.sql`.

#### `customers`

```sql
CONSTRAINT customers_email_shape
    CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$')
```

Deliberately permissive, and the comment says so: "full RFC 5322 validation
belongs in the app". It catches obvious garbage (no `@`, embedded spaces, no
dot in the domain) without rejecting legitimately weird addresses. Trying to
fully validate email with a regex is a well-known dead end.

#### `reservations` — the core relation

```sql
reservation_id bigint    GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
table_id       integer   NOT NULL REFERENCES restaurant_tables (table_id) ON DELETE RESTRICT,
customer_id    integer   NOT NULL REFERENCES customers (customer_id) ON DELETE RESTRICT,
during         tstzrange NOT NULL,
party_size     smallint  NOT NULL,
status         reservation_status NOT NULL DEFAULT 'confirmed',
```

`bigint` for the PK because reservations are the highest-volume table — the one
place where exhausting a 32-bit sequence is plausible.

**There is no `restaurant_id` column.** This is the textbook 3NF decision. A
reservation's restaurant is functionally determined by its table
(`table_id → restaurant_id`), so storing it would be a transitive dependency. But
the argument in `docs/er-diagram.md` is stronger than tidiness: storing it would
*permit a row claiming restaurant A while pointing at a table in restaurant B*.
Removing the column removes the entire class of bug. Joins traverse
`restaurant_tables` — a single indexed hop — and `v_reservation_details`
flattens it for convenience.

```sql
during tstzrange NOT NULL
```

**One range column, not a `starts_at` / `ends_at` pair.** This is what makes
overlap a *declarative* constraint rather than application logic. A range type
stores both bounds plus their inclusivity, and supports operators like `&&`
(overlaps), `@>` (contains), `-|-` (adjacent). `tstz` = timestamp with time
zone.

```sql
CONSTRAINT reservations_during_half_open
    CHECK (NOT isempty(during)
           AND lower_inc(during) AND NOT upper_inc(during)
           AND lower(during) IS NOT NULL AND upper(during) IS NOT NULL)
```

Forces canonical `[start, end)` bounds — lower inclusive, upper exclusive.
**This constraint is mandatory, not stylistic.** With closed `[]` bounds, an
18:00–19:30 booking and a 19:30–21:00 booking share the instant 19:30, so `&&`
reports an overlap and the second booking is wrongly refused even though the
table is genuinely free. The clauses also reject empty ranges and unbounded
(infinite) ones. Checks 4, 8, and 9 pin all three behaviours.

```sql
CONSTRAINT reservations_duration_sane
    CHECK (upper(during) - lower(during) BETWEEN interval '15 minutes' AND interval '8 hours')
```

A sanity bound. Check 10 proves a 24-hour reservation is refused.

```sql
CONSTRAINT reservations_no_double_booking
    EXCLUDE USING gist (table_id WITH =, during WITH &&)
    WHERE (status IN ('pending', 'confirmed', 'seated', 'completed'))
```

The centrepiece — see [§1](#1-the-one-idea-the-whole-system-rests-on). The
`WHERE` clause makes it a **partial** constraint, and that is what lets a
cancellation free the slot *without deleting the row*: a `cancelled` reservation
simply stops being counted, so history survives and the table becomes bookable
again. `completed` deliberately keeps blocking, so past bookings stay
consistent.

Postgres creates the enforcing GiST index automatically. `04_indexes.sql` notes
that this same index also serves the availability anti-join — you get two uses
from one structure.

#### `reviews`

```sql
reservation_id bigint NOT NULL UNIQUE REFERENCES reservations (reservation_id) ON DELETE CASCADE,
rating         smallint NOT NULL,
body_ref       text,

CONSTRAINT reviews_rating_range      CHECK (rating BETWEEN 1 AND 5),
CONSTRAINT reviews_body_ref_objectid CHECK (body_ref ~ '^[0-9a-f]{24}$')
```

A **verified-diner model**: a review hangs off the reservation that earned it.
`UNIQUE` on `reservation_id` makes it one review per visit (check 13 proves it),
and gives the relationship a 1:0..1 cardinality.

Again **no `customer_id` and no `restaurant_id`** — both are reachable through
`reservation_id`. Duplicating them would permit a review whose restaurant
contradicts the visit it claims to describe.

`body_ref` is the **cross-store pointer**: the numeric ratings stay in Postgres
because the analytics aggregate over them, while the long-form prose lives in
MongoDB. The CHECK validates the 24-hex-character ObjectId *format*. Note what
it cannot do — no database can enforce a foreign key into MongoDB, so keeping
the two stores consistent is application work (Day 3).

`ON DELETE CASCADE` here vs `ON DELETE RESTRICT` on `reservations.customer_id` is
a considered difference: deleting a reservation should take its review with it,
but deleting a customer who has reservations should be refused outright.

### `03_triggers.sql` — rules a CHECK cannot express

The opening comment states the constraint that motivates the whole file: **a
CHECK may only reference columns of its own row.** Any rule spanning two tables
must be a trigger.

#### `touch_updated_at()`

```sql
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
```

The canonical audit-timestamp trigger, attached `BEFORE UPDATE ... FOR EACH ROW`
to `restaurants`, `customers`, `reservations`, and `reviews`.

Key concepts: `NEW` is the row as it will be written; a `BEFORE` trigger may
modify it by assigning to `NEW.<column>` and returning it. (`RETURN NULL` from a
BEFORE trigger would silently cancel the operation.) `$$ … $$` is dollar-quoting,
which avoids escaping quotes inside the function body. `plpgsql` is Postgres's
procedural language.

Doing this in a trigger rather than in application code means `updated_at` is
correct even for a manual `psql` update.

#### `reservations_validate()`

```sql
CREATE OR REPLACE TRIGGER reservations_validate
    BEFORE INSERT OR UPDATE OF table_id, party_size ON reservations
    FOR EACH ROW EXECUTE FUNCTION reservations_validate();
```

Note `UPDATE OF table_id, party_size` — the trigger only fires when one of those
two columns changes, so a routine status update doesn't pay for it.

The function looks up the table joined to its restaurant, then enforces three
rules:

1. `party_size > seats` → reject (`check_violation`)
2. `party_size < min_party_size` → reject (`check_violation`)
3. `TG_OP = 'INSERT' AND NOT (table_active AND restaurant_active)` → reject

Rule 3 is the subtle one. **The bookability check is INSERT-only.** The comment
explains why: if it also fired on UPDATE, then deactivating a table would freeze
its existing reservations — they could no longer be cancelled or completed.
`TG_OP` is a variable holding the operation name.

`RAISE EXCEPTION … USING ERRCODE = 'check_violation'` sets the SQLSTATE the
client receives, which is how `app/reservations.py` can catch
`psycopg2.errors.CheckViolation` and translate it to `TableNotBookable`. The
error codes are the API between the layers.

`IF NOT FOUND THEN` catches a `SELECT … INTO` that matched nothing.

#### `reviews_validate()`

Enforces that you can only review a *finished, real* visit:

```sql
IF r.status <> 'completed' THEN … reject
IF upper(r.during) > now() THEN … reject
```

So you cannot review a cancelled booking, a no-show, or a dinner that hasn't
happened yet. This is what keeps the ratings feeding the per-city ranking
trustworthy. Checks 11 and 12 exercise it.

### `04_indexes.sql` — access paths

The header makes an important point: primary keys, UNIQUE constraints, and the
exclusion constraint's GiST index are created **implicitly** and are therefore
not repeated here. The `reservations_no_double_booking` index on
`(table_id, during)` is explicitly noted as *also* the index that serves the
availability anti-join.

| Index | Type | Serves |
|---|---|---|
| `idx_tables_restaurant_capacity` | btree, partial | "active tables at restaurant X seating N" |
| `idx_reservations_starts_at` | btree on expression, partial | time-slot reads with no `table_id` |
| `idx_reservations_customer_recent` | btree | "my bookings, newest first"; **FK index** |
| `idx_restaurants_location` | GiST | `ST_DWithin` radius + `<->` nearest-neighbour |
| `idx_restaurants_city` | btree, partial | per-city listing, `RANK()` partition key |
| `idx_restaurants_menu` | GIN `jsonb_path_ops` | menu containment |
| `idx_restaurants_opening_hours` | GIN default | weekday key probes |
| `idx_restaurants_name_trgm` | GIN `gin_trgm_ops` | `ILIKE '%…%'` |
| `idx_reviews_reservation_rating` | btree | index-only ratings rollup |
| `idx_restaurant_cuisines_cuisine` | btree | FK support, second leg |

Several teaching points are embedded:

**Column order matters.**

```sql
CREATE INDEX idx_tables_restaurant_capacity
    ON restaurant_tables (restaurant_id, seats, min_party_size)
    WHERE is_active;
```

`restaurant_id` (an equality predicate) comes first; `seats` (a range predicate,
`seats >= party_size`) trails it. A btree can seek on leading equality columns
and then range-scan — reverse the order and it can't.

**Partial indexes stay small.** `WHERE is_active` excludes rows that can never
be candidates. `idx_reservations_starts_at`'s `WHERE status IN (…)` matches the
availability predicate *exactly*, which is what allows the planner to use it.

**Expression indexes.**

```sql
CREATE INDEX idx_reservations_starts_at ON reservations (lower(during)) WHERE …;
```

Indexes the *result of a function*. A query filtering on `lower(during)` can use
it; one filtering on `during` alone cannot.

**Postgres does not auto-index foreign keys.** Two indexes here exist purely for
that: `idx_reservations_customer_recent` and `idx_restaurant_cuisines_cuisine`.
The comment spells out the consequence of omitting one — every customer delete
degenerates into a sequential scan of `reservations` to check the FK.

**GIN operator classes are a real choice.** `jsonb_path_ops` is roughly half the
size of the default `jsonb_ops` and is correct for `menu` because lookups there
are always *containment* questions ("has an item tagged vegan"). `opening_hours`
uses the default class because its probes are *key existence* by weekday, which
`jsonb_path_ops` does not index.

**Trigram indexes** are the only way to serve `ILIKE '%pattern%'` — a leading
wildcard makes a btree useless.

The file ends with `ANALYZE;` so the planner has fresh statistics and the first
`EXPLAIN ANALYZE` after a load is meaningful.

> A caveat worth remembering, from `docs/er-diagram.md` §5: at current seed
> volume the planner **correctly ignores** several of these indexes. 96
> `restaurant_tables` rows fit in about three pages, so a sequential scan beats
> an index lookup. That is the planner being right, not the index being wrong.

### `05_views.sql` — read models

Strict 3NF pushes a four-table join into every reporting query
(`reviews → reservations → restaurant_tables → restaurants → cities`). These
views pay that cost once, in one place.

`DROP VIEW … CASCADE` in reverse-dependency order first — note that
`v_restaurant_reviews` is built *on top of* `v_reservation_details`, so views can
compose.

**`v_reservation_details`** — flattens the whole chain. Exposes `during` plus
pre-extracted `lower(during) AS starts_at` / `upper(during) AS ends_at`, and
concatenates `first_name || ' ' || last_name AS customer_name`. Uses
`JOIN … USING (column)`, which is shorthand for `ON a.col = b.col` that also
collapses the duplicate column in the output.

**`v_restaurant_reviews`** — each review resolved to its restaurant and reviewer,
built by joining `reviews` to the view above.

**`v_restaurant_ratings`** — the rollup that Day 4 will rank over:

```sql
count(rv.review_id)               AS review_count,
round(avg(rv.rating)::numeric, 2) AS avg_rating,
…
max(upper(res.during))            AS last_reviewed_visit
FROM restaurants r
JOIN cities ci USING (city_id)
LEFT JOIN restaurant_tables rt  ON …
LEFT JOIN reservations      res ON …
LEFT JOIN reviews           rv  ON …
WHERE r.is_active
GROUP BY r.restaurant_id, r.name, ci.city_id, ci.name, ci.region, r.price_tier
```

Two details to learn from:

- The joins down to `reviews` are **LEFT** so that a restaurant with no reviews
  survives with `review_count = 0` rather than vanishing from listings. An inner
  join here is a classic reporting bug.
- `count(rv.review_id)` counts *non-null* values, so it correctly returns 0 for
  those restaurants — whereas `count(*)` would return 1 (counting the row
  produced by the outer join).
- `avg()` on an `integer` returns `numeric` already, but the explicit
  `::numeric` cast before `round(…, 2)` is needed because `round(double precision, int)`
  does not exist in Postgres — a genuinely common error.

### `06_seed.sql` — reproducible demo data

```sql
SELECT setseed(0.42);
```

Pins the PRNG so `random()` produces the same sequence on every reload. Without
it, row counts and ratings would drift between resets and the tests' assumptions
(and `EXPLAIN` comparisons) would wobble.

```sql
TRUNCATE reviews, reservations, … RESTART IDENTITY CASCADE;
```

`TRUNCATE` is far faster than `DELETE` (no per-row work, no dead tuples).
`RESTART IDENTITY` resets the sequences so IDs start from 1 again — which is why
the seed can rely on `restaurant_id % 4` for its rating baseline.

**Data shape:** 4 cities, 8 cuisines, 8 restaurants, 16 cuisine pairings, 12
tables per restaurant (96 total), 60 customers, ~9,500 reservations, ~2,650
reviews.

```sql
ST_MakePoint(-73.9890, 40.7419)::geography
```

The comment flags the single most common geospatial mistake: **`ST_MakePoint`
takes (longitude, latitude)** — the reverse of the order people speak them in.
Get it backwards and your New York restaurant lands in Antarctica.

**Tables** are generated with a `CROSS JOIN` against a `VALUES` list, giving
every restaurant the same 12-table floor plan: three 2-tops, four 4-tops, three
6-tops, an 8 and a 10, spread across `bar`/`indoor`/`patio`/`private`.

```sql
greatest(1, p.seats - 3)::smallint AS min_party_size
```

So a 2-top accepts 1–2, a 4-top accepts 1–4, a 6-top accepts 3–6, a 10-top
accepts 7–10. The comment explains the purpose: min_party_size scaling with the
table forces the availability query to *make a real choice* instead of always
landing on the largest table.

**Customers** use `generate_series(1, 60)` with a `CROSS JOIN LATERAL` picking
names out of two arrays by modular arithmetic:

```sql
(ARRAY['Ava','Noah',…])[1 + (g * 7) % 20] AS first_name,
(ARRAY['Nguyen','Patel',…])[1 + (g * 11) % 20] AS last_name
```

`LATERAL` is what allows the subquery to reference `g` from the outer query.
Multiplying by coprime constants (7 and 11 against 20) walks the arrays out of
step so you get varied combinations rather than 20 repeats. Postgres arrays are
**1-indexed**, hence the `1 +`.

**Reservations** — the interesting one:

```sql
CROSS JOIN generate_series(-45, 14) AS d(offset_days)
CROSS JOIN (VALUES (time '17:00'), (time '19:00'), (time '21:00')) AS s(slot)
…
WHERE random() < 0.55
```

96 tables × 60 days × 3 slots = 17,280 candidate slots, of which ~55% are kept →
roughly 9,500 rows. The range spans 45 days into the past and 14 into the
future, so there is both history to review and future inventory to book.

Each slot runs for the restaurant's own `slot_minutes`, so **consecutive
bookings abut but never overlap** — precisely the case the half-open bounds
exist to allow. If the bounds were closed, this seed would fail to load.

The status assignment is a nested `CASE` whose probabilities are worth reading
carefully:

```sql
WHEN upper(s.during) <= now() THEN
    CASE WHEN random() < 0.85 THEN 'completed'
         WHEN random() < 0.60 THEN 'cancelled'
         ELSE 'no_show' END
ELSE
    CASE WHEN random() < 0.80 THEN 'confirmed' ELSE 'pending' END
```

The branches are **conditional**, not independent: past bookings are 85%
completed, and of the remaining 15%, 60% cancelled (≈9% overall) and 40%
no-show (≈6%). Future bookings are 80% confirmed, 20% pending.

**Reviews** — ~45% of completed visits get one:

```sql
round((3.1 + (rt.restaurant_id % 4) * 0.45 + (random() - 0.5) * 1.6)::numeric)
```

Each restaurant gets a hidden quality baseline from its id: **3.10, 3.55, 4.00,
4.45**, plus noise of ±0.8. This is deliberate — it gives the Day 4 per-city
`RANK()` a stable, non-tied ordering to discover. Random ratings would produce
meaningless ties.

`greatest(1, least(5, …))` clamps to the 1–5 range. `substr(md5(…), 1, 24)`
fabricates a 24-hex-character string that satisfies the ObjectId format CHECK.

The file ends with `ANALYZE;` and a `UNION ALL` row-count summary so you can see
what loaded.

### `99_checks.sql` — a self-verifying invariant suite

This file is the schema's own test suite, and it is a genuinely nice pattern.

```sql
BEGIN;
…
ROLLBACK;
SELECT count(*) AS leftover_test_cities FROM cities WHERE name = 'Testville';
```

Everything runs inside a transaction that is **rolled back**, so it never leaves
test rows behind — and the final `SELECT` (which runs after the rollback) proves
it, returning 0.

Fixtures create a dedicated `Testville` city and `Check Kitchen` restaurant so
the checks cannot collide with seed data, plus a `CREATE TEMP VIEW fixture`
holding the ids and a slot 200 days out.

Each check follows this shape:

```sql
BEGIN
    INSERT …;                                    -- should fail
    RAISE EXCEPTION 'CHECK n FAILED: …';         -- reached only if it wrongly succeeded
EXCEPTION WHEN exclusion_violation THEN
    RAISE NOTICE 'ok  n: …';
END;
```

A plpgsql `BEGIN … EXCEPTION … END` block is a **subtransaction**. The insert is
expected to raise; if it *doesn't*, control reaches the `RAISE EXCEPTION` and the
whole script aborts. That inversion — "fail loudly if the bad thing was
allowed" — is what makes it a test rather than a demo.

The 16 checks:

| # | Proves |
|---|---|
| 1 | a first booking is accepted |
| 2 | an identical slot on the same table is rejected (23P01) |
| 3 | a *partial* overlap is rejected too |
| 4 | an **abutting** slot IS accepted — the half-open payoff |
| 5 | cancelling releases the slot for rebooking |
| 6 | a party larger than the table is rejected (cross-table trigger) |
| 7 | a party below `min_party_size` is rejected |
| 8 | closed `[]` bounds are rejected |
| 9 | an unbounded range is rejected |
| 10 | a 24-hour reservation is rejected |
| 11 | a review on a non-completed reservation is rejected |
| 12 | a malformed `body_ref` is rejected |
| 13 | a second review for one visit is rejected (unique_violation) |
| 14 | a second primary cuisine is rejected (the partial unique index) |
| 15 | a booking on a deactivated table is rejected |
| 16 | geography is real — `ST_DWithin` finds ≥1 restaurant within 3 km of Times Square |

Checks 2/3 versus 4 are the pair that pins *both directions* of the half-open
range behaviour: overlap must be refused, abutment must be allowed. Testing only
one side would let a regression through.

Run it with:

```bash
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U rr_app -d restaurant_reservations -f /sql/99_checks.sql
```

---

## 4. The Python layer, file by file

Eight modules in `app/`, layered so that each depends only on the ones below it:

```
feedback.py       ← MongoDB prose + cross-store writes   (Day 3)
cache.py          ← Redis availability cache             (Day 3)
     ↓
reservations.py   ← book / cancel / get / list
availability.py   ← the availability CTE
     ↓
db.py             ← pooling + transactions
models.py         ← row → dataclass
errors.py         ← domain exceptions
config.py         ← environment settings
```

`reservations.py` and `availability.py` accept a cache but only under
`TYPE_CHECKING` imports, so the arrow above never becomes a runtime dependency:
the booking core still imports cleanly with neither `redis` nor `pymongo`
installed.

### `app/config.py` — environment-driven settings

Deliberately dependency-free (no `pydantic`, no `python-dotenv`): values come
from `os.environ` with defaults matching `docker-compose.yml`, so a fresh clone
runs with no `.env` at all.

```python
@dataclass(frozen=True)
class Settings:
    dsn: str
    pool_min: int
    …
```

`frozen=True` makes instances immutable — settings can't be mutated at runtime
by accident.

```python
def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
```

Two good habits: an *empty* env var is treated as unset (a common shell
accident), and a bad value fails immediately with a message naming the variable
rather than surfacing as a confusing `TypeError` deep in the pool.

`load_settings()` also validates relationships between values —
`pool_min >= 1` and `pool_max >= pool_min` — which catches a misconfiguration at
startup instead of at first use.

### `app/errors.py` — domain exceptions

A single-rooted hierarchy under `BookingError`. The docstring states the
contract: **callers should never have to catch a psycopg2 error or inspect a
SQLSTATE.**

| Exception | Meaning | Should the caller retry? |
|---|---|---|
| `InvalidBookingRequest` | malformed arguments (naive datetime, party size 0) | No — fix the input |
| `PoolExhausted` | no connection within the timeout | Yes — backpressure, request never reached the DB |
| `RestaurantNotFound` | unknown or deactivated restaurant | No |
| `CustomerNotFound` | unknown customer | No |
| `ReservationNotFound` | unknown reservation | No |
| `NoTableAvailable` | valid request, nothing free in that slot | **No** — the house is full |
| `TableNotBookable` | a pinned table can't take this booking | No |
| `BookingConflict` | lost the race repeatedly, gave up | **Yes** — contention, try later |

The `NoTableAvailable` / `BookingConflict` distinction is the one that earns its
keep. Both mean "you didn't get a table", but one says *stop* and the other says
*try again* — an operational difference a generic `BookingFailed` would erase.
`test_exhausted_retry_budget_raises_conflict` exists specifically to pin it.

### `app/models.py` — row → object mapping

Two frozen dataclasses, `AvailableTable` and `Reservation`, each with a
`from_row()` classmethod. The docstring explains the purpose: rows arrive as
dicts (because of `RealDictCursor`), and these give callers a stable shape that
doesn't change when a query's column list does.

The interesting part:

```python
during = row["during"]
return cls(
    …
    starts_at=during.lower,
    ends_at=during.upper,
)
```

A `tstzrange` comes back from psycopg2 as a `psycopg2.extras.DateTimeTZRange`
object. Unpacking it here means callers deal in plain `datetime`s and never
import a driver-specific type — so swapping psycopg2 for something else would
not ripple outward.

Frozen dataclasses also get `__eq__` for free, which is what lets
`test_get_and_list` simply assert `fetched == res`.

### `app/db.py` — pooling and transaction scoping

The module docstring names two rules it exists to enforce:

- a connection is **always** returned to the pool, including on exception — a
  leaked connection is invisible until the pool is exhausted, at which point
  *everything* fails;
- a **broken** connection is discarded rather than recycled, since handing a
  dead socket to the next caller turns one transient failure into many.

#### The sharp edge it fixes

```python
self._slots = threading.Semaphore(pool_max)
```

`psycopg2.pool.ThreadedConnectionPool.getconn()` **raises `PoolError`
immediately** once `maxconn` connections are checked out — it does not wait. So
a brief traffic spike becomes hard failures for callers that would happily have
waited 50 ms.

The semaphore is **admission control**: at most `pool_max` callers may reach
`getconn()` at once, and the rest block on `self._slots.acquire(timeout=…)`
until a slot frees. Only if the timeout expires do you get `PoolExhausted`.
`test_more_concurrent_callers_than_connections` is the regression test.

#### Constructor details

```python
self._pool = pg_pool.ThreadedConnectionPool(
    pool_min, pool_max,
    dsn=dsn,
    cursor_factory=RealDictCursor,
    application_name=app_name,
    options=f"-c timezone=UTC {options}".strip(),
)
```

- `RealDictCursor` makes rows behave like dicts (`row["seats"]`) instead of
  tuples (`row[0]`), so queries stay readable and column reordering is harmless.
- `application_name` shows up in `pg_stat_activity` — invaluable for working out
  which process is holding a connection when debugging pool exhaustion.
- `options=-c timezone=UTC` pins the *session* timezone so `timestamptz`
  rendering doesn't depend on server locale or client host. The extra `options`
  parameter lets callers append more session GUCs — the test suite uses it for
  `deadlock_timeout=100ms`.

#### `connection()` — the guaranteed-return contract

```python
@contextlib.contextmanager
def connection(self):
    if not self._slots.acquire(timeout=self._acquire_timeout):
        raise PoolExhausted(…)

    conn = None
    broken = False
    try:
        conn = self._pool.getconn()
        yield conn
    except psycopg2.Error:
        broken = conn is not None and conn.closed != 0
        raise
    finally:
        if conn is not None:
            try:
                if not broken and conn.closed == 0:
                    conn.rollback()
            except psycopg2.Error:
                broken = True
                log.warning("rollback failed while returning connection", exc_info=True)
            self._pool.putconn(conn, close=broken or conn.closed != 0)
        self._slots.release()
```

Walk the `finally` block carefully — this is the heart of the module:

1. **Rollback before returning.** Any in-flight transaction is aborted so the
   next borrower never inherits a dirty session.
2. **The rollback is itself wrapped in try/except.** If rolling back fails, the
   connection is definitely broken — mark it and carry on rather than raising
   from a `finally` (which would mask the original exception).
3. **`putconn(close=…)`** discards genuinely broken connections instead of
   returning them to the pool.
4. **`self._slots.release()` is unconditional**, outside the `if conn is not
   None`. If it were skipped on any path, the semaphore would leak a permit and
   the pool would slowly strangle itself.

`conn.closed != 0` is psycopg2's "is this connection dead" check — 0 means open.

#### `transaction()`

```python
@contextlib.contextmanager
def transaction(self, *, readonly: bool = False):
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
```

Commit on success, roll back on *any* exception. Note `except BaseException`
rather than `except Exception` — that catches `KeyboardInterrupt` and
`SystemExit` too, so a Ctrl-C mid-transaction still rolls back cleanly.

The `readonly` implementation is a nice detail: **`SET TRANSACTION READ ONLY`
scopes itself to the current transaction**. The alternative,
`conn.set_session(readonly=True)`, is connection-level and would leak into
whatever the *next* borrower of that pooled connection does.

### `app/availability.py` — the availability CTE

The docstring makes a claim that matters: **the answer is advisory only.** By
the time a caller acts on it, another transaction may have taken the table —
which is exactly why `book_reservation` doesn't trust it.

#### `_REQUEST_CTE`

```sql
req AS (
    SELECT r.restaurant_id,
           %(party_size)s::smallint AS party_size,
           tstzrange(
               %(starts_at)s::timestamptz,
               %(starts_at)s::timestamptz
                   + make_interval(mins => COALESCE(%(duration_minutes)s::int,
                                                    r.slot_minutes)),
               '[)'
           ) AS during
      FROM restaurants r
     WHERE r.restaurant_id = %(restaurant_id)s
       AND r.is_active
)
```

A **CTE** (Common Table Expression, the `WITH` clause) is a named subquery. This
one computes the requested time window *inside SQL*, deriving the duration from
the restaurant's own `slot_minutes` via `COALESCE` — so an explicit
`duration_minutes` overrides it, and otherwise the restaurant decides.

`'[)'` builds the range with half-open bounds, matching the CHECK constraint.

`%(name)s` is psycopg2's **named parameter** placeholder. The driver sends the
value separately from the SQL text, so hostile input can never be parsed as
syntax — this is the SQL-injection defence, and
`test_sql_injection_attempt_is_inert` demonstrates it.

The `::type` casts are not decoration. psycopg2 sends parameters untyped, and
Postgres cannot always infer what a bare parameter is — especially when it might
be `NULL`. Without `::int`, `COALESCE(%(duration_minutes)s, r.slot_minutes)`
with a `NULL` argument raises "could not determine data type".

Because the CTE selects `FROM restaurants WHERE … AND r.is_active`, an unknown or
inactive restaurant yields **zero rows**, which cascades through the joins and
returns an empty result — see `test_availability_for_unknown_restaurant_is_empty`.

#### `_FREE_TABLES`

```sql
SELECT rt.table_id, rt.table_code, rt.seats, rt.min_party_size, rt.zone,
       req.during, req.party_size
  FROM req
  JOIN restaurant_tables rt
    ON rt.restaurant_id = req.restaurant_id
   AND rt.is_active
   AND req.party_size BETWEEN rt.min_party_size AND rt.seats
 WHERE (%(zone)s::table_zone IS NULL OR rt.zone = %(zone)s::table_zone)
   AND (%(table_id)s::int   IS NULL OR rt.table_id = %(table_id)s::int)
   AND NOT EXISTS (
           SELECT 1
             FROM reservations res
            WHERE res.table_id = rt.table_id
              AND res.during && req.during
              AND res.status IN ('pending', 'confirmed', 'seated', 'completed')
       )
```

This fragment is **shared** between the availability listing and the booking
INSERT — assigned to a module constant and interpolated into both. That is
deliberate: the two can never disagree about what "available" means.

Three techniques here:

**Optional filters via `NULL` checks.** `(%(zone)s IS NULL OR rt.zone = %(zone)s)`
means "if no zone was requested, don't filter". This keeps one static SQL string
instead of concatenating clauses — which would reintroduce injection risk and
defeat prepared-statement reuse.

**`NOT EXISTS` anti-join.** "Return tables for which no blocking reservation
overlaps." The `res.table_id = rt.table_id AND res.during && req.during`
predicate is exactly the shape of the GiST index backing
`reservations_no_double_booking`, so **no separate index is needed** to answer
"is this table free?". `NOT EXISTS` also handles NULLs correctly, unlike
`NOT IN`.

**The status list is repeated** here, in the exclusion constraint, and in
`idx_reservations_starts_at`. All three must agree — that duplication is the
main maintenance hazard in the schema.

#### The public function

```python
def find_available_tables(db, *, restaurant_id, starts_at, party_size, …):
```

The `*` makes every argument keyword-only, so call sites read as
`find_available_tables(db, restaurant_id=1, starts_at=…, party_size=4)` and can
never silently transpose two integers.

`ORDER BY seats, table_id` is a **capacity policy**, not cosmetics: filling a
party of two into a two-top keeps the six-tops free for parties that need them.
`table_id` is the deterministic tiebreak (the booking query uses `random()`
instead — see below).

`validate_request()` rejects naive datetimes and non-positive party sizes before
touching the database:

```python
if starts_at.tzinfo is None or starts_at.tzinfo.utcoffset(starts_at) is None:
    raise InvalidBookingRequest("starts_at must be timezone-aware; …")
```

The double check is correct — a `tzinfo` that returns `None` from `utcoffset()`
is still effectively naive. Rejecting naive datetimes matters because
interpreting one against a `timestamptz` column silently assumes a timezone, and
"7 PM" then means different instants on the server and the client.

### `app/reservations.py` — the booking transaction

The module docstring is the best single explanation in the repository; the key
points are reproduced in [§1](#1-the-one-idea-the-whole-system-rests-on).

#### The retryable set

```python
_RETRYABLE = (
    pg_errors.ExclusionViolation,     # 23P01
    pg_errors.DeadlockDetected,       # 40P01
    pg_errors.SerializationFailure,   # 40001
)
```

All three mean *"the transaction was rolled back, nothing was written, try
again"* — never *"the request was invalid"*. `23P01` is the obvious one.

**`40P01` is the subtle one, and understanding it is worth real effort.** An
`EXCLUDE` constraint inserts its index tuple **first**, and only then scans for
conflicts, waiting on any conflicting transaction it finds. So two bookers
racing for the same table have *each already inserted an entry the other must
wait on*. They wait on each other; Postgres detects the cycle and aborts one
with a deadlock error. That victim's booking is perfectly valid — it simply
needs reattempting, exactly like a `23P01` loser. Treating `40P01` as fatal
would fail a booking that should have succeeded.

The comment records that this was **found by an intermittent test failure**, and
`test_mutual_conflict_deadlocks_and_is_retried` now stages it deterministically.

`40001` is included pre-emptively should the isolation level ever be raised.

#### `BOOK_SQL` — selection and insertion in one statement

```sql
WITH req AS (…),
free AS (…),
candidate AS (
    SELECT table_id, during, party_size
      FROM free
     ORDER BY seats, random()
     LIMIT 1
)
INSERT INTO reservations
        (table_id, customer_id, during, party_size, status, special_requests)
SELECT c.table_id, %(customer_id)s::int, c.during, c.party_size,
       %(status)s::reservation_status, %(special_requests)s::text
  FROM candidate c
RETURNING reservation_id, table_id, customer_id, during, party_size,
          status, special_requests
```

Built by f-string interpolation of `_REQUEST_CTE` and `_FREE_TABLES` — note that
only *module constants* are interpolated; every user value goes through `%(name)s`
binding.

`ORDER BY seats, random()`:

- `seats` ascending is the capacity policy (smallest table that fits);
- `random()` is the tiebreak, and it is a **contention optimization**. Ordering
  by `table_id` would stack every concurrent booker onto the same lowest-id
  table, guaranteeing conflicts. Randomizing spreads them across equally-sized
  tables, which measurably cuts retries under load.

`INSERT … SELECT` inserts zero rows if the `SELECT` returns nothing, rather than
erroring — that is the "nothing available" signal the Python code checks for.

`RETURNING` hands back the inserted row in the same round-trip, so no follow-up
`SELECT` is needed.

#### The retry loop

```python
for attempt in range(1, max_attempts + 1):
    try:
        with db.transaction() as cur:
            cur.execute(BOOK_SQL, params)
            row = cur.fetchone()

        if row is not None:
            return Reservation.from_row(row)

        _raise_unavailable(db, params)          # zero rows: a real "unavailable"

    except _RETRYABLE as exc:
        log.debug(…)
        if attempt < max_attempts:
            time.sleep(random.uniform(0.0, 0.02) * attempt)
        continue

    except pg_errors.ForeignKeyViolation as exc:
        raise CustomerNotFound(…) from exc

    except pg_errors.CheckViolation as exc:
        raise TableNotBookable(str(exc).strip()) from exc

raise BookingConflict(…)
```

The crucial distinction: **zero rows inserted** and **an exception** mean
completely different things.

- Zero rows → the candidate subquery genuinely found nothing. Not a race.
  Diagnose and raise a specific error.
- `_RETRYABLE` exception → lost a race. Postgres has already rolled the
  transaction back, so retrying is safe, and the next attempt re-runs the
  candidate search and lands on a **different table**.

That is why `max_attempts` bounds *absorbable contention*, not latency. A caller
racing N−1 others for the last few tables needs enough attempts to walk past
every table it loses — which is why
`test_contending_threads_fill_every_table_exactly_once` sets
`max_attempts = thread_count + capacity`.

`time.sleep(random.uniform(0.0, 0.02) * attempt)` is **randomized exponential-ish
backoff**. The randomness de-synchronizes contending writers (without it, losers
retry in lockstep and collide again); scaling by `attempt` backs off further as
contention proves persistent.

The error translation maps SQLSTATEs to domain exceptions: a
`ForeignKeyViolation` at this point can only be a bad `customer_id` (the
restaurant and table were already validated by the CTE), and a `CheckViolation`
means the cross-table trigger refused it.

Falling out of the loop means every attempt lost a race → `BookingConflict`,
which tells the caller to retry later.

#### `_raise_unavailable()` — diagnosis on the failure path only

Runs *only* when zero rows were inserted, so the extra queries cost nothing in
the common case. It walks candidate causes in order of specificity:

1. restaurant missing → `RestaurantNotFound`
2. restaurant inactive → `RestaurantNotFound`
3. customer missing → `CustomerNotFound`
4. if a table was pinned: doesn't exist / belongs to another restaurant /
   inactive / wrong size → `TableNotBookable`; otherwise → `NoTableAvailable`
   ("already booked for that slot")
5. otherwise count tables that *could* seat this party ignoring time:
   - 0 → "no table that seats a party of N"
   - \>0 → "all N table(s) … are booked for that slot"

Step 5 is the distinction that makes the error useful: *"we have no table that
size"* is a permanent answer, *"they're all taken tonight"* is a temporary one.

Note the whole diagnosis runs in `db.transaction(readonly=True)`.

#### `cancel_reservation()`

```sql
UPDATE reservations
   SET status = 'cancelled'
 WHERE reservation_id = %(reservation_id)s
   AND status NOT IN ('completed', 'cancelled')
RETURNING …
```

Cancelling **sets a status rather than deleting the row**. Because the exclusion
constraint is partial, a `cancelled` row stops being counted — so the table frees
up while history survives.

The `AND status NOT IN (…)` guard makes the operation idempotent-safe: if it
matches nothing, the code re-queries to distinguish *doesn't exist*
(`ReservationNotFound`) from *already terminal* (`BookingConflict`). Doing this
in one UPDATE avoids a check-then-act race of its own.

### `app/cache.py` — the Redis availability cache (Day 3)

Wraps a Redis client and is passed explicitly to `find_available_tables`,
`book_reservation`, and `cancel_reservation`. Nothing takes it implicitly, and
`app/__init__.py` does **not** re-export it — importing this module pulls in
`redis`, and the Day 1–2 core must keep working with only psycopg2 installed.

**What it may and may not do.** Only the read path consults it.
`book_reservation` never does, because correctness comes from the exclusion
constraint and a cache that could veto a booking would be a cache that can
corrupt the schedule. Availability was already advisory, so a cached answer is
no less trustworthy than a fresh one.

**Invalidation via generation counter.** Instead of scanning and deleting keys,
each restaurant carries a counter that is part of every entry key:

```
avail:v1:gen:<restaurant_id>                                     the counter
avail:v1:<rid>:g<N>:<instant>:<party>:<zone>:<duration>          an entry
```

`invalidate()` is a single `INCR`, which makes the whole generation unreachable
in O(1). Superseded entries are never deleted — they expire. Two ordering rules
carry the correctness, and both are easy to invert:

1. `find_available_tables` reads the generation **before** querying Postgres. The
   other way round, a write landing mid-query bumps the counter first and the
   stale rows get written under the *new* generation.
2. `book_reservation` / `cancel_reservation` invalidate **strictly after** the
   transaction commits. Bumping earlier lets a concurrent reader take the new
   generation, query Postgres, still see pre-commit state, and cache it where
   nothing will supersede it.

Within those rules, losing a race is harmless: the reader writes under a
generation that is already obsolete, so nobody reads it.

**The instant is normalized** to UTC ISO format in `_entry_key`, so `19:00Z` and
`21:00+02:00` — the same moment — share one entry rather than caching twice.

**Failure policy: degrade to a miss.** Every method catches the exception and
returns "not cached", because Redis holds a rebuildable copy of what Postgres
already knows. Socket timeouts default to 250 ms: a cache that blocks longer
than the query it replaces is worse than no cache. Errors are counted in
`stats` so an outage is visible rather than merely survivable.

**The TTL is not the invalidation mechanism.** Writes through this package
invalidate explicitly; `CACHE_TTL_SECONDS` (default 60) bounds staleness from
writes that bypass the package entirely — a `psql` session, a cron job, another
service. The generation counter carries a much longer TTL (30 days) for a
specific reason spelled out in the code: if a counter expired while entries from
generation 0 were still live, it would restart at 0 and those entries would
become reachable again.

`invalidate_all(cache, ids)` is a small convenience that no-ops on a `None`
cache, so call sites need not repeat the guard.

### `app/feedback.py` — the MongoDB store (Day 3)

**The split.** Numeric ratings stay in `reviews` because `v_restaurant_ratings`
aggregates over them and the per-city `RANK()` needs them joined to `cities`.
Prose lives in `restaurant_feedback.reviews` because it has no relational shape
and is never joined or aggregated. `reviews.body_ref` is the only link.

**Why the document denormalizes what Postgres normalizes.** `docs/er-diagram.md`
argues at length against storing `restaurant_id` on `reviews`, and this module
stores it on every document. That is the same reasoning applied to a store with
different mechanics: *within* Postgres a derived column is a liability because
`reservation_id` already determines the restaurant and nothing could stop the two
disagreeing; *across* stores there is no shared constraint to lose, since MongoDB
cannot enforce a foreign key into Postgres under any schema. The copy buys the
thing the split exists for — "prose for restaurant X" without a round trip — and
has one documented owner: **Postgres wins every disagreement.**

**Write ordering, and why it is forced.** `add_review` is a free function, not a
method, because it spans both stores and neither owns it:

1. **Postgres** — upsert the ratings row, `body_ref` still NULL. It must come
   first: the `reviews_validate` trigger checks the visit is completed, and there
   is no point writing prose the database will refuse.
2. **MongoDB** — upsert the document, keyed on `reservation_id`.
3. **Postgres** — set `body_ref`.

The pointer is written last **because the pointer is the commitment.** A crash
between 1 and 2 leaves ratings with no prose, which is legal (`body_ref` is
nullable). Between 2 and 3 it leaves an unlinked document, which `reconcile()`
detects and which re-running `add_review` heals by itself, since both upserts are
idempotent. The reverse order would produce a *dangling* pointer, where a reader
cannot distinguish "not written yet" from "lost" — the one outcome worth
designing around.

The Postgres half uses `ON CONFLICT (reservation_id) DO UPDATE`, wrapped in a CTE
that joins `v_reservation_details` to pick up the `restaurant_id` and
`customer_id` the document needs — one round trip instead of two.

**Failure policy: propagate.** The opposite of `cache.py`, and for a stated
reason: Redis holds a rebuildable copy, MongoDB holds the only copy of the prose.
`pymongo` errors reach the caller.

**Indexes** (`ensure_indexes()`, idempotent): `reservation_id` unique — mirroring
the `UNIQUE` in Postgres so "one review per visit" holds in both stores;
`(restaurant_id, created_at desc)` — the query the denormalized column exists to
serve; and a weighted text index over `title` + `body`, which is the capability
that justifies the store at all.

**`reconcile()` stands in for the constraint that cannot exist.** It reports
three kinds of divergence:

| Kind | Meaning | Repairable? |
|---|---|---|
| `dangling` | Postgres points at a document that is not there | only if a document for the same reservation exists under another `_id`; otherwise the pointer is cleared and the prose is genuinely lost |
| `unlinked` | document exists, `body_ref` is NULL — the crash-between-2-and-3 case | always: set the pointer |
| `orphaned` | document whose reservation has no review row | **reported, never deleted** |

That last row is a deliberate refusal. Automatically destroying a diner's prose
because a pointer went missing is not a repair, and it is not reversible.

**`reservation_ids` scopes the comparison**, and exists because the unscoped form
is a footgun. Without it, `repair=True` is a whole-database write that clears
every `body_ref` it cannot resolve — which is correct only when `store` really is
the collection those reviews point into. This suite learned it the hard way: the
reconcile tests originally ran unscoped repairs against the *test* collection
while pointing at the *real* `reviews` table, so all ~2,650 seeded pointers
resolved to nothing, were classified as dangling, and were cleared. Every test
passed. `test_scoped_repair_leaves_other_reviews_alone` is the regression test.

`reconcile` loads both key sets into memory — fine at this scale (thousands of
reviews), and the docstring names the point at which it stops being fine.

#### `app/__init__.py`

Re-exports the public surface with an explicit `__all__`, so callers write
`from app import book_reservation` and never reach into submodules. `cache` and
`feedback` are deliberately excluded, as noted above; they are imported directly.

---

## 5. The tests

69 tests across five files, plus shared fixtures. They are **not hermetic** —
they run against the seeded development database, as `pytest.ini` states.
`./scripts/db.sh reset` restores a known state.

The Day 3 files skip themselves when their store is unreachable
(`pytest.importorskip` plus a `ping`), so the Postgres-only suite still runs on a
machine with no Redis or MongoDB.

```bash
.venv/bin/python -m pytest
```

### `tests/conftest.py` — fixtures

The isolation strategy is simple and effective: **seed data spans 45 days back
to 14 days forward, so every test books ~300 days out**, and an autouse fixture
purges that window before *and* after each test.

```python
@pytest.fixture(autouse=True)
def clean_test_window(db):
    purge(); yield; purge()
```

`autouse=True` applies it to every test without being requested. Purging *before*
as well as after means a crashed previous run can't poison the next one.

```python
database = Database(
    settings.dsn, pool_min=2, pool_max=24, app_name="rr-tests",
    options="-c deadlock_timeout=100ms",
)
```

The pool is widened to 24 so the concurrency tests genuinely *contend* rather
than queueing on connection acquisition.

The `deadlock_timeout` override has a great comment attached. Mutual EXCLUDE
conflicts resolve via deadlock detection, which costs a full `deadlock_timeout`
(**1 second by default**) per occurrence. The contention tests provoke these
deliberately — at the default, one unlucky run took **14 minutes**. 100 ms keeps
the suite quick without changing any behaviour under test: the retry path is
identical, just cheaper.

Session-scoped fixtures (`restaurant_id`, `customer_ids`, `two_seat_table`) query
the seed once and assert helpfully if it's missing: `"seed data missing — run
./scripts/db.sh reset"`.

### `tests/test_booking.py` — 27 single-threaded tests

Behaviour, in groups:

**Happy path** — books a table; the duration comes from the *restaurant*
(Vero Nord seats for 105 minutes, not the 90 default);
`duration_minutes` overrides it.

**Allocation policy** — a party of 2 gets a 2-top, not a 6-top; a second booking
takes a different table; `zone="patio"` is respected.

**Range semantics** — `test_abutting_slot_is_bookable` books at
`first.ends_at` and expects success on the *same* table. This is the half-open
payoff, verified from Python. Its counterpart,
`test_overlapping_slot_on_pinned_table_is_rejected`, books at +30 minutes and
expects rejection.

**Error precision** — each error path gets a test that asserts on the message:
`match="already booked"`, `match="no table that seats"`, `match="seats 1-2"`,
`match="belongs to restaurant"`. That is what stops `_raise_unavailable`'s
carefully-distinguished cases from silently collapsing into one.

**Cancellation** — cancelling frees the slot for rebooking on the same table;
cancelling twice raises `BookingConflict`.

**Injection** — `test_sql_injection_attempt_is_inert` passes
`"'; DROP TABLE reservations; --"` as `special_requests`, then proves the table
still exists and the payload round-trips as *text*.

**Availability** — the list shrinks by exactly one after a booking, is ordered
smallest-first, and its window matches the restaurant's `slot_minutes`.

### `tests/test_concurrency.py` — 6 threaded tests

The docstring states the stakes: a `SELECT`-then-`INSERT` booking function
passes every test in `test_booking.py` and still corrupts the schedule under
load. These tests open the race window deliberately.

```python
def _race(worker, thread_count):
    barrier = Barrier(thread_count)
    def runner(i):
        barrier.wait()      # no thread starts before the others
        return worker(i)
    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        return list(pool.map(runner, range(thread_count)))
```

A `threading.Barrier` releases all threads at the same instant, maximizing
overlap. Without it, thread 0 would usually finish before thread 11 started.

| Test | Asserts |
|---|---|
| `test_only_one_thread_wins_a_single_table` | 12 threads, 1 table → **exactly one** booking; then re-queries the DB to confirm one live row |
| `test_contending_threads_fill_every_table_exactly_once` | `capacity + 8` threads → exactly `capacity` winners, **all distinct tables** |
| `test_no_overlapping_live_reservations_anywhere` | after a contended run, a **self-join** finds zero overlapping live pairs |
| `test_exhausted_retry_budget_raises_conflict` | `max_attempts=1` → losers report `BookingConflict`, not `NoTableAvailable` |
| `test_pool_is_not_leaked_under_load` | `pool_max × 4` bookings through `pool_max` workers all succeed |
| `test_more_concurrent_callers_than_connections` | `pool_max + 8` callers all complete — the semaphore regression test |

The second is the strongest statement of the invariant: *no table is lost to a
spurious conflict, and none is handed to two customers*. The third checks the
invariant globally rather than per-test-case:

```sql
SELECT count(*) FROM reservations a
  JOIN reservations b
    ON a.table_id = b.table_id
   AND a.reservation_id < b.reservation_id
   AND a.during && b.during
 WHERE a.status IN (…) AND b.status IN (…)
```

The `a.reservation_id < b.reservation_id` clause is what stops each pair being
counted twice and stops a row matching itself.

`test_pool_is_not_leaked_under_load` carries an instructive comment about a
**previous version of itself**: it used to point every worker at the same slot,
which made it a contention stress test wearing a pooling test's name — past
capacity, every thread burned its full retry budget, each mutual conflict cost a
`deadlock_timeout`, and workers held connections long enough to starve their
peers into `PoolExhausted`. Now each worker books a different hour, isolating
connection lifecycle from contention. Good lesson: a test that fails for the
wrong reason is worse than no test.

### `tests/test_race_deterministic.py` — 6 staged tests

The best file in the suite conceptually. Its premise: the thread races above
assert the right outcomes but **rarely exercise the retry branch**, because the
window between candidate search and insert is narrow enough that most losing
threads simply find the table already taken.

These tests remove the timing dependency entirely, by exploiting a property of
EXCLUDE constraints: **a conflicting inserter blocks until the first transaction
resolves, and only then learns its fate.**

```python
class _Holder:
    """Holds an uncommitted reservation open until told to commit or roll back."""
```

A background thread inserts a reservation, signals `inserted`, then waits on an
event before committing or rolling back. That pins the interleaving *exactly*.

| Test | Proves |
|---|---|
| `test_conflicting_insert_blocks_until_holder_resolves` | the second inserter **waits** rather than proceeding on stale data; gets 23P01 when the holder commits |
| `test_conflicting_insert_succeeds_when_holder_rolls_back` | the blocked inserter proceeds once the slot is genuinely released |
| `test_book_reservation_retries_onto_another_table` | the retry loop, forced: all other 2-tops booked out, the last one held → the call blocks, gets 23P01, retries, lands on a **4-top** |
| `test_pinned_booking_reports_unavailable_after_losing` | losing a race for a **pinned** table has no fallback → `NoTableAvailable`, not `BookingConflict` |
| `test_mutual_conflict_deadlocks_and_is_retried` | two bookers inserting in **opposite orders** deadlock; exactly one victim, error is `DeadlockDetected`, and it is in `_RETRYABLE` |
| `test_exclusion_constraint_is_the_backstop` | a **raw** double insert, bypassing the application entirely, is still refused |

The blocking assertion pattern is careful:

```python
thread.join(timeout=_BLOCK_PROBE_SECONDS)   # 0.75s
assert thread.is_alive(), "second inserter should still be blocked"
assert result == {}, f"second inserter finished early: {result}"
```

0.75 s is chosen so a false pass requires the second inserter to be *starved* for
that long, not merely slow.

The last test is a guard against a future "optimization": someone might notice
the Python layer already checks availability and propose dropping the
constraint. This test fails loudly if they do.

### `tests/test_cache.py` — 13 tests

Every test is a statement of the same claim: **the cache changes latency and
nothing else.**

| Test | Proves |
|---|---|
| `test_first_lookup_misses_and_second_hits` | the read-through works at all |
| `test_cached_result_matches_uncached` | the hot result is *identical*, not merely similar |
| `test_booking_invalidates_the_listing` | a booking is not served from a stale listing |
| `test_cancelling_invalidates_the_listing` | the riskier direction — a freed table must reappear |
| `test_booking_never_reads_the_cache` | poisons Redis with "nothing is free"; the booking still succeeds |
| `test_invalidation_makes_the_old_generation_unreachable` | the counter semantics |
| `test_party_size_zone_and_duration_are_separate_entries` | key components actually discriminate |
| `test_equivalent_instants_share_one_entry` | `19:00Z` and `21:00+02:00` do not cache twice |
| `test_a_dead_redis_degrades_to_a_miss` | an outage costs latency, never correctness |
| `test_a_write_survives_a_dead_redis` | a failed invalidation does not fail the booking |
| `test_corrupt_entry_is_treated_as_a_miss` | an unparseable payload does not fail the request |
| `test_entries_carry_a_ttl` | the backstop is actually set |
| `test_unknown_restaurant_is_cached_as_empty` | the empty result is cacheable too |

The dead-Redis tests point a client at `127.0.0.1:1` with 100 ms timeouts — a
refused connection is a faster and more deterministic outage than stopping the
container would be.

Isolation uses a separate key namespace (`avail:test`) plus an autouse fixture
that `SCAN`s and deletes it around each test. The fixture checks
`request.fixturenames` first, so tests that never touch Redis neither pay for the
purge nor fail when Redis is absent.

### `tests/test_feedback.py` — 17 tests

The interesting cases are the seams, not the round trip:

| Test | Proves |
|---|---|
| `test_add_review_writes_both_stores` | ratings land in Postgres, prose in Mongo, and the pointer resolves |
| `test_add_review_is_idempotent` | re-running heals a partial write instead of duplicating |
| `test_review_on_an_unfinished_visit_is_refused` | the Postgres trigger runs first, so **no orphan document is created** |
| `test_review_without_prose_reports_precisely` | ratings-only is legal; `FeedbackNotFound` is a distinct outcome |
| `test_full_text_search` / `test_search_can_be_scoped_to_a_restaurant` | the capability that justifies the store |
| `test_ratings_remain_aggregatable_in_postgres` | the split did not put the numbers out of reach of `v_restaurant_ratings` |
| `test_reconcile_detects_and_repairs_an_unlinked_document` | the crash-between-2-and-3 case |
| `test_reconcile_detects_a_dangling_pointer` | Postgres pointing at a deleted document |
| `test_reconcile_reports_orphans_without_deleting_them` | asserts the prose **survives** a repair pass |

Ordering matters in the third row: because Postgres is written first, a review
the trigger refuses never reaches MongoDB. The test asserts the absence
explicitly rather than trusting the sequence.

Isolation uses a dedicated `restaurant_feedback_test` database, dropped at the
end of the session. The `unreviewed` fixture selects seeded reservations that are
`completed`, past, and unreviewed — reviews cannot be tested against the
far-future slots the booking tests use, because `reviews_validate` rejects them.

The `reconcile` tests assert **membership**, never totals: a freshly seeded
database has ~2,650 dangling refs until `backfill_feedback.py` runs, so any
count-based assertion would be coupled to whether that script had been run.

---

## 6. The demo and utility scripts

### `scripts/demo_booking.py`

A narrated end-to-end walkthrough:

```bash
.venv/bin/python scripts/demo_booking.py
```

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

Puts the project root on `sys.path` so `import app` works when the script is run
directly from `scripts/` (the `# noqa: E402` on the imports below acknowledges
that imports-after-code is intentional here).

Six sections: list availability for a party of 4 → book one → show the
smallest-fitting-table policy → cancel and rebook the freed table → **fire 12
threads at a single table** and show exactly one winner → list a customer's
recent bookings.

It books 320 days out and wraps the whole body in `try/finally` so `_purge()`
runs even on failure. `_purge` returns `cur.rowcount` so the script can report
how many demo rows it removed.

### `scripts/demo_day3.py`

The same shape for the Day 3 integrations, in seven sections: cold vs. warm
lookup with timings, invalidation on booking, invalidation on cancellation, a
simulated Redis outage, full-text search, following `body_ref` across both
stores, and a `reconcile` report.

Section 4 silences the `app.cache` logger before triggering the outage. The
warning-with-traceback is correct behaviour in a real system and pure noise in a
demo whose whole point is that the *caller* sees nothing.

Its search terms are drawn from the backfill's own vocabulary. An earlier version
searched for words that only appeared in the test fixtures and reported zero hits
— a reminder that a demo asserting nothing can look broken while working
perfectly.

### `scripts/backfill_feedback.py`

Gives the seeded reviews real MongoDB documents, closing the dangling-reference
gap `06_seed.sql` leaves behind. Worth reading for three techniques:

**Client-side ObjectId generation.** `bson.ObjectId()` mints an id locally, so
the Postgres pointer is known *before* the document is written. That is what
allows both halves to run as single bulk calls — one `bulk_write`, one
`execute_values` — instead of ~2,650 round trips each.

**Deterministic prose.** `random.Random(reservation_id)` seeds a PRNG per review,
so re-running produces identical text — the same reproducibility `setseed(0.42)`
gives the SQL seed.

**An explicit `page_size`.** `execute_values` batches internally and
`cur.rowcount` then reports only the *last* batch. A first version printed
"48 body_ref(s) repointed" for 2,648 rows. Passing `page_size=len(links)` forces
one statement so the count is real — worth knowing generally, since the wrong
number looks entirely plausible.

It re-reads the authoritative `_id`s after the bulk write rather than trusting
the generated ones, because a document that already existed keeps its original
id, and then verifies itself by printing a `reconcile` report.

---

## 7. Postgres feature glossary

Quick reference for everything the project uses.

| Feature | What it does | Where |
|---|---|---|
| **`EXCLUDE` constraint** | forbids row *pairs* satisfying given operators — a generalized UNIQUE | `reservations_no_double_booking` |
| **GiST index** | generalized search tree; supports overlap/containment/nearest-neighbour | exclusion constraint, `location` |
| **`btree_gist`** | adds scalar `=` operator classes to GiST | required by the above |
| **`tstzrange`** | timestamptz range with bound inclusivity; ops `&&`, `@>`, `-\|-` | `reservations.during` |
| **Half-open `[)`** | lower inclusive, upper exclusive — lets intervals abut without overlapping | `reservations_during_half_open` |
| **Partial index/constraint** | `WHERE` clause limits which rows participate | one-primary-cuisine; the exclusion constraint |
| **Expression index** | indexes a function result | `lower(during)` |
| **GIN index** | inverted index for composite values | `jsonb`, trigrams |
| **`jsonb_path_ops`** | smaller GIN opclass for containment only | `menu` |
| **`pg_trgm`** | trigram matching for `ILIKE '%…%'` | restaurant name search |
| **`citext`** | case-insensitive text | emails, cuisine names |
| **PostGIS `geography`** | spheroid coordinates; metre-accurate distance | `restaurants.location` |
| **`ST_DWithin`** | "within N metres of", index-assisted | check 16 |
| **SRID 4326** | WGS84 — ordinary GPS lat/lon | `geography(Point, 4326)` |
| **CTE (`WITH`)** | named subquery | availability, booking |
| **`LATERAL`** | lets a subquery reference the row to its left | seed data generation |
| **`NOT EXISTS` anti-join** | rows with no matching partner; NULL-safe | availability |
| **`INSERT … SELECT … RETURNING`** | insert from a query, get the row back in one round-trip | `BOOK_SQL` |
| **`GENERATED ALWAYS AS IDENTITY`** | standard auto-increment; blocks manual writes | every PK |
| **Enum type** | fixed value list as a type | `table_zone`, `reservation_status` |
| **Trigger + plpgsql** | row-level logic; the only way to span tables | `03_triggers.sql` |
| **SQLSTATE** | 5-char error code — the API between DB and app | `23P01`, `40P01`, `23503`, `23514` |
| **`setseed`** | pins the PRNG for reproducible data | `06_seed.sql` |
| **`TRUNCATE … RESTART IDENTITY`** | fast wipe + sequence reset | `06_seed.sql` |

**SQLSTATEs that matter here:**

| Code | Name | psycopg2 class | Meaning in this project |
|---|---|---|---|
| `23P01` | `exclusion_violation` | `ExclusionViolation` | lost a booking race — **retry** |
| `40P01` | `deadlock_detected` | `DeadlockDetected` | mutual EXCLUDE conflict — **retry** |
| `40001` | `serialization_failure` | `SerializationFailure` | isolation conflict — **retry** |
| `23503` | `foreign_key_violation` | `ForeignKeyViolation` | unknown `customer_id` |
| `23514` | `check_violation` | `CheckViolation` | trigger refused it — table not bookable |
| `23505` | `unique_violation` | `UniqueViolation` | duplicate review / second primary cuisine |

**Redis and MongoDB features used:**

| Feature | What it does | Where |
|---|---|---|
| `INCR` | atomic increment; creates the key at 1 if absent | generation counter |
| `SET … EX` | write with a TTL in one command | cache entries |
| `SCAN` | cursor-based key iteration; non-blocking, unlike `KEYS` | test teardown, and the design this cache *avoids* |
| pipeline | batches commands into one round trip | `INCR` + `EXPIRE` in `invalidate` |
| `--appendonly` | AOF persistence | compose config (unnecessary for a pure cache) |
| `update_one(upsert=True)` | insert-or-update in one atomic operation | idempotent document writes |
| `$set` / `$setOnInsert` | update always / only on insert | preserves `created_at` across re-runs |
| unique index | rejects duplicate `reservation_id` | mirrors the Postgres `UNIQUE` |
| text index + `$text` | full-text search, optionally weighted per field | `search()` |
| `$meta: "textScore"` | relevance score for projection and sorting | `search()` ordering |
| `bulk_write` | many operations in one round trip | backfill |
| `ObjectId()` | 12-byte id, generatable client-side | backfill's two-phase link |

---

## 8. How a single booking flows end to end

Tracing `book_reservation(db, restaurant_id=1, customer_id=7, starts_at=…, party_size=4)`:

```
1.  validate_request()          naive datetime? party_size < 1?   → InvalidBookingRequest
2.  load_settings()             max_attempts default (8)
3.  build_params()              assemble the bind dict
4.  ┌─ attempt 1..max_attempts
5.  │  db.transaction()         semaphore → getconn → BEGIN
6.  │  execute(BOOK_SQL)
7.  │     req CTE               resolve the restaurant, derive [start, start+slot_minutes)
8.  │     free CTE              active tables, size fits, zone/table filters,
9.  │                           NOT EXISTS overlapping live reservation   [GiST]
10. │     candidate             ORDER BY seats, random() LIMIT 1
11. │     INSERT … RETURNING    ← exclusion constraint evaluated HERE
12. │  commit / rollback        connection returned to pool
13. │
14. │  row is not None  →  Reservation.from_row(row)                     ✓ done
15. │  row is None      →  _raise_unavailable()  → specific error        ✗
16. │  23P01 / 40P01    →  backoff, retry (different table next time)    ↻
17. │  23503            →  CustomerNotFound                              ✗
18. │  23514            →  TableNotBookable                              ✗
19. └─ budget exhausted →  BookingConflict                               ✗
```

The line to focus on is **11**. Everything above it is advisory; the constraint
at line 11 is what actually decides. That is why steps 7–10 can be optimistic and
lock-free, and why line 16 is a retry rather than an error.

---

## 9. Known gaps and doc drift

### Gaps (scoped out, not oversights)

From `docs/er-diagram.md` §6:

- **Restaurant-local time zones are unmodelled.** `opening_hours` stores
  wall-clock strings (`"17:00"`) while `reservations.during` is absolute
  `timestamptz`. Interpreting one against the other across cities needs an IANA
  timezone column on `restaurants`. The container runs UTC so the seed is
  self-consistent, but a real deployment must close this.
- **Opening hours are not enforced against bookings.** Nothing rejects a
  reservation at 03:00 on a day the restaurant is closed.
- **No table combining** — a party of 6 cannot be seated by joining two 4-tops.
- **No waitlist, deposits, or partial-party seating.**
- **`reviews.body_ref` is format-checked but not referentially enforced** — no
  database can enforce a foreign key into MongoDB.

And from the code:

- **The seeded `body_ref` values point at nothing** until
  `scripts/backfill_feedback.py` runs. `sql/06_seed.sql` fabricates them with
  `substr(md5(...), 1, 24)`, so a fresh seed leaves ~2,650 dangling cross-store
  references and `reconcile()` reports every one of them.
- **The blocking-status list is duplicated four times** — in the exclusion
  constraint, `idx_reservations_starts_at`, `_FREE_TABLES`, and
  `_BLOCKING_STATUSES` in `reservations.py`. (That last constant is defined but
  never read.) All must be kept in agreement by hand.
- **Escalation path if same-slot contention ever becomes real:** a
  `pg_advisory_xact_lock` keyed on the contended slot would serialize those
  bookings and remove the mutual-conflict window. Documented in the README as
  deliberately *not* implemented — optimistic retry is the right default.

- **Rating 1 is unreachable in the seed.** The per-restaurant baseline floor is
  3.10 and the noise term is ±0.8, so the lowest rating `06_seed.sql` can
  produce is 2. Any analysis assuming a full 1–5 spread will find an empty
  bottom band.

### Doc drift (found and fixed)

Three inconsistencies in `README.md` were corrected while writing this: a stale
test count, a `PostGIS 3.4` reference where `docker-compose.yml` pins `3.5`, and
an Apple Silicon note advising a switch to the image the compose file already
used.
