from pathlib import Path
from unittest.mock import Mock

import pytest

from app import app
import tracking_routes
import tracking_service


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def set_tracking_session(client, **values):
    with client.session_transaction() as session:
        session.update(values)
        session["csrf_token"] = "tracking-csrf-token"


def test_customer_cannot_start_update_or_stop_tracking(client):
    set_tracking_session(client, user_id=12, user_phone="9000000012")
    payload = {"csrf_token": "tracking-csrf-token", "vehicle_id": 3, "session_token": "known-token"}

    start = client.post("/live-tracking/api/session/start", json=payload)
    update = client.post(
        "/live-tracking/api/location/update",
        json={**payload, "latitude": 14.5, "longitude": 77.5},
    )
    stop = client.post("/live-tracking/api/session/stop", json=payload)

    assert start.status_code == update.status_code == stop.status_code == 403
    assert all(response.get_json()["success"] is False for response in (start, update, stop))


def test_owner_can_start_update_and_stop_tracking(client, monkeypatch):
    set_tracking_session(client, owner_id=7)
    start_session = Mock(
        return_value=(
            {
                "session_id": 20,
                "session_token": "owner-session-token",
                "access_token": "customer-view-token",
            },
            None,
        )
    )
    record_location = Mock(
        return_value=(
            True,
            {"latitude": 14.5, "longitude": 77.5, "recorded_at": "2026-10-25T00:00:00+00:00"},
        )
    )
    stop_session = Mock(return_value=(True, "Tracking session stopped successfully."))
    monkeypatch.setattr(tracking_routes, "start_tracking_session", start_session)
    monkeypatch.setattr(tracking_routes, "record_live_location", record_location)
    monkeypatch.setattr(tracking_routes, "stop_tracking_session", stop_session)

    start = client.post(
        "/live-tracking/api/session/start",
        json={
            "csrf_token": "tracking-csrf-token",
            "vehicle_id": 3,
            "booking_id": 15,
            "owner_id": 999,
        },
    )
    update = client.post(
        "/live-tracking/api/location/update",
        json={
            "csrf_token": "tracking-csrf-token",
            "session_token": "owner-session-token",
            "latitude": 14.5,
            "longitude": 77.5,
            "accuracy": 0,
        },
    )
    stop = client.post(
        "/live-tracking/api/session/stop",
        json={"csrf_token": "tracking-csrf-token", "session_token": "owner-session-token"},
    )

    assert start.status_code == update.status_code == stop.status_code == 200
    assert start_session.call_args.kwargs["owner_id"] == 7
    assert start_session.call_args.kwargs["booking_id"] == 15
    assert record_location.call_args.kwargs["owner_id"] == 7
    assert record_location.call_args.kwargs["lat"] == 14.5
    assert record_location.call_args.kwargs["lng"] == 77.5
    assert record_location.call_args.kwargs["accuracy"] == 0
    assert stop_session.call_args.kwargs["owner_id"] == 7


def test_location_update_api_keeps_accuracy_optional(client, monkeypatch):
    set_tracking_session(client, owner_id=7)
    record_location = Mock(return_value=(True, {"latitude": 14.5, "longitude": 77.5}))
    monkeypatch.setattr(tracking_routes, "record_live_location", record_location)

    response = client.post(
        "/live-tracking/api/location/update",
        json={
            "csrf_token": "tracking-csrf-token",
            "session_token": "owner-session-token",
            "latitude": 14.5,
            "longitude": 77.5,
        },
    )

    assert response.status_code == 200
    assert record_location.call_args.kwargs["accuracy"] is None


def test_tracking_poll_database_failure_returns_sanitized_service_error(client, monkeypatch, caplog):
    from mysql.connector import Error

    set_tracking_session(client, user_id=12, user_phone="9000000012")
    monkeypatch.setattr(
        tracking_routes,
        "get_live_tracking_data_by_booking",
        Mock(side_effect=Error("private connection details")),
    )

    with caplog.at_level("ERROR", logger="tracking_routes"):
        response = client.get("/live-tracking/api/location/poll?booking_id=26")

    assert response.status_code == 503
    assert response.get_json()["error"] == "Tracking data is temporarily unavailable."
    assert "private connection details" not in response.get_data(as_text=True)
    assert "private connection details" not in caplog.text


def test_database_error_from_location_update_is_logged_and_sanitized(client, monkeypatch, caplog):
    set_tracking_session(client, owner_id=7)
    monkeypatch.setattr(
        tracking_routes,
        "record_live_location",
        Mock(return_value=(False, "Database error: secret session value")),
    )

    with caplog.at_level("ERROR", logger="tracking_routes"):
        response = client.post(
            "/live-tracking/api/location/update",
            json={
                "csrf_token": "tracking-csrf-token",
                "session_token": "do-not-log-this-token",
                "latitude": 14.5,
                "longitude": 77.5,
            },
        )

    assert response.status_code == 503
    assert response.get_json()["error"] == "Unable to save GPS coordinates right now. Please retry shortly."
    assert "secret session value" not in response.get_data(as_text=True)
    assert "do-not-log-this-token" not in caplog.text


def test_location_service_saves_latest_and_history_coordinates(monkeypatch):
    class Cursor:
        def __init__(self):
            self.queries = []
            self.closed = False

        def execute(self, query, params):
            self.queries.append((query, params))

        def fetchone(self):
            return {
                "id": 18,
                "owner_id": 7,
                "vehicle_id": 3,
                "booking_id": 15,
                "status": "ACTIVE",
            }

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self):
            self.db_cursor = Cursor()
            self.committed = False
            self.closed = False

        def cursor(self, dictionary=False):
            assert dictionary is True
            return self.db_cursor

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("valid coordinates should not roll back")

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(tracking_service, "get_db_connection", lambda: connection)

    success, saved = tracking_service.record_live_location(
        owner_id=7,
        session_token="authorized-driver-token",
        lat=14.5,
        lng=77.5,
        accuracy=8,
        speed=2,
        heading=90,
        altitude=400,
    )

    assert success is True
    assert saved["latitude"] == 14.5
    assert saved["longitude"] == 77.5
    location_writes = [
        params
        for query, params in connection.db_cursor.queries
        if "INSERT INTO vehicle_live_locations" in query
        or "INSERT INTO vehicle_location_history" in query
    ]
    assert len(location_writes) == 2
    assert all(params[0:2] == (18, 3) for params in location_writes)
    assert all(params[2:4] == (14.5, 77.5) for params in location_writes)
    assert connection.committed is True
    assert connection.closed is True
    assert "b.status IN ('Accepted', 'Confirmed')" in connection.db_cursor.queries[0][0]


def test_owner_cannot_control_another_owners_session(client, monkeypatch):
    set_tracking_session(client, owner_id=7)
    record_location = Mock(
        return_value=(False, "Unauthorized: tracking session does not belong to this owner.")
    )
    stop_session = Mock(return_value=(False, "Active tracking session not found or already stopped."))
    monkeypatch.setattr(tracking_routes, "record_live_location", record_location)
    monkeypatch.setattr(tracking_routes, "stop_tracking_session", stop_session)

    update = client.post(
        "/live-tracking/api/location/update",
        json={
            "csrf_token": "tracking-csrf-token",
            "session_token": "another-owner-token",
            "latitude": 14.5,
            "longitude": 77.5,
            "owner_id": 999,
        },
    )
    stop = client.post(
        "/live-tracking/api/session/stop",
        json={
            "csrf_token": "tracking-csrf-token",
            "session_token": "another-owner-token",
            "owner_id": 999,
        },
    )

    assert update.status_code == stop.status_code == 403
    assert record_location.call_args.kwargs["owner_id"] == 7
    assert stop_session.call_args.kwargs["owner_id"] == 7


def test_tracking_control_endpoints_still_require_csrf(client):
    with client.session_transaction() as session:
        session["owner_id"] = 7

    response = client.post(
        "/live-tracking/api/location/update",
        json={"session_token": "token", "latitude": 14.5, "longitude": 77.5},
    )

    assert response.status_code == 400
    assert response.get_data(as_text=True) == "Invalid CSRF token"


def test_customer_tracking_page_only_polls_and_renders_map():
    project_root = Path(__file__).resolve().parents[1]
    template = (project_root / "templates" / "customer_live_tracking.html").read_text(encoding="utf-8")
    script = (project_root / "static" / "customer_tracking.js").read_text(encoding="utf-8")
    fullscreen_template = (
        project_root / "templates" / "customer_tracking_fullscreen.html"
    ).read_text(encoding="utf-8")

    assert 'id="trackingMap"' in template
    assert "customer_tracking.js" in template
    assert "POLL_INTERVAL_MS = 5000" in script
    assert "setInterval(pollLiveLocation, POLL_INTERVAL_MS)" in script
    assert "Live tracking has not started yet." in script
    assert ">🛰️ Live Tracking</h2>" in template
    assert "Live tracking has not started yet." in template
    assert "const latLng = asCoordinatePair([data.location.latitude, data.location.longitude]);" in script
    assert "vehicleMarker = L.marker(latLng" in script
    assert "navigator.geolocation" not in script
    assert "/api/location/update" not in script
    fullscreen_script = script
    assert "Route distance temporarily unavailable" in fullscreen_script
    assert 'color: "#1a73e8"' in fullscreen_script
    assert 'color: "#1a73e8"' in script
    assert "L.polyline(route.coordinates" in script
    assert "renderEndpoints(" in script
    assert "data.booking.pickup_coords" in script
    assert "updateBookedRoute()" in script
    assert "updateRemainingRoute(latLng)" in script
    assert "Route distance temporarily unavailable" in script
    assert '"ananthapuram": [14.6819, 77.6006]' not in script
    assert '<html lang="en">' in fullscreen_template
    assert '"https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"' in script
    assert "basemaps.cartocdn.com" not in script
    assert "maxZoom: 19" in script
    assert "geocodePlace" not in script
    assert "api-key" not in fullscreen_template.lower()
    assert "maps.googleapis.com" not in fullscreen_template.lower()
    assert "cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js" in fullscreen_template
    assert "cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css" in fullscreen_template
    assert 'id="mapLoadStatus"' in fullscreen_template
    assert "height: 100dvh" in fullscreen_template
    fullscreen_css = (
        project_root / "static" / "customer_tracking_fullscreen.css"
    ).read_text(encoding="utf-8")
    assert "width: 100vw" in fullscreen_css
    assert "height: 100vh" in fullscreen_css
    assert "height: 100dvh" in fullscreen_css


def test_fullscreen_booking_tracking_requires_customer_login(client):
    response = client.get("/live-tracking/booking/15")

    assert response.status_code == 302
    assert response.location.endswith("/login")


def test_fullscreen_booking_tracking_renders_only_authorized_booking(client, monkeypatch):
    set_tracking_session(client, user_id=12, user_phone="9000000012")
    tracking_data = {
        "tracking_state": "LIVE",
        "last_updated": "2026-10-31 11:00:00",
        "location": {"latitude": 14.5, "longitude": 77.5},
        "vehicle": {"name": "Authorized Truck", "type": "Pickup"},
        "booking": {
            "id": 15,
            "status": "Confirmed",
            "pickup": "Pickup Town",
            "destination": "Destination City",
        },
    }
    tracking_lookup = Mock(return_value=(tracking_data, None))
    monkeypatch.setattr(tracking_routes, "get_live_tracking_data_by_booking", tracking_lookup)

    response = client.get("/live-tracking/booking/15")

    assert response.status_code == 200
    assert b"Authorized Truck" in response.data
    assert b"Recenter Vehicle" in response.data
    assert b"Full Screen" in response.data
    assert b"Back to My Bookings" in response.data
    assert b"valRemainingDistance" in response.data
    assert b"valEstDuration" in response.data
    assert b"access_token" not in response.data
    fullscreen_css = (
        Path(__file__).resolve().parents[1] / "static" / "customer_tracking_fullscreen.css"
    ).read_text(encoding="utf-8")
    assert "width: 100vw" in fullscreen_css
    assert "height: 100vh" in fullscreen_css
    assert tracking_lookup.call_args.kwargs["booking_id"] == 15
    assert tracking_lookup.call_args.kwargs["customer_phone"] == "9000000012"
    assert tracking_lookup.call_args.kwargs.get("owner_id") is None


def test_fullscreen_booking_tracking_rejects_another_customers_booking(client, monkeypatch):
    set_tracking_session(client, user_id=12, user_phone="9000000012")
    monkeypatch.setattr(
        tracking_routes,
        "get_live_tracking_data_by_booking",
        Mock(return_value=(None, "Unauthorized: You do not have permission to view tracking for this booking.")),
    )

    response = client.get("/live-tracking/booking/16")

    assert response.status_code == 403
    assert b"You are not authorized to view this booking." in response.data
    assert b'id="trackingMap"' not in response.data


def test_fullscreen_tracking_uses_each_requested_bookings_location(client, monkeypatch):
    set_tracking_session(client, user_id=12, user_phone="9000000012")
    locations = {
        15: (14.5, 77.5),
        16: (19.1, 72.9),
    }

    def tracking_lookup(booking_id, customer_phone=None, customer_name=None, owner_id=None):
        latitude, longitude = locations[booking_id]
        return {
            "tracking_state": "LIVE",
            "last_updated": "2026-10-31 11:00:00",
            "location": {"latitude": latitude, "longitude": longitude},
            "vehicle": {"name": f"Vehicle {booking_id}", "type": "Truck"},
            "booking": {
                "id": booking_id,
                "status": "Confirmed",
                "pickup": "Pickup",
                "destination": "Destination",
            },
        }, None

    monkeypatch.setattr(tracking_routes, "get_live_tracking_data_by_booking", tracking_lookup)

    first = client.get("/live-tracking/booking/15")
    second = client.get("/live-tracking/booking/16")

    assert first.status_code == second.status_code == 200
    assert b"Vehicle 15" in first.data
    assert b"14.500000, 77.500000" in first.data
    assert b"Vehicle 16" in second.data
    assert b"19.100000, 72.900000" in second.data
    assert b"14.500000, 77.500000" not in second.data


def test_customer_booking_view_shows_not_started_message_without_driver_controls(client, monkeypatch):
    set_tracking_session(client, user_id=12, user_phone="9000000012")
    booking_data = {
        "tracking_state": "NOT_STARTED",
        "booking": {
            "id": 15,
            "customer_name": "Test Customer",
            "booking_date": "2026-10-31",
            "status": "Confirmed",
            "pickup": "Pickup Town",
            "destination": "Destination City",
            "rent": "2500.00",
        },
        "vehicle": {
            "id": 3,
            "name": "Test Truck",
            "type": "Pickup",
            "registration_number": "VH-003",
            "base_location": "Pickup Town",
            "owner_name": "Test Owner",
            "contact_number": "9000000001",
        },
        "location": None,
    }
    monkeypatch.setattr(
        tracking_routes,
        "get_live_tracking_data_by_booking",
        Mock(return_value=(booking_data, None)),
    )

    response = client.get("/live-tracking/customer?booking_id=15")

    assert response.status_code == 200
    assert b"Live tracking has not started yet." in response.data
    assert b"Test Truck" in response.data
    assert b"Destination City" in response.data
    assert response.data.index(b"Trip & Route Details") < response.data.index(b'id="trackingMap"')
    assert b"btnStartTracking" not in response.data
    assert b"btnStopTracking" not in response.data


def test_customer_tracking_poll_keeps_multiple_bookings_isolated(client, monkeypatch):
    set_tracking_session(client, user_id=12, user_phone="9000000012")
    requested_bookings = []
    booking_payloads = {
        21: {
            "booking": {"id": 21, "status": "Pending"},
            "tracking_state": "NOT_STARTED",
            "location": None,
        },
        22: {
            "booking": {"id": 22, "status": "Confirmed"},
            "tracking_state": "LIVE",
            "location": {"latitude": 14.5, "longitude": 77.5},
        },
    }

    def tracking_for_booking(booking_id, customer_phone=None, customer_name=None, owner_id=None):
        requested_bookings.append(booking_id)
        return booking_payloads[booking_id], None

    monkeypatch.setattr(
        tracking_routes,
        "get_live_tracking_data_by_booking",
        tracking_for_booking,
    )

    pending = client.get("/live-tracking/api/location/poll?booking_id=21")
    active = client.get("/live-tracking/api/location/poll?booking_id=22")

    assert pending.status_code == active.status_code == 200
    assert pending.get_json()["data"]["booking"]["id"] == 21
    assert pending.get_json()["data"]["tracking_state"] == "NOT_STARTED"
    assert pending.get_json()["data"]["location"] is None
    assert active.get_json()["data"]["booking"]["id"] == 22
    assert active.get_json()["data"]["tracking_state"] == "LIVE"
    assert active.get_json()["data"]["location"] == {"latitude": 14.5, "longitude": 77.5}
    assert requested_bookings == [21, 22]
