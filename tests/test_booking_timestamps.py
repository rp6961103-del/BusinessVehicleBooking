from datetime import datetime, timezone

from app import format_booking_created_at


def test_booking_timestamp_is_converted_from_utc_to_ist():
    utc_timestamp = datetime(2026, 10, 31, 7, 24, tzinfo=timezone.utc)

    assert format_booking_created_at(utc_timestamp) == "31 Oct 2026, 12:54 PM IST"


def test_naive_database_datetime_is_treated_as_utc():
    database_timestamp = datetime(2026, 10, 31, 7, 24)

    assert format_booking_created_at(database_timestamp) == "31 Oct 2026, 12:54 PM IST"


def test_missing_or_invalid_booking_timestamp_is_not_invented():
    assert format_booking_created_at(None) == "Booking time unavailable"
    assert format_booking_created_at("not-a-timestamp") == "Booking time unavailable"
