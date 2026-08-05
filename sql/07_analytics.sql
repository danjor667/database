-- =============================================================================
-- 07_analytics.sql — window-function read models
-- =============================================================================
-- Day 4. Applied after 05_views.sql (see SCHEMA_FILES in scripts/db.sh); the
-- number only reflects apply order, and 06 is the seed rather than schema.
--
-- Measured behaviour of everything here is in docs/performance.md.

DROP VIEW IF EXISTS v_restaurant_rankings CASCADE;


-- -----------------------------------------------------------------------------
-- v_restaurant_rankings — restaurants ranked by rating, within their city
-- -----------------------------------------------------------------------------
-- The assignment's headline query: "rank restaurants by average review score per
-- city". The partition is the city, so every city's ranking restarts at 1 and a
-- one-restaurant city trivially has a single rank-1 entry (Austin, in the seed).
--
-- Three ranking functions are exposed because they answer different questions,
-- and the difference is only observable on ties:
--
--   avg_rating | RANK | DENSE_RANK | ROW_NUMBER
--   -----------+------+------------+-----------
--        4.40  |   1  |     1      |     1
--        4.20  |   2  |     2      |     2
--        4.20  |   2  |     2      |     3      <- tie broken arbitrarily
--        4.10  |   4  |     3      |     4      <- RANK skips, DENSE_RANK does not
--
-- RANK is the honest default for a leaderboard: two restaurants genuinely tied
-- for 2nd means nobody is 3rd. ROW_NUMBER is only appropriate when an arbitrary
-- but stable total order is required (pagination), which is why its ORDER BY
-- carries an explicit tiebreak — without one, the same query can return
-- different numbering between runs.
--
-- NOTE ON THE SEED: no two restaurants in a city share a rounded rating, so all
-- three columns are identical in *this* view on the shipped dataset. Ties do
-- occur in v_slot_demand below — several restaurants have two equally busy
-- slots — which is where the difference is observable on real data.
-- tests/test_analytics.py checks both.
--
-- Restaurants with no reviews sort last (NULLS LAST) rather than first, which is
-- the Postgres default for DESC and would otherwise put unrated restaurants at
-- the top of every city.
CREATE VIEW v_restaurant_rankings AS
SELECT ra.restaurant_id,
       ra.restaurant_name,
       ra.city_id,
       ra.city,
       ra.region,
       ra.price_tier,
       ra.review_count,
       ra.avg_rating,
       ra.avg_food_rating,
       ra.avg_service_rating,
       ra.avg_ambience_rating,
       ra.last_reviewed_visit,

       RANK()       OVER w  AS city_rank,
       DENSE_RANK() OVER w  AS city_dense_rank,
       ROW_NUMBER() OVER (PARTITION BY ra.city_id
                          ORDER BY ra.avg_rating DESC NULLS LAST,
                                   ra.review_count DESC,
                                   ra.restaurant_id)   AS city_row_number,

       count(*)     OVER (PARTITION BY ra.city_id)     AS city_restaurant_count,

       -- The city's own mean, repeated on every row. This is what a window
       -- function buys over a GROUP BY: the aggregate is available *alongside*
       -- the detail rows instead of collapsing them.
       round(avg(ra.avg_rating) OVER (PARTITION BY ra.city_id), 2)
                                                       AS city_avg_rating,
       round(ra.avg_rating - avg(ra.avg_rating) OVER (PARTITION BY ra.city_id), 2)
                                                       AS vs_city_avg,

       -- Relative standing within the city, 0.0 (best) to 1.0 (worst).
       -- Defined as (rank - 1) / (rows - 1), so a single-restaurant city is 0.
       round(PERCENT_RANK() OVER w :: numeric, 3)      AS city_percentile,

       -- Share of the city's reviews. Reveals when a high rating rests on a
       -- thin sample relative to its neighbours.
       round(100.0 * ra.review_count
             / NULLIF(sum(ra.review_count) OVER (PARTITION BY ra.city_id), 0), 1)
                                                       AS pct_of_city_reviews
  FROM v_restaurant_ratings ra
WINDOW w AS (PARTITION BY ra.city_id ORDER BY ra.avg_rating DESC NULLS LAST);

COMMENT ON VIEW v_restaurant_rankings IS
    'Per-city leaderboard. RANK/DENSE_RANK/ROW_NUMBER differ only on ties.';
COMMENT ON COLUMN v_restaurant_rankings.city_rank IS
    'RANK() within city by avg_rating DESC; ties share a rank and skip the next.';
COMMENT ON COLUMN v_restaurant_rankings.vs_city_avg IS
    'avg_rating minus the city mean; positive means above local average.';


-- -----------------------------------------------------------------------------
-- v_slot_demand — how heavily each (restaurant, weekday, hour) is booked
-- -----------------------------------------------------------------------------
-- Feeds two things: an operational "when are we full?" report, and the Redis
-- cache-warming job, which wants the busiest slots first.
--
-- Only blocking statuses count as demand — a cancelled booking did not consume
-- the table. That list matches the exclusion constraint and the availability
-- CTE; all three must agree.
CREATE VIEW v_slot_demand AS
SELECT rt.restaurant_id,
       r.name                                   AS restaurant_name,
       EXTRACT(dow  FROM lower(res.during))::int AS weekday,       -- 0 = Sunday
       EXTRACT(hour FROM lower(res.during))::int AS hour_of_day,
       count(*)                                 AS bookings,
       sum(res.party_size)                      AS covers,
       round(avg(res.party_size)::numeric, 1)   AS avg_party_size,

       -- Rank each slot against the same restaurant's other slots.
       RANK() OVER (PARTITION BY rt.restaurant_id ORDER BY count(*) DESC)
                                                AS demand_rank,

       -- Share of the restaurant's total bookings. Window functions may be
       -- applied over the result of an aggregate, which is why count(*) can
       -- appear inside sum() OVER here without a subquery.
       round(100.0 * count(*)
             / sum(count(*)) OVER (PARTITION BY rt.restaurant_id), 1)
                                                AS pct_of_bookings
  FROM reservations res
  JOIN restaurant_tables rt USING (table_id)
  JOIN restaurants r        USING (restaurant_id)
 WHERE res.status IN ('pending', 'confirmed', 'seated', 'completed')
 GROUP BY rt.restaurant_id, r.name,
          EXTRACT(dow FROM lower(res.during)),
          EXTRACT(hour FROM lower(res.during));

COMMENT ON VIEW v_slot_demand IS
    'Booking volume per restaurant/weekday/hour, ranked within each restaurant.';