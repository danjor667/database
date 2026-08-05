-- =============================================================================
-- 04_indexes.sql — access paths
-- =============================================================================
-- Every index here is justified by a query the lab actually runs, and every one
-- has since been measured -- see docs/performance.md. Two carry the load at seed
-- volume (the exclusion constraint's GiST index, and
-- idx_reservations_customer_recent); the rest are correctly ignored by the
-- planner until the tables are larger, which scripts/explain.py --scale
-- demonstrates.
--
-- Already created implicitly and therefore NOT repeated below:
--   * primary keys and UNIQUE constraints (btree)
--   * reservations_no_double_booking  -> GiST on (table_id, during), which is
--     also the index that serves the availability anti-join.

-- -----------------------------------------------------------------------------
-- Availability: table capacity + time slot
-- -----------------------------------------------------------------------------
-- Drives the first leg of the availability CTE: "active tables at restaurant X
-- that can seat a party of N". Partial on is_active because closed tables are
-- never candidates, and seats trails restaurant_id so the range predicate
-- (seats >= :party_size) can be applied inside the index scan.
CREATE INDEX IF NOT EXISTS idx_tables_restaurant_capacity
    ON restaurant_tables (restaurant_id, seats, min_party_size)
    WHERE is_active;

-- Start-time access for operational reads ("tonight's covers", the Redis
-- cache-warming job) that filter on time without knowing a table_id, so the
-- GiST constraint index cannot help. Partial on the blocking statuses keeps it
-- small and matches the availability predicate exactly.
CREATE INDEX IF NOT EXISTS idx_reservations_starts_at
    ON reservations (lower(during))
    WHERE status IN ('pending', 'confirmed', 'seated', 'completed');

-- "My bookings", newest first. Also the FK-supporting index on customer_id:
-- Postgres does not create one automatically, and without it every customer
-- delete degenerates into a seq scan of reservations.
CREATE INDEX IF NOT EXISTS idx_reservations_customer_recent
    ON reservations (customer_id, lower(during) DESC);


-- -----------------------------------------------------------------------------
-- Location-based search
-- -----------------------------------------------------------------------------
-- GiST over the geography column. Serves both radius search
-- (ST_DWithin(location, :point, :metres)) and nearest-neighbour ordering
-- (ORDER BY location <-> :point LIMIT k) as an index scan.
CREATE INDEX IF NOT EXISTS idx_restaurants_location
    ON restaurants USING gist (location);

-- Per-city listing and the grouping key for the RANK() analytics.
CREATE INDEX IF NOT EXISTS idx_restaurants_city
    ON restaurants (city_id)
    WHERE is_active;


-- -----------------------------------------------------------------------------
-- JSONB and text search
-- -----------------------------------------------------------------------------
-- jsonb_path_ops is roughly half the size of the default jsonb_ops and is the
-- right choice here because menu lookups are always containment questions
-- ("has an item tagged vegan"), never "does this key exist anywhere".
CREATE INDEX IF NOT EXISTS idx_restaurants_menu
    ON restaurants USING gin (menu jsonb_path_ops);

-- Opening-hours probes are key lookups by weekday, so the default operator
-- class (which indexes keys as well as values) is the correct one.
CREATE INDEX IF NOT EXISTS idx_restaurants_opening_hours
    ON restaurants USING gin (opening_hours);

-- Fuzzy name search: supports ILIKE '%pattern%', which no btree can serve.
CREATE INDEX IF NOT EXISTS idx_restaurants_name_trgm
    ON restaurants USING gin (name gin_trgm_ops);


-- -----------------------------------------------------------------------------
-- Reviews
-- -----------------------------------------------------------------------------
-- Covering index for the ratings rollup: the aggregate reads only these two
-- columns, so it can be answered index-only without touching the heap.
CREATE INDEX IF NOT EXISTS idx_reviews_reservation_rating
    ON reviews (reservation_id, rating);


-- FK support for restaurant_cuisines' second leg (the PK covers restaurant_id).
CREATE INDEX IF NOT EXISTS idx_restaurant_cuisines_cuisine
    ON restaurant_cuisines (cuisine_id);


-- Fresh statistics so the first EXPLAIN ANALYZE after a load is meaningful.
ANALYZE;