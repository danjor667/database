-- =============================================================================
-- 98_explain.sql — index vs no index, query by query
-- =============================================================================
-- Read-only: every statement is an EXPLAIN over a SELECT, plus the planner
-- switches. Plain SQL, no psql backslash commands, so it runs anywhere.
--
--   ./scripts/db.sh psql -f /sql/98_explain.sql     # all of it at once
--
-- In an IDE console, run it one statement at a time (⌘⏎ / Ctrl+Enter). Every
-- query below follows the SAME three steps:
--
--   a.  EXPLAIN ANALYZE the query              <- indexes available
--   b.  turn the index paths off, EXPLAIN ANALYZE the same query again
--   c.  turn the index paths back on           <- always run this
--
-- Nothing is dropped or created. `SET enable_indexscan = off` only tells the
-- planner it may not use that access path; the indexes stay exactly where they
-- are. That is why this is safe, and why step (c) fully undoes step (b).
--
-- All three switches are needed in step (b). Turn off only enable_indexscan and
-- the planner reaches for a bitmap scan instead, and you have measured nothing.
--
-- IF YOU LOSE YOUR PLACE: run the RESET block immediately below. Leaving a
-- session with index scans disabled will quietly distort every query you run
-- afterwards, including ones that have nothing to do with this file.
--
--
-- READING THE OUTPUT — the four things worth comparing between (a) and (b):
--
--   node type   `Index Scan` / `Index Only Scan` / `Bitmap Index Scan`
--               versus `Seq Scan`.
--   Index Cond: the condition was handed to the index; only matches were read.
--   Filter: + `Rows Removed by Filter: N`
--               every row was read and then thrown away. N is the wasted work.
--   Buffers:    pages touched. The stablest signal — it barely moves between
--               runs, whereas milliseconds on a laptop swing with whatever else
--               the machine is doing. On a dataset this small, trust buffers and
--               row counts over the clock.
--
-- In a node line, `(cost=...)` is the planner's guess and `(actual ...)` is what
-- happened. `loops=N` means the node ran N times and `actual time` is PER LOOP.
--
-- RUN EACH EXPLAIN TWICE AND READ THE SECOND. The first execution of anything
-- pays for cache and catalog warm-up that has nothing to do with the query —
-- enough to make step (a) look slower than step (b) and invert the whole
-- comparison. scripts/explain.py discards a warm-up run and takes the median of
-- the rest for exactly this reason; doing it by hand here means running it
-- twice. Buffers are not affected and can be read on the first run.
--
--
-- IF EVERY QUERY BELOW REPORTS `Seq Scan` NO MATTER WHAT YOU SET:
-- check for invalid indexes. An index marked invalid still enforces its
-- constraint but the planner will not use it for reads, so index-vs-no-index
-- comparisons silently become no-op-vs-no-op.
--
--     SELECT c.relname, i.indisvalid
--       FROM pg_index i
--       JOIN pg_class c ON c.oid = i.indexrelid
--       JOIN pg_class t ON t.oid = i.indrelid
--      WHERE t.relnamespace = 'public'::regnamespace AND NOT i.indisvalid;
--
-- Any row returned is a problem. Repair with `REINDEX TABLE <table>;` — it
-- rebuilds the index and clears the flag without touching data. Note that
-- `REINDEX` restores whatever definition the catalog holds, so if an index was
-- also recreated with the wrong columns, re-run its CREATE INDEX from
-- sql/04_indexes.sql afterwards.
--
-- Do NOT use `UPDATE pg_index SET indisvalid = false` to simulate a missing
-- index. It is a catalog hack that leaves the database in exactly this state.
-- The `SET enable_*` switches used below are the supported way, and they are
-- per-session and instantly reversible.
-- =============================================================================


-- =============================================================================
-- RESET — run this any time, and always when you are finished
-- =============================================================================

SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;

-- Confirm. All three must read 'on' before you trust anything below.
SELECT name, setting
  FROM pg_settings
 WHERE name IN ('enable_indexscan', 'enable_bitmapscan', 'enable_indexonlyscan')
 ORDER BY name;



-- #############################################################################
-- QUERY 1 — availability: is this table free at this time?
-- app/availability.py:AVAILABILITY_SQL, parameters filled in.
-- EXPECT A BIG DIFFERENCE: ~20x slower and ~7x more pages without the index.
-- #############################################################################

-- 1a. -------------------------------------------------------- indexes ON ----
-- Look for: Index Only Scan using reservations_no_double_booking, loops=7,
-- Heap Fetches: 0. That index is the one Postgres built to ENFORCE the
-- exclusion constraint — nobody added it for reads.

EXPLAIN (ANALYZE, BUFFERS)
WITH req AS (
    SELECT r.restaurant_id, 4::smallint AS party_size,
           tstzrange(((current_date + 7) + time '19:00')::timestamptz,
                     ((current_date + 7) + time '19:00')::timestamptz
                         + make_interval(mins => COALESCE(NULL::int, r.slot_minutes)),
                     '[)') AS during
      FROM restaurants r
     WHERE r.restaurant_id = 1 AND r.is_active
),
free AS (
    SELECT rt.table_id, rt.table_code, rt.seats, rt.min_party_size, rt.zone,
           req.during, req.party_size
      FROM req
      JOIN restaurant_tables rt
        ON rt.restaurant_id = req.restaurant_id
       AND rt.is_active
       AND req.party_size BETWEEN rt.min_party_size AND rt.seats
     WHERE (NULL::table_zone IS NULL OR rt.zone = NULL::table_zone)
       AND (NULL::int IS NULL OR rt.table_id = NULL::int)
       AND NOT EXISTS (
               SELECT 1 FROM reservations res
                WHERE res.table_id = rt.table_id
                  AND res.during && req.during
                  AND res.status IN ('pending', 'confirmed', 'seated', 'completed')
           )
)
SELECT table_id, table_code, seats, min_party_size, zone,
       lower(during) AS starts_at, upper(during) AS ends_at
  FROM free
 ORDER BY seats, table_id;


-- 1b. ------------------------------------------------------- indexes OFF ----
SET enable_indexscan     = off;
SET enable_bitmapscan    = off;
SET enable_indexonlyscan = off;

-- Same query. Now the anti-join must scan reservations once per candidate
-- table instead of probing the GiST index.

EXPLAIN (ANALYZE, BUFFERS)
WITH req AS (
    SELECT r.restaurant_id, 4::smallint AS party_size,
           tstzrange(((current_date + 7) + time '19:00')::timestamptz,
                     ((current_date + 7) + time '19:00')::timestamptz
                         + make_interval(mins => COALESCE(NULL::int, r.slot_minutes)),
                     '[)') AS during
      FROM restaurants r
     WHERE r.restaurant_id = 1 AND r.is_active
),
free AS (
    SELECT rt.table_id, rt.table_code, rt.seats, rt.min_party_size, rt.zone,
           req.during, req.party_size
      FROM req
      JOIN restaurant_tables rt
        ON rt.restaurant_id = req.restaurant_id
       AND rt.is_active
       AND req.party_size BETWEEN rt.min_party_size AND rt.seats
     WHERE (NULL::table_zone IS NULL OR rt.zone = NULL::table_zone)
       AND (NULL::int IS NULL OR rt.table_id = NULL::int)
       AND NOT EXISTS (
               SELECT 1 FROM reservations res
                WHERE res.table_id = rt.table_id
                  AND res.during && req.during
                  AND res.status IN ('pending', 'confirmed', 'seated', 'completed')
           )
)
SELECT table_id, table_code, seats, min_party_size, zone,
       lower(during) AS starts_at, upper(during) AS ends_at
  FROM free
 ORDER BY seats, table_id;


-- 1c. -------------------------------------------------------- indexes ON ----
SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;



-- #############################################################################
-- QUERY 2 — a customer's bookings, newest first
-- app/reservations.py:_LIST_SQL. Served by idx_reservations_customer_recent.
-- EXPECT A MODEST TIME DIFFERENCE BUT A HUGE WORK DIFFERENCE: watch the
-- `Rows Removed by Filter` line appear in 2b.
-- #############################################################################

-- 2a. -------------------------------------------------------- indexes ON ----
EXPLAIN (ANALYZE, BUFFERS)
SELECT reservation_id, during, status
  FROM reservations
 WHERE customer_id = 1
 ORDER BY lower(during) DESC
 LIMIT 20;


-- 2b. ------------------------------------------------------- indexes OFF ----
SET enable_indexscan     = off;
SET enable_bitmapscan    = off;
SET enable_indexonlyscan = off;

-- `Rows Removed by Filter: ~9357` — every reservation in the table was read and
-- discarded to return 168. The clock barely moves because 9.5k rows fit in RAM;
-- the work done is 50x larger regardless.

EXPLAIN (ANALYZE, BUFFERS)
SELECT reservation_id, during, status
  FROM reservations
 WHERE customer_id = 1
 ORDER BY lower(during) DESC
 LIMIT 20;


-- 2c. -------------------------------------------------------- indexes ON ----
SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;



-- #############################################################################
-- QUERY 3 — restaurants within 5 km of a point
-- app/analytics.py:_NEARBY_SQL. Served by idx_restaurants_location (GiST).
-- EXPECT THE ACCESS PATH TO CHANGE, BUT NOT THE CLOCK — see the note after 3c.
-- #############################################################################

-- 3a. -------------------------------------------------------- indexes ON ----
EXPLAIN (ANALYZE, BUFFERS)
SELECT restaurant_id, name
  FROM restaurants
 WHERE ST_DWithin(location, ST_MakePoint(-73.9855, 40.7580)::geography, 5000);


-- 3b. ------------------------------------------------------- indexes OFF ----
SET enable_indexscan     = off;
SET enable_bitmapscan    = off;
SET enable_indexonlyscan = off;

EXPLAIN (ANALYZE, BUFFERS)
SELECT restaurant_id, name
  FROM restaurants
 WHERE ST_DWithin(location, ST_MakePoint(-73.9855, 40.7580)::geography, 5000);


-- 3c. -------------------------------------------------------- indexes ON ----
SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;

-- NOTE: (3b) will very likely be FASTER than (3a), which is not a mistake.
-- There are 8 restaurants in 2 pages; reading the GiST index costs more pages
-- than reading the whole table. The planner still picks the index in (3a)
-- because ST_DWithin on geography is expensive per row, so its cost model
-- expects the index prefilter to pay off. At 50,000 rows it does, enormously —
-- run `scripts/explain.py --scale` to see 0.08 ms against 62 ms.
--
-- Worth seeing in (3a): the index cannot evaluate ST_DWithin directly. It
-- splits into `Index Cond: (location && _st_expand(point, 5000))`, a cheap
-- bounding-box test, then `Filter: st_dwithin(...)` for the exact answer.



-- #############################################################################
-- QUERY 4 — the 5 nearest restaurants (KNN, no radius)
-- app/analytics.py:_NEAREST_SQL. The `<->` operator.
-- EXPECT NO DIFFERENCE AT THIS SIZE: 8 rows are cheaper to sort than to walk.
-- #############################################################################

-- 4a. -------------------------------------------------------- indexes ON ----
EXPLAIN (ANALYZE, BUFFERS)
SELECT restaurant_id, name
  FROM restaurants
 ORDER BY location <-> ST_MakePoint(-73.9855, 40.7580)::geography
 LIMIT 5;


-- 4b. ------------------------------------------------------- indexes OFF ----
SET enable_indexscan     = off;
SET enable_bitmapscan    = off;
SET enable_indexonlyscan = off;

EXPLAIN (ANALYZE, BUFFERS)
SELECT restaurant_id, name
  FROM restaurants
 ORDER BY location <-> ST_MakePoint(-73.9855, 40.7580)::geography
 LIMIT 5;


-- 4c. -------------------------------------------------------- indexes ON ----
SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;

-- Both plans are a Seq Scan + Sort. The index is never used either way, so
-- disabling it changes nothing. At 50k rows (4a) becomes an index walk that
-- stops after 5 rows, and the gap opens up.



-- #############################################################################
-- QUERY 5 — per-city restaurant leaderboard
-- app/analytics.py:_RANKING_SQL, over v_restaurant_rankings.
-- EXPECT NO DIFFERENCE: this reads every restaurant by definition.
-- #############################################################################

-- 5a. -------------------------------------------------------- indexes ON ----
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM v_restaurant_rankings;


-- 5b. ------------------------------------------------------- indexes OFF ----
SET enable_indexscan     = off;
SET enable_bitmapscan    = off;
SET enable_indexonlyscan = off;

EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM v_restaurant_rankings;


-- 5c. -------------------------------------------------------- indexes ON ----
SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;

-- An index helps you FIND a few rows among many. This query wants all of them,
-- ranked. There is nothing for an index to narrow, which is why no index in
-- 04_indexes.sql targets it and none should.



-- #############################################################################
-- QUERY 6 — busiest (weekday, hour) slots
-- app/analytics.py:_SLOT_DEMAND_SQL, over v_slot_demand.
-- EXPECT NO DIFFERENCE, for the same reason as query 5. This is the slowest
-- query in the app (~7 ms) and indexing is not the fix — pre-aggregation is.
-- #############################################################################

-- 6a. -------------------------------------------------------- indexes ON ----
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM v_slot_demand WHERE demand_rank <= 3;


-- 6b. ------------------------------------------------------- indexes OFF ----
SET enable_indexscan     = off;
SET enable_bitmapscan    = off;
SET enable_indexonlyscan = off;

EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM v_slot_demand WHERE demand_rank <= 3;


-- 6c. -------------------------------------------------------- indexes ON ----
SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;



-- #############################################################################
-- QUERY 7 — the ratings rollup, two ways
-- NOT an index comparison. Same data, same indexes, two query SHAPES.
-- Included because it is the largest win in this file (2x) and no index
-- produces it. Both run with indexes ON.
-- #############################################################################

-- 7a. ---------------------------------------- aggregate reviews, then join ---
-- The current v_restaurant_ratings (sql/05_views.sql).

EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM v_restaurant_ratings;


-- 7b. ---------------------------------------- join everything, then group ----
-- The shape it replaced.

EXPLAIN (ANALYZE, BUFFERS)
SELECT r.restaurant_id,
       count(rv.review_id)    AS review_count,
       avg(rv.rating)         AS avg_rating,
       max(upper(res.during)) AS last_visit
  FROM restaurants r
  LEFT JOIN restaurant_tables rt  ON rt.restaurant_id  = r.restaurant_id
  LEFT JOIN reservations      res ON res.table_id      = rt.table_id
  LEFT JOIN reviews           rv  ON rv.reservation_id = res.reservation_id
 WHERE r.is_active
 GROUP BY r.restaurant_id;

-- (7b) fans out to one row per RESERVATION (~9.5k) to compute an aggregate over
-- REVIEWS (~2.6k). Buffers barely differ — both read the same tables — so the
-- whole cost is intermediate rows carried through the joins.
--
-- (7b) was also WRONG: with reviews joined last, un-reviewed reservations still
-- fed max(), so last_reviewed_visit reported the last reservation, often a
-- future booking. Aggregating first makes that unrepresentable.
-- See sql/05_views.sql and docs/performance.md §5.



-- =============================================================================
-- FINISH — leave the session as you found it
-- =============================================================================

SET enable_indexscan     = on;
SET enable_bitmapscan    = on;
SET enable_indexonlyscan = on;

SELECT name, setting
  FROM pg_settings
 WHERE name IN ('enable_indexscan', 'enable_bitmapscan', 'enable_indexonlyscan')
 ORDER BY name;


-- =============================================================================
-- WHY SO MANY OF THESE SHOW NO DIFFERENCE
-- =============================================================================
-- Queries 4, 5 and 6 look identical with indexes on and off, and that is the
-- honest result rather than a broken demo. `restaurants` is 8 rows in 2 pages;
-- `restaurant_tables` is 96 rows in 1 page. A sequential scan of one page beats
-- any index lookup, and the planner ignoring an index there is the planner being
-- right.
--
-- Only `reservations` (~9.5k rows) is large enough for an index to matter, which
-- is exactly where queries 1 and 2 do show a difference.
--
-- The lesson is about method: an index that looks useless on a development
-- dataset can be the difference between 0.1 ms and 100 ms in production. Measure
-- at a representative size:
--
--     .venv/bin/python scripts/explain.py -n 15     # every probe, median of 15
--     .venv/bin/python scripts/explain.py --scale   # builds 50k rows, measures
--                                                   # the GiST crossover
--
-- How big each table actually is:

SELECT relname                          AS table_name,
       to_char(reltuples, 'FM999G999')  AS est_rows,
       relpages                         AS pages,
       pg_size_pretty(pg_total_relation_size(oid)) AS total_size
  FROM pg_class
 WHERE relnamespace = 'public'::regnamespace
   AND relkind = 'r'
   AND relname IN ('cities', 'restaurants', 'restaurant_tables', 'cuisines',
                   'restaurant_cuisines', 'customers', 'reservations', 'reviews')
 ORDER BY relpages DESC, relname;


-- And which indexes the planner has actually chosen, ever. idx_scan counts
-- since the last stats reset, across every session that has touched this
-- database — so it includes the test suite and the app, not just this file.

SELECT indexrelname                AS index_name,
       relname                     AS table_name,
       idx_scan                    AS times_used,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
  FROM pg_stat_user_indexes
 WHERE schemaname = 'public'
   AND relname IN ('cities', 'restaurants', 'restaurant_tables', 'cuisines',
                   'restaurant_cuisines', 'customers', 'reservations', 'reviews')
 ORDER BY idx_scan DESC, indexrelname;
