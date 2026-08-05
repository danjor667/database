"""Analytical queries: window functions and location search.

Day 4. Every query here is measured by `scripts/explain.py`, and the numbers
plus the reasoning behind each access path are in `docs/performance.md`.

Three things worth knowing before reading:

* **The heavy lifting is in SQL, not Python.** Ranking, percentages and
  distances are computed by the database and returned finished. Pulling rows
  into Python to rank them there would move the work to the slowest machine in
  the system and discard the index that made it cheap.

* **`geography` means metres.** `restaurants.location` is
  `geography(Point, 4326)`, so `ST_DWithin(location, point, 5000)` really is
  "within 5 km" on a spheroid, not 5000 degrees of something. The cost is that
  `geography` maths is more expensive than planar `geometry`; at city scale that
  trade is obviously right.

* **`ST_MakePoint` takes (longitude, latitude)** — the reverse of how people say
  it. Every function here takes them as keyword-only arguments for that reason.
"""

from __future__ import annotations

from typing import Any, Sequence

from .db import Database
from .models import NearbyRestaurant, RestaurantRanking

# -- ranking ----------------------------------------------------------------

_RANKING_SQL = """
SELECT restaurant_id, restaurant_name, city, region, price_tier,
       review_count, avg_rating, city_rank, city_dense_rank,
       city_restaurant_count, city_avg_rating, vs_city_avg, pct_of_city_reviews
  FROM v_restaurant_rankings
 WHERE (%(city)s::text IS NULL OR city = %(city)s::text)
   AND (%(max_rank)s::int IS NULL OR city_rank <= %(max_rank)s::int)
   AND review_count >= %(min_reviews)s::int
 ORDER BY city, city_rank, restaurant_name
"""

# -- location ---------------------------------------------------------------

# ST_DWithin is the indexable form. The equivalent-looking
# `ST_Distance(...) <= radius` is NOT indexable: the planner cannot use a GiST
# index to bound the output of a function it has to evaluate per row, so it
# degrades to a sequential scan with a distance calculation on every restaurant.
# Distance is still selected, for display — computing it is cheap once the index
# has narrowed the candidates.
_NEARBY_SQL = """
SELECT r.restaurant_id,
       r.name,
       ci.name AS city,
       r.price_tier,
       round(ST_Distance(r.location, search.point)::numeric, 0) AS metres,
       ra.avg_rating,
       ra.review_count
  FROM restaurants r
  JOIN cities ci USING (city_id)
  LEFT JOIN v_restaurant_ratings ra USING (restaurant_id)
 CROSS JOIN LATERAL (
        SELECT ST_MakePoint(%(longitude)s::float8,
                            %(latitude)s::float8)::geography AS point
       ) AS search
 WHERE r.is_active
   AND ST_DWithin(r.location, search.point, %(radius_metres)s::float8)
   AND (%(min_rating)s::numeric IS NULL OR ra.avg_rating >= %(min_rating)s::numeric)
   AND (%(price_tier)s::int IS NULL OR r.price_tier <= %(price_tier)s::int)
 ORDER BY metres
 LIMIT %(limit)s
"""

# `<->` is the KNN distance operator. Given a GiST index, Postgres walks the tree
# in distance order and stops after LIMIT rows — it never computes a distance for
# the whole table, which is what makes "nearest 5" cheap on a large table.
# ORDER BY ST_Distance(...) would compute every distance and then sort.
_NEAREST_SQL = """
SELECT r.restaurant_id,
       r.name,
       ci.name AS city,
       r.price_tier,
       round(ST_Distance(r.location, search.point)::numeric, 0) AS metres,
       ra.avg_rating,
       ra.review_count
  FROM restaurants r
  JOIN cities ci USING (city_id)
  LEFT JOIN v_restaurant_ratings ra USING (restaurant_id)
 CROSS JOIN LATERAL (
        SELECT ST_MakePoint(%(longitude)s::float8,
                            %(latitude)s::float8)::geography AS point
       ) AS search
 WHERE r.is_active
 ORDER BY r.location <-> search.point
 LIMIT %(limit)s
"""

# The payoff query: everything Day 4 built, combined. Geography narrows the
# candidates, the ratings rollup ranks them, and the Day 2 availability CTE
# decides which can actually seat the party — each clause served by the index
# built for it.
_NEARBY_AVAILABLE_SQL = """
WITH search AS (
    SELECT ST_MakePoint(%(longitude)s::float8,
                        %(latitude)s::float8)::geography AS point
),
nearby AS (
    SELECT r.restaurant_id, r.name, r.slot_minutes, r.price_tier,
           ST_Distance(r.location, search.point) AS metres
      FROM restaurants r
     CROSS JOIN search
     WHERE r.is_active
       AND ST_DWithin(r.location, search.point, %(radius_metres)s::float8)
),
free AS (
    SELECT n.restaurant_id,
           count(*) AS free_tables,
           min(rt.seats) AS smallest_fit
      FROM nearby n
      JOIN restaurant_tables rt
        ON rt.restaurant_id = n.restaurant_id
       AND rt.is_active
       AND %(party_size)s::smallint BETWEEN rt.min_party_size AND rt.seats
     WHERE NOT EXISTS (
               SELECT 1
                 FROM reservations res
                WHERE res.table_id = rt.table_id
                  AND res.during && tstzrange(
                          %(starts_at)s::timestamptz,
                          %(starts_at)s::timestamptz
                              + make_interval(mins => n.slot_minutes), '[)')
                  AND res.status IN ('pending','confirmed','seated','completed')
           )
     GROUP BY n.restaurant_id
)
SELECT n.restaurant_id,
       n.name,
       ci.name AS city,
       n.price_tier,
       round(n.metres::numeric, 0) AS metres,
       ra.avg_rating,
       ra.review_count,
       f.free_tables,
       f.smallest_fit,
       RANK() OVER (ORDER BY ra.avg_rating DESC NULLS LAST) AS rating_rank
  FROM nearby n
  JOIN free f USING (restaurant_id)
  JOIN restaurants r USING (restaurant_id)
  JOIN cities ci USING (city_id)
  LEFT JOIN v_restaurant_ratings ra USING (restaurant_id)
 ORDER BY ra.avg_rating DESC NULLS LAST, n.metres
 LIMIT %(limit)s
"""

# -- demand -----------------------------------------------------------------

_SLOT_DEMAND_SQL = """
SELECT restaurant_id, restaurant_name, weekday, hour_of_day,
       bookings, covers, avg_party_size, demand_rank, pct_of_bookings
  FROM v_slot_demand
 WHERE (%(restaurant_id)s::int IS NULL OR restaurant_id = %(restaurant_id)s::int)
   AND demand_rank <= %(top_n)s::int
 ORDER BY restaurant_name, demand_rank
"""

# LAG reaches back to the previous row of the window, which is how a
# month-over-month delta is expressed without a self-join. The first month has no
# predecessor, so `delta` is NULL there rather than being silently treated as 0.
_RATING_TREND_SQL = """
WITH monthly AS (
    SELECT rt.restaurant_id,
           date_trunc('month', upper(res.during))::date AS month,
           count(*)                                     AS reviews,
           avg(rv.rating)                               AS avg_rating
      FROM reviews rv
      JOIN reservations      res USING (reservation_id)
      JOIN restaurant_tables rt  USING (table_id)
     WHERE (%(restaurant_id)s::int IS NULL
            OR rt.restaurant_id = %(restaurant_id)s::int)
     GROUP BY rt.restaurant_id, date_trunc('month', upper(res.during))
)
SELECT r.name AS restaurant_name,
       m.month,
       m.reviews,
       round(m.avg_rating::numeric, 2) AS avg_rating,
       round(LAG(m.avg_rating) OVER w :: numeric, 2) AS prev_avg_rating,
       round((m.avg_rating - LAG(m.avg_rating) OVER w)::numeric, 2) AS delta,
       round(AVG(m.avg_rating) OVER (
                 PARTITION BY m.restaurant_id ORDER BY m.month
                 ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
             )::numeric, 2) AS rolling_3mo
  FROM monthly m
  JOIN restaurants r USING (restaurant_id)
WINDOW w AS (PARTITION BY m.restaurant_id ORDER BY m.month)
 ORDER BY r.name, m.month
"""


def rank_restaurants_by_city(
    db: Database,
    *,
    city: str | None = None,
    max_rank: int | None = None,
    min_reviews: int = 0,
) -> list[RestaurantRanking]:
    """Restaurants ranked by average review score, within each city.

    The assignment's headline query. `max_rank=3` gives every city's top three —
    a per-group top-N that a plain `ORDER BY ... LIMIT` cannot express, because
    the limit would apply across all cities rather than within each.

    `min_reviews` guards against a restaurant with two glowing reviews
    outranking one with three hundred. On the seed dataset every restaurant has
    300+ reviews, so it changes nothing there; it matters the moment real data
    arrives with a long tail.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(
            _RANKING_SQL,
            {"city": city, "max_rank": max_rank, "min_reviews": min_reviews},
        )
        return [RestaurantRanking.from_row(row) for row in cur.fetchall()]


def nearby_restaurants(
    db: Database,
    *,
    longitude: float,
    latitude: float,
    radius_metres: float = 5000,
    min_rating: float | None = None,
    price_tier: int | None = None,
    limit: int = 20,
) -> list[NearbyRestaurant]:
    """Active restaurants within `radius_metres`, nearest first."""
    with db.transaction(readonly=True) as cur:
        cur.execute(
            _NEARBY_SQL,
            {
                "longitude": longitude,
                "latitude": latitude,
                "radius_metres": radius_metres,
                "min_rating": min_rating,
                "price_tier": price_tier,
                "limit": limit,
            },
        )
        return [NearbyRestaurant.from_row(row) for row in cur.fetchall()]


def nearest_restaurants(
    db: Database, *, longitude: float, latitude: float, limit: int = 5
) -> list[NearbyRestaurant]:
    """The `limit` closest restaurants, with no radius bound.

    Uses the `<->` KNN operator, so the GiST index is walked in distance order
    and stops early. Prefer this over `nearby_restaurants` with a huge radius:
    the radius form must find every match before sorting.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(
            _NEAREST_SQL,
            {"longitude": longitude, "latitude": latitude, "limit": limit},
        )
        return [NearbyRestaurant.from_row(row) for row in cur.fetchall()]


def find_nearby_available(
    db: Database,
    *,
    longitude: float,
    latitude: float,
    starts_at: Any,
    party_size: int,
    radius_metres: float = 5000,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Well-rated restaurants near a point that can seat a party at a time.

    Combines all three Day 4 threads — GiST radius search, the ratings rollup,
    and the Day 2 availability anti-join — in one statement. Returns plain dicts
    because the shape is a report, not a domain entity.

    The result is advisory in exactly the way `find_available_tables` is: a table
    counted free here may be taken before anyone books it. The exclusion
    constraint remains the only arbiter.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(
            _NEARBY_AVAILABLE_SQL,
            {
                "longitude": longitude,
                "latitude": latitude,
                "starts_at": starts_at,
                "party_size": party_size,
                "radius_metres": radius_metres,
                "limit": limit,
            },
        )
        return [dict(row) for row in cur.fetchall()]


def busiest_slots(
    db: Database, *, restaurant_id: int | None = None, top_n: int = 5
) -> list[dict[str, Any]]:
    """The `top_n` busiest (weekday, hour) slots per restaurant."""
    with db.transaction(readonly=True) as cur:
        cur.execute(
            _SLOT_DEMAND_SQL, {"restaurant_id": restaurant_id, "top_n": top_n}
        )
        return [dict(row) for row in cur.fetchall()]


def rating_trend(
    db: Database, *, restaurant_id: int | None = None
) -> list[dict[str, Any]]:
    """Month-over-month average rating, with LAG delta and a 3-month average."""
    with db.transaction(readonly=True) as cur:
        cur.execute(_RATING_TREND_SQL, {"restaurant_id": restaurant_id})
        return [dict(row) for row in cur.fetchall()]


__all__ = [
    "busiest_slots",
    "find_nearby_available",
    "nearby_restaurants",
    "nearest_restaurants",
    "rank_restaurants_by_city",
    "rating_trend",
]