"""Single-threaded behaviour of book_reservation and friends."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import (
    CustomerNotFound,
    Database,
    InvalidBookingRequest,
    NoTableAvailable,
    Reservation,
    ReservationNotFound,
    RestaurantNotFound,
    TableNotBookable,
    book_reservation,
    cancel_reservation,
    find_available_tables,
    get_reservation,
    list_customer_reservations,
)
from app.errors import BookingConflict


def test_books_a_table(db: Database, restaurant_id: int, customer_id: int, slot):
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
    )

    assert isinstance(res, Reservation)
    assert res.reservation_id > 0
    assert res.status == "confirmed"
    assert res.party_size == 2
    assert res.starts_at == slot
    # Vero Nord seats for 105 minutes; the duration comes from the restaurant,
    # not from the caller.
    assert res.ends_at - res.starts_at == timedelta(minutes=105)


def test_duration_override_is_honoured(
    db: Database, restaurant_id: int, customer_id: int, slot
):
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
        duration_minutes=60,
    )
    assert res.ends_at - res.starts_at == timedelta(minutes=60)


def test_prefers_the_smallest_fitting_table(
    db: Database, restaurant_id: int, customer_id: int, slot
):
    """A party of two should not consume a six-top while two-tops are free."""
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
    )
    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT seats FROM restaurant_tables WHERE table_id = %s", (res.table_id,)
        )
        seats = cur.fetchone()["seats"]
    assert seats == 2


def test_second_booking_takes_a_different_table(
    db: Database, restaurant_id: int, customer_ids: list[int], slot
):
    first = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[0],
        starts_at=slot,
        party_size=2,
    )
    second = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[1],
        starts_at=slot,
        party_size=2,
    )
    assert first.table_id != second.table_id


def test_pinned_table_is_used(
    db: Database, restaurant_id: int, customer_id: int, slot, two_seat_table: int
):
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
        table_id=two_seat_table,
    )
    assert res.table_id == two_seat_table


def test_pinned_table_twice_is_rejected(
    db: Database, restaurant_id: int, customer_ids: list[int], slot, two_seat_table: int
):
    book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[0],
        starts_at=slot,
        party_size=2,
        table_id=two_seat_table,
    )
    with pytest.raises(NoTableAvailable, match="already booked"):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_ids[1],
            starts_at=slot,
            party_size=2,
            table_id=two_seat_table,
        )


def test_abutting_slot_is_bookable(
    db: Database, restaurant_id: int, customer_ids: list[int], slot, two_seat_table: int
):
    """The slot immediately after must be free — half-open range bounds."""
    first = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[0],
        starts_at=slot,
        party_size=2,
        table_id=two_seat_table,
    )
    second = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[1],
        starts_at=first.ends_at,
        party_size=2,
        table_id=two_seat_table,
    )
    assert second.starts_at == first.ends_at
    assert second.table_id == two_seat_table


def test_overlapping_slot_on_pinned_table_is_rejected(
    db: Database, restaurant_id: int, customer_ids: list[int], slot, two_seat_table: int
):
    book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[0],
        starts_at=slot,
        party_size=2,
        table_id=two_seat_table,
    )
    with pytest.raises(NoTableAvailable):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_ids[1],
            starts_at=slot + timedelta(minutes=30),
            party_size=2,
            table_id=two_seat_table,
        )


def test_fully_booked_restaurant_reports_capacity(
    db: Database, restaurant_id: int, customer_ids: list[int], slot
):
    """Book out every two-seatable table, then expect a precise error."""
    free = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    assert len(free) > 0
    for i, table in enumerate(free):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_ids[i % len(customer_ids)],
            starts_at=slot,
            party_size=2,
            table_id=table.table_id,
        )

    with pytest.raises(NoTableAvailable, match="are booked for that slot"):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_ids[0],
            starts_at=slot,
            party_size=2,
        )


def test_party_too_large_for_any_table(
    db: Database, restaurant_id: int, customer_id: int, slot
):
    with pytest.raises(NoTableAvailable, match="no table that seats"):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            starts_at=slot,
            party_size=25,
        )


def test_pinned_table_too_small(
    db: Database, restaurant_id: int, customer_id: int, slot, two_seat_table: int
):
    with pytest.raises(TableNotBookable, match="seats 1-2"):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            starts_at=slot,
            party_size=6,
            table_id=two_seat_table,
        )


def test_pinned_table_from_another_restaurant(
    db: Database, restaurant_id: int, customer_id: int, slot
):
    with db.transaction(readonly=True) as cur:
        cur.execute(
            """
            SELECT table_id FROM restaurant_tables
             WHERE restaurant_id <> %s AND seats = 4
             ORDER BY table_id LIMIT 1
            """,
            (restaurant_id,),
        )
        foreign_table = cur.fetchone()["table_id"]

    with pytest.raises(TableNotBookable, match="belongs to restaurant"):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            starts_at=slot,
            party_size=2,
            table_id=foreign_table,
        )


def test_unknown_restaurant(db: Database, customer_id: int, slot):
    with pytest.raises(RestaurantNotFound):
        book_reservation(
            db,
            restaurant_id=10_000_000,
            customer_id=customer_id,
            starts_at=slot,
            party_size=2,
        )


def test_unknown_customer(db: Database, restaurant_id: int, slot):
    with pytest.raises(CustomerNotFound):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=10_000_000,
            starts_at=slot,
            party_size=2,
        )


def test_naive_datetime_is_rejected(db: Database, restaurant_id: int, customer_id: int):
    with pytest.raises(InvalidBookingRequest, match="timezone-aware"):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            starts_at=datetime(2027, 6, 1, 19, 0),  # no tzinfo
            party_size=2,
        )


def test_zero_party_size_is_rejected(
    db: Database, restaurant_id: int, customer_id: int, slot
):
    with pytest.raises(InvalidBookingRequest, match="party_size"):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            starts_at=slot,
            party_size=0,
        )


def test_zone_preference_is_respected(
    db: Database, restaurant_id: int, customer_id: int, slot
):
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=4,
        zone="patio",
    )
    with db.transaction(readonly=True) as cur:
        cur.execute(
            "SELECT zone FROM restaurant_tables WHERE table_id = %s", (res.table_id,)
        )
        assert cur.fetchone()["zone"] == "patio"


def test_cancel_releases_the_slot(
    db: Database, restaurant_id: int, customer_ids: list[int], slot, two_seat_table: int
):
    first = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[0],
        starts_at=slot,
        party_size=2,
        table_id=two_seat_table,
    )
    cancelled = cancel_reservation(db, first.reservation_id)
    assert cancelled.status == "cancelled"

    # The same table is now bookable again for the same slot.
    rebooked = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[1],
        starts_at=slot,
        party_size=2,
        table_id=two_seat_table,
    )
    assert rebooked.table_id == two_seat_table
    assert rebooked.reservation_id != first.reservation_id


def test_cancel_twice_is_rejected(
    db: Database, restaurant_id: int, customer_id: int, slot
):
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
    )
    cancel_reservation(db, res.reservation_id)
    with pytest.raises(BookingConflict, match="cancelled"):
        cancel_reservation(db, res.reservation_id)


def test_cancel_unknown_reservation(db: Database):
    with pytest.raises(ReservationNotFound):
        cancel_reservation(db, 10_000_000)


def test_get_and_list(db: Database, restaurant_id: int, customer_id: int, slot):
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_id,
        starts_at=slot,
        party_size=2,
        special_requests="window seat",
    )
    fetched = get_reservation(db, res.reservation_id)
    assert fetched == res
    assert fetched.special_requests == "window seat"

    listed = list_customer_reservations(db, customer_id, limit=5)
    # Ordered by start time descending, and our slot is 300 days out.
    assert listed[0].reservation_id == res.reservation_id


def test_get_unknown_reservation(db: Database):
    with pytest.raises(ReservationNotFound):
        get_reservation(db, 10_000_000)


def test_sql_injection_attempt_is_inert(db: Database, restaurant_id: int, slot):
    """Parameterized queries mean hostile input is data, never syntax."""
    hostile = "'; DROP TABLE reservations; --"
    with pytest.raises(InvalidBookingRequest):
        book_reservation(
            db,
            restaurant_id=restaurant_id,
            customer_id=1,
            starts_at=slot,
            party_size=0,
            special_requests=hostile,
        )

    # Prove the table is still there, and that the payload round-trips as text.
    res = book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=1,
        starts_at=slot,
        party_size=2,
        special_requests=hostile,
    )
    assert get_reservation(db, res.reservation_id).special_requests == hostile


def test_availability_shrinks_as_tables_are_booked(
    db: Database, restaurant_id: int, customer_ids: list[int], slot
):
    before = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    book_reservation(
        db,
        restaurant_id=restaurant_id,
        customer_id=customer_ids[0],
        starts_at=slot,
        party_size=2,
    )
    after = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    assert len(after) == len(before) - 1


def test_availability_is_ordered_smallest_first(
    db: Database, restaurant_id: int, slot
):
    tables = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    assert [t.seats for t in tables] == sorted(t.seats for t in tables)


def test_availability_window_matches_restaurant_slot(
    db: Database, restaurant_id: int, slot
):
    tables = find_available_tables(
        db, restaurant_id=restaurant_id, starts_at=slot, party_size=2
    )
    assert tables
    for table in tables:
        assert table.starts_at == slot
        assert table.ends_at - table.starts_at == timedelta(minutes=105)


def test_availability_for_unknown_restaurant_is_empty(db: Database, slot):
    assert (
        find_available_tables(
            db, restaurant_id=10_000_000, starts_at=slot, party_size=2
        )
        == []
    )