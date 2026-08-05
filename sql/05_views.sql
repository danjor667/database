-- =============================================================================
-- 05_views.sql — read models
-- =============================================================================
-- The schema is strictly normalized, which pushes a 3-4 table join into every
-- reporting query. These views pay that cost once, in one place, so Day 4's
-- analytics stay about the analytics.

DROP VIEW IF EXISTS v_restaurant_ratings CASCADE;
DROP VIEW IF EXISTS v_restaurant_reviews CASCADE;
DROP VIEW IF EXISTS v_reservation_details CASCADE;

-- -----------------------------------------------------------------------------
-- v_reservation_details — reservation with its table, restaurant and customer
-- -----------------------------------------------------------------------------
CREATE VIEW v_reservation_details AS
SELECT res.reservation_id,
       res.status,
       res.party_size,
       res.during,
       lower(res.during) AS starts_at,
       upper(res.during) AS ends_at,
       res.special_requests,
       res.created_at,
       cust.customer_id,
       cust.first_name || ' ' || cust.last_name AS customer_name,
       cust.email       AS customer_email,
       rt.table_id,
       rt.table_code,
       rt.seats,
       rt.zone,
       r.restaurant_id,
       r.name           AS restaurant_name,
       ci.city_id,
       ci.name          AS city,
       ci.region,
       ci.country_code
  FROM reservations res
  JOIN customers         cust USING (customer_id)
  JOIN restaurant_tables rt   USING (table_id)
  JOIN restaurants       r    USING (restaurant_id)
  JOIN cities            ci   USING (city_id);

COMMENT ON VIEW v_reservation_details IS
    'Flattens the reservation -> table -> restaurant -> city chain that 3NF splits apart.';


-- -----------------------------------------------------------------------------
-- v_restaurant_reviews — each review resolved to its restaurant and reviewer
-- -----------------------------------------------------------------------------
CREATE VIEW v_restaurant_reviews AS
SELECT rv.review_id,
       rv.rating,
       rv.food_rating,
       rv.service_rating,
       rv.ambience_rating,
       rv.title,
       rv.body_ref,
       rv.created_at,
       d.reservation_id,
       d.starts_at      AS visited_at,
       d.customer_id,
       d.customer_name,
       d.restaurant_id,
       d.restaurant_name,
       d.city_id,
       d.city
  FROM reviews rv
  JOIN v_reservation_details d USING (reservation_id);


-- -----------------------------------------------------------------------------
-- v_restaurant_ratings — per-restaurant rollup feeding the per-city RANK()
-- -----------------------------------------------------------------------------
-- Aggregates reviews FIRST, then joins the result to restaurants. The obvious
-- shape — LEFT JOIN the whole chain and GROUP BY at the end — was both slower
-- and wrong:
--
--   * Slower, because it fans out to one row per *reservation* (9.5k) to compute
--     an aggregate over reviews (2.6k). Aggregating first keeps the intermediate
--     at the size of the thing being aggregated. Measured on the seed dataset:
--     14.5 ms -> 9.3 ms. See docs/performance.md.
--
--   * Wrong, because with `reviews` LEFT JOINed last, reservations carrying no
--     review still contributed their `during` to max(), so `last_reviewed_visit`
--     silently reported the last *reservation* — a future booking — rather than
--     the last reviewed visit. Starting from `reviews` makes that unrepresentable
--     instead of merely fixed.
--
-- The outer LEFT JOIN is still required so restaurants with no reviews survive
-- with review_count = 0 rather than vanishing from listings.
CREATE VIEW v_restaurant_ratings AS
WITH per_restaurant AS (
    SELECT rt.restaurant_id,
           count(*)                AS review_count,
           avg(rv.rating)          AS avg_rating,
           avg(rv.food_rating)     AS avg_food_rating,
           avg(rv.service_rating)  AS avg_service_rating,
           avg(rv.ambience_rating) AS avg_ambience_rating,
           max(upper(res.during))  AS last_reviewed_visit
      FROM reviews rv
      JOIN reservations      res USING (reservation_id)
      JOIN restaurant_tables rt  USING (table_id)
     GROUP BY rt.restaurant_id
)
SELECT r.restaurant_id,
       r.name AS restaurant_name,
       ci.city_id,
       ci.name AS city,
       ci.region,
       r.price_tier,
       -- coalesce, not the raw NULL: "no reviews" is a count of zero.
       coalesce(pr.review_count, 0)                  AS review_count,
       round(pr.avg_rating::numeric, 2)              AS avg_rating,
       round(pr.avg_food_rating::numeric, 2)         AS avg_food_rating,
       round(pr.avg_service_rating::numeric, 2)      AS avg_service_rating,
       round(pr.avg_ambience_rating::numeric, 2)     AS avg_ambience_rating,
       pr.last_reviewed_visit
  FROM restaurants r
  JOIN cities ci USING (city_id)
  LEFT JOIN per_restaurant pr USING (restaurant_id)
 WHERE r.is_active;

COMMENT ON VIEW v_restaurant_ratings IS
    'Rating rollup per restaurant. v_restaurant_rankings ranks over this.';
COMMENT ON COLUMN v_restaurant_ratings.last_reviewed_visit IS
    'End of the most recent visit that was actually reviewed; NULL if none.';