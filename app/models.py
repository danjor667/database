"""Row-to-object mapping.

Rows arrive as dicts (RealDictCursor); these dataclasses give callers a stable
shape that does not change when a query's column list does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class AvailableTable:
    table_id: int
    table_code: str
    seats: int
    min_party_size: int
    zone: str
    starts_at: datetime
    ends_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AvailableTable":
        return cls(
            table_id=row["table_id"],
            table_code=row["table_code"],
            seats=row["seats"],
            min_party_size=row["min_party_size"],
            zone=row["zone"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
        )


@dataclass(frozen=True)
class RestaurantRanking:
    """One row of the per-city leaderboard (`v_restaurant_rankings`)."""

    restaurant_id: int
    restaurant_name: str
    city: str
    region: str
    price_tier: int
    review_count: int
    avg_rating: float | None
    city_rank: int
    city_dense_rank: int
    city_restaurant_count: int
    city_avg_rating: float | None
    vs_city_avg: float | None
    pct_of_city_reviews: float | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RestaurantRanking":
        return cls(
            restaurant_id=row["restaurant_id"],
            restaurant_name=row["restaurant_name"],
            city=row["city"],
            region=row["region"],
            price_tier=row["price_tier"],
            review_count=row["review_count"],
            avg_rating=_as_float(row["avg_rating"]),
            city_rank=row["city_rank"],
            city_dense_rank=row["city_dense_rank"],
            city_restaurant_count=row["city_restaurant_count"],
            city_avg_rating=_as_float(row["city_avg_rating"]),
            vs_city_avg=_as_float(row["vs_city_avg"]),
            pct_of_city_reviews=_as_float(row["pct_of_city_reviews"]),
        )


@dataclass(frozen=True)
class NearbyRestaurant:
    """A restaurant with its distance from a search point, in metres."""

    restaurant_id: int
    name: str
    city: str
    metres: float
    price_tier: int
    avg_rating: float | None
    review_count: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "NearbyRestaurant":
        return cls(
            restaurant_id=row["restaurant_id"],
            name=row["name"],
            city=row["city"],
            metres=_as_float(row["metres"]),
            price_tier=row["price_tier"],
            avg_rating=_as_float(row.get("avg_rating")),
            review_count=row.get("review_count") or 0,
        )


def _as_float(value: Any) -> float | None:
    """numeric columns arrive as Decimal; callers want plain floats."""
    return None if value is None else float(value)


@dataclass(frozen=True)
class Feedback:
    """A long-form feedback document from MongoDB.

    `feedback_id` is the stringified ObjectId — the same value Postgres stores in
    `reviews.body_ref`. It is kept as `str` rather than `bson.ObjectId` so that
    callers of this package never need bson imported.
    """

    feedback_id: str
    reservation_id: int
    restaurant_id: int
    customer_id: int
    rating: int
    body: str
    title: str | None = None
    tags: tuple[str, ...] = ()
    language: str = "en"
    visited_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> "Feedback":
        return cls(
            feedback_id=str(doc["_id"]),
            reservation_id=doc["reservation_id"],
            restaurant_id=doc["restaurant_id"],
            customer_id=doc["customer_id"],
            rating=doc["rating"],
            body=doc["body"],
            title=doc.get("title"),
            tags=tuple(doc.get("tags") or ()),
            language=doc.get("language", "en"),
            visited_at=doc.get("visited_at"),
            created_at=doc.get("created_at"),
        )


@dataclass(frozen=True)
class Reservation:
    reservation_id: int
    table_id: int
    customer_id: int
    starts_at: datetime
    ends_at: datetime
    party_size: int
    status: str
    special_requests: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Reservation":
        # `during` comes back as psycopg2.extras.DateTimeTZRange; unpack it so
        # callers deal in plain datetimes rather than a driver-specific type.
        during = row["during"]
        return cls(
            reservation_id=row["reservation_id"],
            table_id=row["table_id"],
            customer_id=row["customer_id"],
            starts_at=during.lower,
            ends_at=during.upper,
            party_size=row["party_size"],
            status=row["status"],
            special_requests=row.get("special_requests"),
        )