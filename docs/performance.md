# Query Performance — measurements and what changed because of them

Day 4. Everything here was measured on this machine with
`scripts/explain.py`; nothing is quoted from memory or from a blog post. Re-run
it yourself:

```bash
.venv/bin/python scripts/explain.py -n 15 --scale
```

---

## 1. How these numbers were produced

`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, driven from Python so each probe can
be repeated and summarised.

- **Median of 15 runs, first run discarded.** One run measures cache state as
  much as it measures the query. The warm-up is thrown away and the median —
  not the mean, and certainly not the best — is reported, so a checkpoint or an
  autovacuum landing mid-run cannot flatter the result.
- **Postgres's own `Execution Time`**, which excludes client round trip and
  result serialisation. This measures the database, not psycopg2.
- **`Planning Time` is reported separately**, because for the fastest queries
  here it is the larger number. See finding 2.
- **Buffer counts alongside timings.** Buffers are the stabler signal: they
  count pages touched and barely move between runs, whereas timings on a laptop
  swing with whatever else the machine is doing.
- **A/B via `SET LOCAL enable_indexscan = off`** (plus bitmap and index-only)
  rather than dropping indexes. It forces the planner to cost the alternative on
  the same data, in the same session, with no schema change to undo.

**Environment.** PostgreSQL 16.10 in Docker, `imresamu/postgis:16-3.5` — the
multi-arch build, so on Apple Silicon this is native rather than emulated.
Dataset is the standard seed: 8 restaurants, 96 tables, 9,517 reservations,
2,648 reviews.

**Caveat, stated up front.** This is a laptop, not an isolated benchmark rig.
Sub-millisecond timings carry noise of the same order as the measurement — the
`slot-demand` probe showed a 10.9 ms spread across 15 runs of a ~7 ms query.
Treat ratios and buffer counts as the findings; treat absolute milliseconds as
indicative.

---

## 2. Results

Median of 15 runs, seed dataset:

| probe | exec | planning | rows | buffers | access path |
|---|---:|---:|---:|---:|---|
| `availability-cte` | **0.082 ms** | 0.218 ms | 1 | 18 | `reservations_no_double_booking` |
| `availability-cte` (index disabled) | 1.342 ms | 0.254 ms | 1 | 127 | seq scan |
| `ranking-per-city` | 2.803 ms | 0.241 ms | 8 | 166 | seq scan |
| `ratings-rollup` (current) | **2.548 ms** | 0.239 ms | 8 | 166 | seq scan |
| `ratings-rollup` (original shape) | 5.527 ms | 0.190 ms | 8 | 165 | seq scan |
| `location-radius` | 0.022 ms | 0.038 ms | 2 | 3 | `idx_restaurants_location` |
| `location-knn` | 0.015 ms | 0.019 ms | 5 | 2 | seq scan |
| `customer-bookings` | 0.028 ms | 0.025 ms | 20 | 21 | `idx_reservations_customer_recent` |
| `slot-demand` | 7.207 ms | 0.225 ms | 28 | 127 | seq scan |

---

## 3. Finding 1 — the availability query is served by the constraint's own index

This is the query the lab asks us to optimize, and the interesting part is that
**it needed no new index**.

```
with the index:     0.082 ms,  18 buffers   [reservations_no_double_booking]
index disabled:     1.342 ms, 127 buffers   [seq scan]
```

Roughly **16–18× faster across runs, and 7× fewer pages touched.** The buffer
ratio is the number to trust; the timing ratio moved between 16.4× and 18.3× on
repeat runs.

The index doing the work is not one anybody added for reads. It is the GiST
index Postgres created automatically to enforce:

```sql
EXCLUDE USING gist (table_id WITH =, during WITH &&)
    WHERE (status IN ('pending','confirmed','seated','completed'))
```

The availability CTE's anti-join asks exactly the question that index answers —
"is there a blocking reservation on this table overlapping this range?" — so the
correctness structure and the read path are the same structure. `04_indexes.sql`
predicted this in a comment; the measurement confirms it.

**Consequence:** there is no separate availability index to maintain, and none
should be added. A second index on `(table_id, during)` would duplicate the
first, cost writes, and win nothing.

## 4. Finding 2 — planning costs more than execution on the hot path

```
availability-cte:   execution 0.082 ms,  planning 0.218 ms
```

**Planning is ~2.7× the execution time.** The query is a three-CTE statement with
six bind parameters; the planner's work does not shrink just because the data
does.

This is the single most actionable result here, and it is invisible unless you
look at planning time separately — which is why `scripts/explain.py` prints it
as its own column.

Two ways to collect it, neither implemented yet and both deliberately deferred:

1. **Prepared statements.** `PREPARE` once per connection, `EXECUTE` per booking.
   After five executions Postgres will consider a generic plan and skip planning
   entirely. The catch is that a generic plan cannot use the actual parameter
   values for selectivity estimates, which for a query whose `WHERE` clauses are
   half `IS NULL` guards could pick a worse plan. It needs measuring before
   adopting, not assuming.
2. **The Redis cache** (Day 3) removes planning and execution together for repeat
   lookups. Measured client-side, median of 200 calls through
   `find_available_tables`:

   ```
   Postgres round trip   1.866 ms
   Redis hit             0.812 ms      2.3x
   ```

   Note what that comparison exposes: the database itself accounts for only
   ~0.30 ms of the 1.866 ms (0.082 execution + 0.218 planning). The remaining
   ~1.5 ms is connection checkout, psycopg2 round trip, and row mapping. **The
   cache is not saving database time — it is saving driver and network time**,
   and Redis costs 0.8 ms of its own to do it. A 2.3× win, not the order of
   magnitude a cache is usually assumed to buy.

The honest summary: at seed scale this query is already fast enough that
optimizing it further is not where the time should go. The finding is recorded
because it will matter at a volume where planning stays flat and execution grows.

## 5. Finding 3 — the ratings rollup was both slow and wrong

`v_restaurant_ratings` originally LEFT JOINed the whole chain and aggregated at
the end:

```sql
FROM restaurants r
LEFT JOIN restaurant_tables rt  ON ...
LEFT JOIN reservations      res ON ...
LEFT JOIN reviews           rv  ON ...
GROUP BY r.restaurant_id, ...
```

That fans out to **one row per reservation (9,517)** in order to compute an
aggregate over **reviews (2,648)**. Rewriting it to aggregate reviews first and
then join the eight-row result to `restaurants`:

```
original shape:   5.527 ms
current:          2.548 ms      2.1x faster
```

Buffers barely moved (165 → 166) — both plans read the same tables. The win is
entirely in how much intermediate data is carried through the joins.

**The rewrite also fixed a correctness bug**, which is the more important half.
With `reviews` LEFT JOINed last, reservations carrying *no* review still
contributed their `during` to `max()`, so `last_reviewed_visit` reported the last
**reservation** — frequently a future booking. Measured before the fix:

| restaurant | view reported | actual last reviewed visit |
|---|---|---|
| Fog Line Kitchen | 2026-08-18 22:45 | 2026-08-03 22:45 |
| Harbor & Vine | 2026-08-18 23:00 | 2026-08-03 23:00 |

Two weeks wrong, in a column whose name states the opposite. Starting from
`reviews` makes the error unrepresentable rather than merely corrected.
`test_last_reviewed_visit_counts_only_reviewed_visits` is the regression test.

> Worth generalising: the bug and the slowness had the same root cause. Joining
> down to the thing you are aggregating and grouping at the end both carries too
> much data *and* silently widens what the aggregate sees.

## 6. Finding 4 — write the predicate the index can use

`ST_DWithin(location, point, 5000)` and `ST_Distance(location, point) <= 5000`
return identical answers. Only one is indexable. Measured on 50,000 points:

```
ST_DWithin(...)            0.09 ms   [GiST index scan]
ST_Distance(...) <= 5000  64.36 ms   [seq scan]
```

**~700× apart, for the same result.** The planner cannot bound the output of a
function with a GiST index, so the second form must compute a spheroid distance
for every row in the table. No amount of indexing fixes a predicate written this
way — this is a query-shape problem, not a schema problem.

The same applies to ordering. `ORDER BY location <-> point LIMIT 5` walks the
index in distance order and stops after five rows (0.17 ms at 50k). `ORDER BY
ST_Distance(...) LIMIT 5` computes 50,000 distances and sorts them.

## 7. Finding 5 — most of these indexes do not pay at seed scale, and that is correct

Six of the ten indexes in `04_indexes.sql` are ignored by the planner on the
seed dataset. `restaurants` is 8 rows in 2 pages; `restaurant_tables` is 96 rows
in 1 page. A sequential scan of one page beats any index lookup, and the planner
choosing it is the planner being right.

That makes the seed useless for demonstrating what `idx_restaurants_location` is
*for*, so `scripts/explain.py --scale` builds a 50,000-point table and measures
the crossover directly:

```
50,000 points, ST_DWithin within 5 km:

  sequential scan     61–99 ms      417 buffers
  GiST index scan       0.09 ms      26 buffers

  ~700x faster, 16x fewer pages
```

(The sequential-scan figure moved between 61 ms and 99 ms across runs — see the
caveat in §1. The index figure was stable at 0.09–0.12 ms.)

**The lesson is about method, not about GiST.** An index that looks useless on a
development dataset can be the difference between 0.1 ms and 100 ms in
production, and an index that looks essential can be dead weight. The only way to
know is to measure at a representative size — which is what the `--scale` mode
exists to make cheap.

Two indexes *do* earn their place at seed scale, and the measurements say why:

- `reservations_no_double_booking` — §3, on 9,517 rows.
- `idx_reservations_customer_recent` — `customer-bookings` at 0.028 ms / 21
  buffers. Without it, "my bookings" is a 9,517-row scan, and every customer
  delete becomes one too, since Postgres does not auto-index foreign keys.

`location-radius` also chose `idx_restaurants_location` at 8 rows, which is
mildly surprising. The reason is that `ST_DWithin` on `geography` is expensive
enough per row that the index's cheap bounding-box prefilter wins even on a
single page.

---

## 8. What was deliberately not done

- **No materialized view for the ratings rollup.** At 2.5 ms it is not the
  bottleneck, and a materialized view introduces a refresh policy and a staleness
  window in exchange for milliseconds. `docs/er-diagram.md` names this as the
  escalation path if the rollup ever dominates; the measurement says it does not.
- **No prepared statements.** See §4 — the win is real but a generic plan could
  regress this particular query, and that needs measuring first.
- **No index on `v_slot_demand`'s grouping columns.** It is the slowest probe
  (7.2 ms) and it is a reporting query over the full reservations table. An
  expression index on `EXTRACT(dow ...)` would help a full scan not at all, since
  the query reads every row by definition. If it needs to be fast, it needs to be
  pre-aggregated, not indexed.
- **No partitioning of `reservations`.** At 9.5k rows this would be pure
  ceremony. It becomes a real question somewhere north of a few million, where
  pruning by time range starts to matter.

## 9. Reproducing

```bash
./scripts/db.sh reset                                  # known dataset
.venv/bin/python scripts/explain.py -n 15              # the table in §2
.venv/bin/python scripts/explain.py --scale            # the crossover in §7
.venv/bin/python scripts/explain.py -k availability    # one probe
```

The `--scale` study creates and drops `scale_restaurants` inside the run; it uses
`setseed(0.17)` so the generated points — and therefore the measurement — are the
same on every run.