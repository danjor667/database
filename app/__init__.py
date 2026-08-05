"""Restaurant reservation backend — data access layer."""

from .availability import find_available_tables
from .config import Settings, load_settings
from .db import Database
from .errors import (
    BookingConflict,
    BookingError,
    CustomerNotFound,
    FeedbackNotFound,
    InvalidBookingRequest,
    NoTableAvailable,
    PoolExhausted,
    ReservationNotFound,
    RestaurantNotFound,
    ReviewNotFound,
    TableNotBookable,
)
from .models import AvailableTable, Feedback, Reservation
from .reservations import (
    book_reservation,
    cancel_reservation,
    get_reservation,
    list_customer_reservations,
)

# `cache` and `feedback` are deliberately NOT re-exported here. Importing them
# pulls in redis / pymongo, and the Day 1-2 core must keep working with only
# psycopg2 installed. Import them directly:
#
#     from app.cache import AvailabilityCache
#     from app.feedback import FeedbackStore, add_review

__all__ = [
    "AvailableTable",
    "BookingConflict",
    "BookingError",
    "CustomerNotFound",
    "Database",
    "Feedback",
    "FeedbackNotFound",
    "InvalidBookingRequest",
    "NoTableAvailable",
    "PoolExhausted",
    "Reservation",
    "ReservationNotFound",
    "RestaurantNotFound",
    "ReviewNotFound",
    "Settings",
    "TableNotBookable",
    "book_reservation",
    "cancel_reservation",
    "find_available_tables",
    "get_reservation",
    "list_customer_reservations",
    "load_settings",
]