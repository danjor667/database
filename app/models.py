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