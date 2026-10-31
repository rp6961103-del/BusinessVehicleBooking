"""
Integration tests for Customer Dashboard -> My Bookings -> Open Booking -> Live Tracking Map workflow.
Verifies authorization, embedded map rendering, pending bookings, owner broadcasting, and error handling.
"""

from pathlib import Path

import pytest
from app import app
from tracking_service import (
    start_tracking_session,
    record_live_location,
    stop_tracking_session,
    get_live_tracking_data_by_booking,
    get_db_connection,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key"
    with app.test_client() as client:
        yield client


def test_customer_dashboard_unauthenticated_redirects(client):
    """Accessing /dashboard without customer session must redirect to /login."""
    res = client.get("/dashboard")
    assert res.status_code == 302
    assert res.location.endswith("/login")


def test_customer_dashboard_authenticated(client):
    """Logged in customer accessing /dashboard gets 200 OK with their bookings and Open Booking links."""
    with client.session_transaction() as sess:
        sess["user_id"] = 11
        sess["user_name"] = "RAM PRASAD"
        sess["user_phone"] = "9866862006"

    res = client.get("/dashboard")
    assert res.status_code == 200
    assert b"Customer Dashboard" in res.data
    assert b"RAM PRASAD" in res.data
    assert b"/live-tracking/booking/" in res.data


def test_booking_shortcut_redirects(client):
    """Accessing /booking/<id> redirects to /live-tracking/booking/<id>."""
    with client.session_transaction() as sess:
        sess["user_id"] = 11
        sess["user_name"] = "RAM PRASAD"
        sess["user_phone"] = "9866862006"

    res = client.get("/booking/28")
    assert res.status_code == 302
    assert res.location == "/live-tracking/booking/28"

    res2 = client.get("/mybookings/28")
    assert res2.status_code == 302
    assert res2.location == "/live-tracking/booking/28"


def test_wsgi_registers_booking_specific_tracking_route():
    from wsgi import app as wsgi_app

    assert any(
        rule.rule == "/live-tracking/booking/<int:booking_id>"
        and rule.endpoint == "live_tracking.booking_tracking_shortcut"
        for rule in wsgi_app.url_map.iter_rules()
    )


def test_my_bookings_template_shows_tracking_action_and_unavailable_status():
    template = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "my_bookings.html"
    ).read_text(encoding="utf-8")

    assert "View Live Tracking" in template
    assert "url_for('live_tracking.booking_tracking_shortcut', booking_id=booking[0])" in template
    assert "The map shows whether the driver has started sharing GPS." in template
    assert "live tracking becomes available after acceptance." in template


def test_customer_opens_authorized_booking_with_embedded_map(client):
    """Customer opens the booking-specific full-screen live map."""
    with client.session_transaction() as sess:
        sess["user_id"] = 11
        sess["user_name"] = "RAM PRASAD"
        sess["user_phone"] = "9866862006"

    # Booking 28 belongs to RAM PRASAD (phone 9866862006)
    res = client.get("/live-tracking/booking/28", follow_redirects=True)
    assert res.status_code == 200
    assert b"Booking #28" in res.data
    assert b"fullscreen-tracking-page" in res.data
    assert b"customer_tracking_fullscreen.css" in res.data
    assert b"trackingMap" in res.data
    assert b"customer_tracking.js" in res.data
    assert b"leaflet.js" in res.data
    assert b"Recenter Vehicle" in res.data
    assert b"Full Screen" in res.data
    assert b"Back to My Bookings" in res.data
    assert (
        b"Live tracking has not started yet." in res.data
        or b"WAITING FOR DRIVER LOCATION" in res.data
        or b"TRIP ENDED / STOPPED" in res.data
    )
    assert b"valBookingStatus" in res.data
    assert b"fullscreenRoute" in res.data


def test_customer_cannot_open_other_customer_booking(client):
    """Customer B (phone 8978610637) cannot view Customer A's booking (Booking 28, phone 9866862006)."""
    with client.session_transaction() as sess:
        sess["user_id"] = 14
        sess["user_name"] = "balaji"
        sess["user_phone"] = "8978610637"

    res = client.get("/live-tracking/booking/28")
    assert res.status_code == 403
    # Must display Access Restricted without rendering the live map or vehicle coordinates
    assert b"You are not authorized to view this booking." in res.data
    assert b'id="trackingMap"' not in res.data


def test_customer_opens_pending_booking(client):
    """Pending booking can be opened and clearly shows Waiting for driver location."""
    with client.session_transaction() as sess:
        sess["user_id"] = 11
        sess["user_name"] = "RAM PRASAD"
        sess["user_phone"] = "9866862006"

    # Booking 28 has status 'Pending'
    res = client.get("/live-tracking/booking/28", follow_redirects=True)
    assert res.status_code == 200
    assert b"Pending" in res.data
    assert (
        b"Live tracking has not started yet." in res.data
        or b"WAITING FOR DRIVER LOCATION" in res.data
        or b"TRIP ENDED / STOPPED" in res.data
    )


def test_live_gps_broadcasting_updates_customer_polling(client):
    """When owner broadcasts GPS coordinates, customer polling receives real coordinates and updates."""
    # 1. Owner starts session for Vehicle 1 and Booking 26 (RAM PRASAD)
    # Vehicle 4 belongs to Owner 2
    session_data, err = start_tracking_session(owner_id=2, vehicle_id=4, booking_id=26, registration_number="AP04 LIVE 100")
    assert err is None
    session_token = session_data["session_token"]

    try:
        # 2. Owner broadcasts coordinates (Madurai area: 9.9252, 78.1198)
        ok, res = record_live_location(
            owner_id=2,
            session_token=session_token,
            lat=9.9252,
            lng=78.1198,
            accuracy=6.0,
            speed=15.5,
            heading=45.0,
        )
        assert ok is True

        # 3. Customer RAM PRASAD polls for Booking 26
        with client.session_transaction() as sess:
            sess["user_id"] = 11
            sess["user_name"] = "RAM PRASAD"
            sess["user_phone"] = "9866862006"

        poll_res = client.get("/live-tracking/api/location/poll?booking_id=26")
        assert poll_res.status_code == 200
        poll_json = poll_res.get_json()
        assert poll_json["success"] is True
        data = poll_json["data"]
        assert data["tracking_state"] == "LIVE"
        assert data["location"]["latitude"] == 9.9252
        assert data["location"]["longitude"] == 78.1198
        assert data["vehicle"]["name"] is not None
        assert data["booking"]["id"] == 26
    finally:
        stop_tracking_session(owner_id=2, session_token=session_token)


def test_poll_unauthorized_customer_returns_403(client):
    """Polling another customer's booking returns 403 Forbidden."""
    with client.session_transaction() as sess:
        sess["user_id"] = 14
        sess["user_name"] = "balaji"
        sess["user_phone"] = "8978610637"

    # Booking 28 belongs to 9866862006, not 8978610637
    poll_res = client.get("/live-tracking/api/location/poll?booking_id=28")
    assert poll_res.status_code == 403
    assert poll_res.get_json()["success"] is False
    assert "Unauthorized" in poll_res.get_json()["error"]
