"""Domain exceptions.

Callers should never have to catch a psycopg2 error or inspect a SQLSTATE: the
data layer translates them here. In particular a lost booking race
(SQLSTATE 23P01) is a normal, expected outcome, not a database malfunction.
"""

from __future__ import annotations


class BookingError(Exception):
    """Base class for every failure this package raises."""


class InvalidBookingRequest(BookingError):
    """Arguments were malformed — e.g. a naive datetime or a party size of 0."""


class PoolExhausted(BookingError):
    """No pooled connection became available within the acquire timeout.

    Backpressure, not corruption: the request never reached the database, so it
    is safe to retry.
    """


class RestaurantNotFound(BookingError):
    """No such restaurant, or it is deactivated."""


class CustomerNotFound(BookingError):
    """No such customer."""


class ReservationNotFound(BookingError):
    """No such reservation."""


class NoTableAvailable(BookingError):
    """The request is valid but nothing suitable is free in that slot."""


class TableNotBookable(BookingError):
    """A specific table was requested that cannot take this booking.

    Raised when the table is inactive, belongs to another restaurant, or cannot
    seat the party — as distinct from being merely occupied.
    """


class BookingConflict(BookingError):
    """Lost the race to concurrent bookings repeatedly and gave up retrying.

    Signals contention, not invalid input: the same request may well succeed if
    retried later.
    """