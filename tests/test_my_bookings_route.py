import mysql.connector
import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


class BookingCursor:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))
        if self.error:
            raise self.error

    def fetchall(self):
        return self.rows


def login_customer_session(client, customer_id=1):
    with client.session_transaction() as session:
        session["user_id"] = customer_id
        session["user_name"] = "Test Customer"
        session["user_phone"] = "9000000002"


def test_my_bookings_route_shows_only_the_authenticated_customers_bookings(
    client, monkeypatch
):
    booking = (
        21,
        "Test Customer",
        "9000000002",
        "2026-11-24",
        "Pending",
        "Integration Truck",
        "Pickup",
        "Integration Owner",
        "Test Town",
        "Test City",
        "2500.00",
        None,
        None,
    )
    second_booking = (
        22,
        "Test Customer",
        "9000000002",
        "2026-12-01",
        "Confirmed",
        "Second Truck",
        "Van",
        "Integration Owner",
        "Other Town",
        "Other City",
        "1800.00",
        None,
        None,
    )
    rejected_booking = (
        23,
        "Test Customer",
        "9000000002",
        "2026-12-05",
        "Rejected",
        "Third Vehicle",
        "Cargo",
        "Integration Owner",
        "Town Three",
        "City Three",
        "1200.00",
        None,
        None,
    )
    cursor = BookingCursor([booking, second_booking, rejected_booking])
    monkeypatch.setattr(app_module, "cursor", cursor)
    login_customer_session(client, customer_id=37)

    response = client.get("/mybookings")

    assert response.status_code == 200
    assert b"Integration Truck" in response.data
    assert b"2026-11-24" in response.data
    assert b"2500.00" in response.data
    assert b"Pending" in response.data
    assert b"Second Truck" in response.data
    assert b"Third Vehicle" in response.data
    assert b'href="/live-tracking/booking/22"' in response.data
    assert response.data.count("View Live Tracking".encode()) == 1
    assert b'href="/live-tracking/booking/21"' not in response.data
    assert b'href="/live-tracking/booking/23"' not in response.data
    assert b"booking-tracking-map" not in response.data
    assert b"booking_tracking.js" not in response.data
    assert len(cursor.executed) == 1
    query, params = cursor.executed[0]
    assert "WHERE u.id = %s" in query
    assert params == (37,)


def test_dashboard_my_bookings_link_uses_registered_route(client, monkeypatch):
    monkeypatch.setattr(app_module, "cursor", BookingCursor())
    login_customer_session(client)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'href="/mybookings"' in response.data
    assert client.get("/my_bookings").status_code == 404


def test_my_bookings_requires_customer_authentication(client):
    response = client.get("/mybookings")

    assert response.status_code == 302
    assert response.location.endswith("/login")


def test_my_bookings_reports_database_outage_without_exposing_details(client, monkeypatch):
    cursor = BookingCursor(error=mysql.connector.Error("private database detail"))
    monkeypatch.setattr(app_module, "cursor", cursor)
    login_customer_session(client)

    response = client.get("/mybookings")

    assert response.status_code == 503
    assert b"temporarily unavailable" in response.data
    assert b"private database detail" not in response.data


def test_my_bookings_reports_uninitialized_database_cursor(client, monkeypatch):
    monkeypatch.setattr(app_module, "cursor", None)
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    login_customer_session(client)

    response = client.get("/mybookings")

    assert response.status_code == 503
    assert b"temporarily unavailable" in response.data
