from datetime import date, timedelta
import logging

import mysql.connector
import app as app_module
from .conftest import csrf_token
from werkzeug.security import check_password_hash, generate_password_hash


def test_database_connection_and_cleanup(test_db):
    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        assert cursor.fetchone()[0] == test_db.database
    finally:
        cursor.close()


def test_customer_registration_and_duplicate_handling(app_client, test_db, seeded_data):
    token = csrf_token(app_client)
    response = app_client.post(
        "/register",
        data={
            "csrf_token": token,
            "name": "New Integration Customer",
            "phone": "9000000010",
            "email": "new-integration@test.example",
            "password": "customer-password",
        },
    )
    assert response.status_code == 200
    assert b"Registration Successful" in response.data

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT email, password_hash FROM users WHERE phone = %s",
            ("9000000010",),
        )
        registered_email, password_hash = cursor.fetchone()
        assert registered_email == "new-integration@test.example"
        assert check_password_hash(password_hash, "customer-password")
    finally:
        cursor.close()

    duplicate_token = csrf_token(app_client)
    duplicate = app_client.post(
        "/register",
        data={
            "csrf_token": duplicate_token,
            "name": "Duplicate Customer",
            "phone": "9000000010",
            "password": "customer-password",
        },
    )
    assert duplicate.status_code == 200
    assert b"Already Registered" in duplicate.data

    duplicate_email = app_client.post(
        "/register",
        data={
            "csrf_token": duplicate_token,
            "name": "Duplicate Email Customer",
            "phone": "9000000011",
            "email": "new-integration@test.example",
            "password": "customer-password",
        },
    )
    assert duplicate_email.status_code == 200
    assert b"Already Registered" in duplicate_email.data

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM users WHERE phone IN (%s, %s)", ("9000000010", "9000000011"))
        assert cursor.fetchone()[0] == 1
    finally:
        cursor.close()


def test_customer_registration_rejects_invalid_input(app_client, test_db):
    invalid_registrations = (
        {"name": "Invalid Phone", "phone": "123", "password": "customer-password"},
        {"name": "Invalid Email", "phone": "9000000012", "email": "not-an-email", "password": "customer-password"},
        {"name": "Short Password", "phone": "9000000013", "password": "short"},
        {"name": "Long Email", "phone": "9000000014", "email": "a" * 90 + "@test.example", "password": "customer-password"},
    )
    for registration in invalid_registrations:
        token = csrf_token(app_client)
        response = app_client.post(
            "/register",
            data={"csrf_token": token, **registration},
        )
        assert response.status_code == 400
        assert b"Invalid registration details" in response.data

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM users WHERE phone IN (%s, %s, %s)",
            ("9000000012", "9000000013", "9000000014"),
        )
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()


def test_customer_registration_database_failure_is_reported_without_secrets(
    app_client, test_db, monkeypatch, caplog
):
    class FailingCursor:
        def execute(self, *_args, **_kwargs):
            raise mysql.connector.Error(
                "simulated connection failure containing private-password",
                errno=2003,
            )

    monkeypatch.setattr(app_module, "cursor", FailingCursor())
    token = csrf_token(app_client)
    with caplog.at_level(logging.ERROR, logger="app"):
        response = app_client.post(
            "/register",
            data={
                "csrf_token": token,
                "name": "Database Failure Customer",
                "phone": "9000000015",
                "password": "customer-password",
            },
        )

    assert response.status_code == 503
    assert b"temporarily unavailable" in response.data
    assert "private-password" not in caplog.text

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM users WHERE phone = %s", ("9000000015",))
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()


def test_customer_registration_handles_duplicate_key_race(app_client, test_db, monkeypatch):
    class DuplicateKeyCursor:
        def __init__(self):
            self.execute_count = 0

        def execute(self, *_args, **_kwargs):
            self.execute_count += 1
            if self.execute_count == 2:
                raise mysql.connector.IntegrityError(
                    "duplicate value must not be exposed",
                    errno=1062,
                )

        def fetchone(self):
            return None

    monkeypatch.setattr(app_module, "cursor", DuplicateKeyCursor())
    token = csrf_token(app_client)
    response = app_client.post(
        "/register",
        data={
            "csrf_token": token,
            "name": "Concurrent Registration",
            "phone": "9000000017",
            "email": "race@test.example",
            "password": "customer-password",
        },
    )

    assert response.status_code == 200
    assert b"Already Registered" in response.data
    assert b"duplicate value must not be exposed" not in response.data

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM users WHERE phone = %s", ("9000000017",))
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()


def test_customer_registration_requires_csrf_without_saving(app_client, test_db):
    response = app_client.post(
        "/register",
        data={
            "name": "CSRF Failure Customer",
            "phone": "9000000016",
            "password": "customer-password",
        },
    )
    assert response.status_code == 400

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM users WHERE phone = %s", ("9000000016",))
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()

def test_customer_login_and_authentication(app_client, seeded_data):
    token = csrf_token(app_client)
    response = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/vehicles")
    assert app_client.get("/vehicles").status_code == 200


def test_owner_authentication_and_vehicle_crud(app_client, test_db, seeded_data):
    token = csrf_token(app_client)
    login = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "email": "owner@test.example",
            "password": "owner-password",
        },
    )
    assert login.status_code == 302

    token = csrf_token(app_client, "/addvehicle")
    created = app_client.post(
        "/addvehicle",
        data={
            "csrf_token": token,
            "vehicle_name": "Created Truck",
            "vehicle_type": "Lorry",
            "contact_number": "9000000001",
            "location": "Test Town",
        },
    )
    assert created.status_code == 200

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT id FROM vehicles WHERE vehicle_name = %s", ("Created Truck",))
        created_id = cursor.fetchone()[0]
    finally:
        cursor.close()

    token = csrf_token(app_client, "/myvehicles")
    updated = app_client.post(
        f"/editvehicle/{created_id}",
        data={
            "csrf_token": token,
            "vehicle_name": "Updated Truck",
            "vehicle_type": "Lorry",
            "contact_number": "9000000001",
            "location": "Test City",
        },
    )
    assert updated.status_code == 302

    token = csrf_token(app_client, "/myvehicles")
    deleted = app_client.post(
        f"/deletevehicle/{created_id}",
        data={"csrf_token": token},
    )
    assert deleted.status_code == 302


def test_vehicle_listing_details_search_and_filtering(app_client, seeded_data):
    token = csrf_token(app_client)
    login = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert login.status_code == 302

    assert app_client.get("/vehicles?q=Integration").status_code == 200
    assert app_client.get("/vehicles?vehicle_type=Pickup&min_rent=2000&max_rent=3000").status_code == 200
    details = app_client.get(f"/vehicle/{seeded_data['vehicle_id']}")
    assert details.status_code == 200
    assert b"Integration Truck" in details.data
    assert app_client.get("/vehicle/999999").status_code == 404


def test_customer_vehicle_listing_includes_multiple_owners_and_route_status(
    app_client, test_db, seeded_data
):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO owners (owner_name, phone, email, password_hash) "
            "VALUES (%s, %s, %s, %s)",
            (
                "Second Listing Owner",
                "9000000021",
                "second-listing-owner@test.example",
                generate_password_hash("owner-password"),
            ),
        )
        second_owner_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicles (vehicle_name, vehicle_type, owner_id, contact_number, location) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("Second Owner Route Truck", "Lorry", second_owner_id, "9000000021", "Second Town"),
        )
        routed_vehicle_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicle_routes (vehicle_id, from_location, to_location, rent) "
            "VALUES (%s, %s, %s, %s)",
            (routed_vehicle_id, "Second Town", "Second City", "3500.00"),
        )
        cursor.execute(
            "INSERT INTO vehicles (vehicle_name, vehicle_type, owner_id, contact_number, location) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("Second Owner Setup Truck", "Pickup", second_owner_id, "9000000021", "Setup Town"),
        )
        unconfigured_vehicle_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicles (vehicle_name, vehicle_type, owner_id, contact_number, location, is_active) "
            "VALUES (%s, %s, %s, %s, %s, FALSE)",
            ("Inactive Owner Truck", "Tipper", second_owner_id, "9000000021", "Inactive Town"),
        )
        test_db.commit()
    finally:
        cursor.close()

    token = csrf_token(app_client)
    login = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert login.status_code == 302

    response = app_client.get("/vehicles")
    assert response.status_code == 200
    assert b"Integration Truck" in response.data
    assert b"Integration Owner" in response.data
    assert b"Second Owner Route Truck" in response.data
    assert b"Second Listing Owner" in response.data
    assert b"Second Town" in response.data
    assert b"Second City" in response.data
    assert b"3500.00" in response.data
    assert b"Second Owner Setup Truck" in response.data
    assert b"No routes configured" in response.data
    assert b"Not bookable yet" in response.data
    assert b"Inactive Owner Truck" not in response.data
    assert f"/book/{routed_vehicle_id}?route_id=".encode() in response.data
    assert f"/book/{unconfigured_vehicle_id}?route_id=".encode() not in response.data

    details = app_client.get(f"/vehicle/{unconfigured_vehicle_id}")
    assert details.status_code == 200
    assert b"Second Listing Owner" in details.data
    assert b"not available for booking" in details.data
    assert b"Book This Vehicle" not in details.data

    assert app_client.get(f"/book/{unconfigured_vehicle_id}").status_code == 404


def test_booking_is_stored_and_visible_to_customer_and_owner(app_client, test_db, seeded_data):
    token = csrf_token(app_client)
    login = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert login.status_code == 302

    booking_date = (date.today() + timedelta(days=30)).isoformat()
    token = csrf_token(app_client, f"/book/{seeded_data['vehicle_id']}")
    booking = app_client.post(
        f"/book/{seeded_data['vehicle_id']}",
        data={"csrf_token": token, "booking_date": booking_date},
    )
    assert booking.status_code == 200
    assert b"Booking Successful" in booking.data
    assert b"Integration Owner" in booking.data
    customer_history = app_client.get("/mybookings")
    assert customer_history.status_code == 200
    assert b"Integration Owner" in customer_history.data
    assert app_client.get("/dashboard").status_code == 200

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT status, created_at FROM bookings WHERE vehicle_id = %s AND phone = %s",
            (seeded_data["vehicle_id"], "9000000002"),
        )
        created_booking = cursor.fetchone()
        assert created_booking[0].lower() == "pending"
        assert created_booking[1] is not None
        expected_booked_on = app_module.format_booking_created_at(created_booking[1])
    finally:
        cursor.close()

    with app_client.session_transaction() as session:
        session.clear()
    token = csrf_token(app_client)
    owner_login = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "email": "owner@test.example",
            "password": "owner-password",
        },
    )
    assert owner_login.status_code == 302
    owner_dashboard = app_client.get("/owner_dashboard")
    assert owner_dashboard.status_code == 200
    assert f"Booked on: {expected_booked_on}".encode() in owner_dashboard.data


def test_customer_booking_history_uses_authenticated_customer_id(
    app_client, test_db, seeded_data
):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (name, phone, password_hash, email) "
            "VALUES (%s, %s, %s, %s)",
            (
                "Other Integration Customer",
                "9000000022",
                generate_password_hash("other-customer-password"),
                "other-customer@test.example",
            ),
        )
        other_customer_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO bookings "
            "(vehicle_id, route_id, customer_name, phone, booking_date, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                seeded_data["vehicle_id"],
                seeded_data["route_id"],
                "Other Integration Customer",
                "9000000022",
                (date.today() + timedelta(days=40)).isoformat(),
                "Pending",
            ),
        )
        other_booking_id = cursor.lastrowid
        test_db.commit()
    finally:
        cursor.close()

    token = csrf_token(app_client)
    login = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert login.status_code == 302

    booking_date = (date.today() + timedelta(days=39)).isoformat()
    token = csrf_token(app_client, f"/book/{seeded_data['vehicle_id']}")
    created = app_client.post(
        f"/book/{seeded_data['vehicle_id']}",
        data={"csrf_token": token, "booking_date": booking_date},
    )
    assert created.status_code == 200
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT id FROM bookings WHERE phone = %s AND booking_date = %s",
            ("9000000002", booking_date),
        )
        customer_booking_id = cursor.fetchone()[0]
    finally:
        cursor.close()

    with app_client.session_transaction() as customer_session:
        assert customer_session["user_id"] == seeded_data["customer_id"]
        customer_session["user_phone"] = "9000000022"

    dashboard = app_client.get("/dashboard")
    history = app_client.get("/mybookings")
    expected_booking_link = f"/live-tracking/booking/{customer_booking_id}".encode()
    other_booking_link = f"/live-tracking/booking/{other_booking_id}".encode()
    assert expected_booking_link in dashboard.data
    assert other_booking_link not in dashboard.data
    assert expected_booking_link in history.data
    assert other_booking_link not in history.data

    with app_client.session_transaction() as customer_session:
        customer_session["user_id"] = other_customer_id
        customer_session["user_phone"] = "9000000002"
    other_dashboard = app_client.get("/dashboard")
    assert other_booking_link in other_dashboard.data
    assert expected_booking_link not in other_dashboard.data


def test_bookings_are_scoped_to_vehicle_owner_and_actions(app_client, test_db, seeded_data):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO owners (owner_name, phone, email, password_hash) "
            "VALUES (%s, %s, %s, %s)",
            ("Second Owner", "9000000011", "second-owner@test.example",
             generate_password_hash("owner-password")),
        )
        second_owner_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicles (vehicle_name, vehicle_type, owner_id, contact_number, location) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("Second Owner Truck", "Pickup", second_owner_id, "9000000011", "Other Town"),
        )
        second_vehicle_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicle_routes (vehicle_id, from_location, to_location, rent) "
            "VALUES (%s, %s, %s, %s)",
            (second_vehicle_id, "Other Town", "Other City", "3000.00"),
        )
        test_db.commit()
    finally:
        cursor.close()

    token = csrf_token(app_client)
    login = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert login.status_code == 302

    first_date = (date.today() + timedelta(days=30)).isoformat()
    second_date = (date.today() + timedelta(days=31)).isoformat()
    for vehicle_id, booking_date in (
        (seeded_data["vehicle_id"], first_date),
        (second_vehicle_id, second_date),
    ):
        token = csrf_token(app_client, f"/book/{vehicle_id}")
        response = app_client.post(
            f"/book/{vehicle_id}",
            data={"csrf_token": token, "booking_date": booking_date},
        )
        assert response.status_code == 200

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT id, vehicle_id, status FROM bookings WHERE vehicle_id IN (%s, %s) "
            "ORDER BY vehicle_id",
            (seeded_data["vehicle_id"], second_vehicle_id),
        )
        first_booking, second_booking = cursor.fetchall()
        assert first_booking[1] == seeded_data["vehicle_id"]
        assert second_booking[1] == second_vehicle_id
    finally:
        cursor.close()

    token = csrf_token(app_client)
    first_owner_login = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "email": "owner@test.example",
            "password": "owner-password",
        },
    )
    assert first_owner_login.status_code == 302
    first_owner_dashboard = app_client.get("/owner_dashboard")
    assert b"Integration Truck" in first_owner_dashboard.data
    assert b"Second Owner Truck" not in first_owner_dashboard.data

    token = csrf_token(app_client, "/owner_dashboard")
    for status in ("Accepted", "Rejected"):
        denied = app_client.post(
            f"/update_booking/{second_booking[0]}/{status}",
            data={"csrf_token": token},
        )
        assert denied.status_code == 403

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT status FROM bookings WHERE id = %s", (second_booking[0],))
        assert cursor.fetchone()[0] == "Pending"
    finally:
        cursor.close()

    with app_client.session_transaction() as session:
        session.clear()
    token = csrf_token(app_client)
    second_owner_login = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "email": "second-owner@test.example",
            "password": "owner-password",
        },
    )
    assert second_owner_login.status_code == 302
    second_owner_dashboard = app_client.get("/owner_dashboard")
    assert b"Second Owner Truck" in second_owner_dashboard.data
    assert b"Integration Truck" not in second_owner_dashboard.data

    token = csrf_token(app_client, "/owner_dashboard")
    denied = app_client.post(
        f"/update_booking/{first_booking[0]}/Rejected",
        data={"csrf_token": token},
    )
    assert denied.status_code == 403

    token = csrf_token(app_client, "/owner_dashboard")
    accepted = app_client.post(
        f"/update_booking/{second_booking[0]}/Accepted",
        data={"csrf_token": token},
    )
    assert accepted.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT status FROM bookings WHERE id = %s", (second_booking[0],))
        assert cursor.fetchone()[0] == "Accepted"
    finally:
        cursor.close()


def test_booking_form_rejects_missing_csrf_without_saving(app_client, test_db, seeded_data):
    token = csrf_token(app_client)
    login = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert login.status_code == 302

    booking_date = (date.today() + timedelta(days=45)).isoformat()
    response = app_client.post(
        f"/book/{seeded_data['vehicle_id']}",
        data={"booking_date": booking_date},
    )

    assert response.status_code == 400
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM bookings WHERE vehicle_id = %s AND booking_date = %s",
            (seeded_data["vehicle_id"], booking_date),
        )
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()


def test_booking_validation_and_duplicate_prevention(app_client, seeded_data):
    token = csrf_token(app_client)
    login = app_client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert login.status_code == 302

    token = csrf_token(app_client, f"/book/{seeded_data['vehicle_id']}")
    invalid = app_client.post(
        f"/book/{seeded_data['vehicle_id']}",
        data={"csrf_token": token, "booking_date": "not-a-date"},
    )
    assert invalid.status_code == 400

    token = csrf_token(app_client, f"/book/{seeded_data['vehicle_id']}")
    first = app_client.post(
        f"/book/{seeded_data['vehicle_id']}",
        data={"csrf_token": token, "booking_date": "2099-01-01"},
    )
    assert first.status_code == 200

    token = csrf_token(app_client, f"/book/{seeded_data['vehicle_id']}")
    duplicate = app_client.post(
        f"/book/{seeded_data['vehicle_id']}",
        data={"csrf_token": token, "booking_date": "2099-01-01"},
    )
    assert duplicate.status_code == 409


def test_owner_authorization_and_status_security(app_client, test_db, seeded_data):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO owners (owner_name, phone, email, password_hash) VALUES (%s, %s, %s, %s)",
            ("Other Owner", "9000000099", "other@test.example", generate_password_hash("owner-password")),
        )
        other_owner_id = cursor.lastrowid
        test_db.commit()
    finally:
        cursor.close()

    token = csrf_token(app_client)
    login = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "email": "other@test.example",
            "password": "owner-password",
        },
    )
    assert login.status_code == 302

    with app_client.session_transaction() as session:
        session.clear()
        session["owner_id"] = other_owner_id
    token = csrf_token(app_client, "/myvehicles")
    response = app_client.post(
        f"/editvehicle/{seeded_data['vehicle_id']}",
        data={
            "csrf_token": token,
            "vehicle_name": "Unauthorized",
            "vehicle_type": "Pickup",
            "contact_number": "9000000001",
            "location": "Test Town",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/myvehicles")
    assert app_client.get(f"/update_booking/1/Accepted").status_code == 405


def test_security_enforcement_and_csrf(app_client):
    assert app_client.get("/vehicles").location.endswith("/login")
    assert app_client.get("/owner_dashboard").location.endswith("/ownerlogin")
    assert app_client.post("/logout").status_code == 400
