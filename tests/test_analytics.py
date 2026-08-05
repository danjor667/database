"""Day 4 analytics: window functions and location search.

These assert on *semantics* the SQL is easy to get subtly wrong — per-group
top-N, tie handling, NULL placement, distance units — rather than on the seed's
particular numbers, which would break the moment the dataset changed.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import Database
from app.analytics import (
    busiest_slots,
    find_nearby_available,
    nearby_restaurants,
    nearest_restaurants,
    rank_restaurants_by_city,
    rating_trend,
)

# Times Square. Longitude first — the order ST_MakePoint takes.
TIMES_SQUARE = {"longitude": -73.9855, "latitude": 40.7580}


@pytest.fixture
def unrated_restaurant(db: Database):
    """A restaurant with no tables and no reviews, removed afterwards."""
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO restaurants (name, city_id, address_line, location)
            VALUES ('Zzz Test Diner',
                    (SELECT city_id FROM cities WHERE name = 'New York'),
                    '1 Nowhere St',
                    ST_MakePoint(-73.9700, 40.7550)::geography)
            RETURNING restaurant_id
            """
        )
        restaurant_id = cur.fetchone()["restaurant_id"]
    try:
        yield restaurant_id
    finally:
        with db.transaction() as cur:
            cur.execute(
                "DELETE FROM restaurants WHERE restaurant_id = %s", (restaurant_id,)
            )


# -- ranking ----------------------------------------------------------------


def test_every_city_ranking_starts_at_one(db: Database):
    rows = rank_restaurants_by_city(db)
    assert rows

    by_city: dict[str, list[int]] = {}
    for row in rows:
        by_city.setdefault(row.city, []).append(row.city_rank)

    for city, ranks in by_city.items():
        assert min(ranks) == 1, f"{city} has no rank 1"
        assert ranks == sorted(ranks)


def test_max_rank_is_per_city_not_a_global_limit(db: Database):
    """The whole point of PARTITION BY: top-N *within each group*."""
    top1 = rank_restaurants_by_city(db, max_rank=1)
    cities = {row.city for row in rank_restaurants_by_city(db)}

    assert len(top1) == len(cities), "expected exactly one winner per city"
    assert {row.city for row in top1} == cities
    assert all(row.city_rank == 1 for row in top1)


def test_rank_orders_by_rating_descending(db: Database):
    for city in {r.city for r in rank_restaurants_by_city(db)}:
        rows = rank_restaurants_by_city(db, city=city)
        rated = [r.avg_rating for r in rows if r.avg_rating is not None]
        assert rated == sorted(rated, reverse=True), f"{city} is out of order"


def test_city_aggregate_columns_are_consistent(db: Database):
    """The window aggregates must agree with the rows they are computed over."""
    for city in {r.city for r in rank_restaurants_by_city(db)}:
        rows = rank_restaurants_by_city(db, city=city)
        assert all(r.city_restaurant_count == len(rows) for r in rows)

        rated = [r.avg_rating for r in rows if r.avg_rating is not None]
        if rated:
            expected = round(sum(rated) / len(rated), 2)
            assert all(abs(r.city_avg_rating - expected) <= 0.01 for r in rows)

        for row in rows:
            if row.avg_rating is not None:
                assert abs(row.vs_city_avg - (row.avg_rating - row.city_avg_rating)) <= 0.01

        shares = [r.pct_of_city_reviews for r in rows if r.pct_of_city_reviews]
        if shares:
            assert abs(sum(shares) - 100.0) < 0.5


def test_unrated_restaurant_survives_and_sorts_last(
    db: Database, unrated_restaurant: int
):
    """LEFT JOIN + NULLS LAST: no reviews must not mean invisible, or first."""
    rows = rank_restaurants_by_city(db, city="New York")
    mine = [r for r in rows if r.restaurant_id == unrated_restaurant]

    assert mine, "a restaurant with no reviews vanished from the ranking"
    assert mine[0].review_count == 0
    assert mine[0].avg_rating is None
    assert mine[0].city_rank == max(r.city_rank for r in rows), "unrated sorted first"


def test_min_reviews_filters_thin_samples(db: Database, unrated_restaurant: int):
    rows = rank_restaurants_by_city(db, city="New York", min_reviews=1)
    assert unrated_restaurant not in {r.restaurant_id for r in rows}


def test_rank_dense_rank_and_row_number_differ_only_on_ties(db: Database):
    """Pins the semantics documented in sql/07_analytics.sql.

    Asserted against a literal VALUES list rather than the seed, because the
    seed contains no tied ratings — which is precisely why the difference is
    invisible there and worth proving separately.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT v,
                   RANK()       OVER w AS rnk,
                   DENSE_RANK() OVER w AS dense,
                   ROW_NUMBER() OVER w AS rownum
              FROM (VALUES (4.4), (4.2), (4.2), (4.1)) AS t(v)
            WINDOW w AS (ORDER BY v DESC)
             ORDER BY v DESC, rownum
            """
        )
        rows = cur.fetchall()

    assert [r["rnk"] for r in rows] == [1, 2, 2, 4], "RANK must skip after a tie"
    assert [r["dense"] for r in rows] == [1, 2, 2, 3], "DENSE_RANK must not skip"
    assert [r["rownum"] for r in rows] == [1, 2, 3, 4], "ROW_NUMBER must be distinct"


def test_seed_has_no_ties_so_all_three_agree(db: Database):
    for row in rank_restaurants_by_city(db, min_reviews=1):
        assert row.city_rank == row.city_dense_rank


# -- the rollup the ranking sits on -----------------------------------------


def test_last_reviewed_visit_counts_only_reviewed_visits(db: Database):
    """Regression: the view once reported the last *reservation* instead.

    With `reviews` LEFT JOINed last, un-reviewed reservations still fed max(),
    so a future booking could be reported as the last reviewed visit.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT v.restaurant_id, v.last_reviewed_visit, t.truth
              FROM v_restaurant_ratings v
              JOIN LATERAL (
                    SELECT max(upper(res.during)) AS truth
                      FROM reviews rv
                      JOIN reservations      res USING (reservation_id)
                      JOIN restaurant_tables rt  USING (table_id)
                     WHERE rt.restaurant_id = v.restaurant_id
                   ) t ON true
            """
        )
        rows = cur.fetchall()

    assert rows
    for row in rows:
        assert row["last_reviewed_visit"] == row["truth"], (
            f"restaurant {row['restaurant_id']}: view says "
            f"{row['last_reviewed_visit']}, actual {row['truth']}"
        )


def test_rollup_review_counts_match_the_base_table(db: Database):
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT v.restaurant_id, v.review_count, t.actual
              FROM v_restaurant_ratings v
              JOIN LATERAL (
                    SELECT count(*) AS actual
                      FROM reviews rv
                      JOIN reservations      res USING (reservation_id)
                      JOIN restaurant_tables rt  USING (table_id)
                     WHERE rt.restaurant_id = v.restaurant_id
                   ) t ON true
            """
        )
        for row in cur.fetchall():
            assert row["review_count"] == row["actual"]


# -- location ---------------------------------------------------------------


def test_radius_search_respects_its_bound(db: Database):
    radius = 5000
    rows = nearby_restaurants(db, **TIMES_SQUARE, radius_metres=radius)
    assert rows, "expected at least one restaurant near Times Square"
    assert all(r.metres <= radius for r in rows)
    assert [r.metres for r in rows] == sorted(r.metres for r in rows)


def test_a_wider_radius_can_only_add(db: Database):
    near = {r.restaurant_id for r in nearby_restaurants(db, **TIMES_SQUARE, radius_metres=3000)}
    far = {r.restaurant_id for r in nearby_restaurants(db, **TIMES_SQUARE, radius_metres=20000)}
    assert near <= far


def test_distances_are_metres_not_degrees(db: Database):
    """`geography` means the number is metres on a spheroid.

    Vero Nord is at 18 W 21st St, a bit under 2 km from Times Square. A planar
    `geometry` calculation would return ~0.02 (degrees) instead.
    """
    rows = nearby_restaurants(db, **TIMES_SQUARE, radius_metres=20000)
    by_name = {r.name: r.metres for r in rows}
    assert 1000 < by_name["Vero Nord"] < 3000, by_name


def test_nearest_returns_closest_first_without_a_radius(db: Database):
    rows = nearest_restaurants(db, **TIMES_SQUARE, limit=3)
    assert len(rows) == 3
    assert [r.metres for r in rows] == sorted(r.metres for r in rows)

    # KNN and radius search must agree about what is closest.
    widest = nearby_restaurants(db, **TIMES_SQUARE, radius_metres=1_000_000, limit=3)
    assert [r.restaurant_id for r in rows] == [r.restaurant_id for r in widest]


def test_rating_and_price_filters_apply(db: Database):
    rated = nearby_restaurants(
        db, **TIMES_SQUARE, radius_metres=1_000_000, min_rating=4.0, limit=50
    )
    assert rated
    assert all(r.avg_rating >= 4.0 for r in rated)

    cheap = nearby_restaurants(
        db, **TIMES_SQUARE, radius_metres=1_000_000, price_tier=2, limit=50
    )
    assert all(r.price_tier <= 2 for r in cheap)


# -- the combined query -----------------------------------------------------


def test_nearby_available_combines_all_three(db: Database):
    slot = datetime.combine(
        date.today() + timedelta(days=305), time(19, 0), tzinfo=timezone.utc
    )
    rows = find_nearby_available(
        db, **TIMES_SQUARE, starts_at=slot, party_size=4,
        radius_metres=1_000_000, limit=20,
    )
    assert rows

    assert all(r["free_tables"] > 0 for r in rows)
    assert all(r["smallest_fit"] >= 4 for r in rows), "a table too small was counted"

    ratings = [r["avg_rating"] for r in rows if r["avg_rating"] is not None]
    assert ratings == sorted(ratings, reverse=True)
    assert rows[0]["rating_rank"] == 1


def test_nearby_available_drops_a_fully_booked_restaurant(
    db: Database, restaurant_id: int, customer_ids: list[int], slot: datetime
):
    """Book out every table that fits, and the restaurant must disappear."""
    from app import book_reservation, find_available_tables

    free = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=4
    )
    assert free
    for i, table in enumerate(free):
        book_reservation(
            db, restaurant_id=restaurant_id, customer_id=customer_ids[i % len(customer_ids)],
            starts_at=slot, party_size=4, table_id=table.table_id,
        )

    rows = find_nearby_available(
        db, **TIMES_SQUARE, starts_at=slot, party_size=4,
        radius_metres=1_000_000, limit=50,
    )
    assert restaurant_id not in {r["restaurant_id"] for r in rows}


# -- demand and trend -------------------------------------------------------


def test_busiest_slots_are_ranked_and_capped(db: Database, restaurant_id: int):
    rows = busiest_slots(db, restaurant_id=restaurant_id, top_n=3)
    assert rows
    assert all(r["demand_rank"] <= 3 for r in rows)
    assert [r["bookings"] for r in rows] == sorted(
        (r["bookings"] for r in rows), reverse=True
    )
    assert all(0 <= r["weekday"] <= 6 for r in rows)


def test_tied_slots_share_a_rank(db: Database):
    """RANK's tie behaviour, on real data rather than a literal.

    Unlike the ratings, slot demand does produce ties in the seed — several
    restaurants have two equally busy slots. Where a tie exists, RANK must give
    both the same number and then skip.
    """
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT restaurant_id, bookings, demand_rank
              FROM v_slot_demand
             WHERE restaurant_id IN (
                       SELECT restaurant_id FROM v_slot_demand
                        GROUP BY restaurant_id, demand_rank
                       HAVING count(*) > 1 LIMIT 1)
             ORDER BY demand_rank, bookings DESC
            """
        )
        rows = cur.fetchall()

    if not rows:
        pytest.skip("no tied slots in this dataset")

    by_rank: dict[int, set[int]] = {}
    for row in rows:
        by_rank.setdefault(row["demand_rank"], set()).add(row["bookings"])

    # Equal rank must mean equal value...
    for rank, counts in by_rank.items():
        assert len(counts) == 1, f"rank {rank} covers differing counts {counts}"

    # ...and a tie of N must consume N rank positions.
    ranks = sorted(by_rank)
    tied = [r for r in ranks if sum(1 for x in rows if x["demand_rank"] == r) > 1]
    assert tied, "expected at least one tied rank"
    for rank in tied:
        width = sum(1 for x in rows if x["demand_rank"] == rank)
        following = [r for r in ranks if r > rank]
        if following:
            assert min(following) == rank + width, (
                f"RANK must skip {width} positions after a {width}-way tie"
            )


def test_rating_trend_lag_is_the_previous_month(db: Database, restaurant_id: int):
    rows = rating_trend(db, restaurant_id=restaurant_id)
    assert len(rows) >= 2

    assert rows[0]["prev_avg_rating"] is None, "the first month has no predecessor"
    assert rows[0]["delta"] is None

    for previous, current in zip(rows, rows[1:]):
        assert current["prev_avg_rating"] == previous["avg_rating"]
        assert abs(
            float(current["delta"])
            - (float(current["avg_rating"]) - float(previous["avg_rating"]))
        ) <= 0.01