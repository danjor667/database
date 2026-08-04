"""Restaurant reservation backend — data access layer."""

from .availability import find_available_tables
from .config import Settings, load_settings
from .db import Database
from .errors import (
    BookingConflict,
    BookingError,
    CustomerNotFound,
    InvalidBookingRequest,
    NoTableAvailable,
    PoolExhausted,
    ReservationNotFound,
    RestaurantNotFound,
    TableNotBookable,
)
from .models import AvailableTable, Reservation
from .reservations import (
    book_reservation,
    cancel_reservation,
    get_reservation,
    list_customer_reservations,
)

__all__ = [
    "AvailableTable",
    "BookingConflict",
    "BookingError",
    "CustomerNotFound",
    "Database",
    "InvalidBookingRequest",
    "NoTableAvailable",
    "PoolExhausted",
    "Reservation",
    "ReservationNotFound",
    "RestaurantNotFound",
    "Settings",
    "TableNotBookable",
    "book_reservation",
    "cancel_reservation",
    "find_available_tables",
    "get_reservation",
    "list_customer_reservations",
    "load_settings",
]