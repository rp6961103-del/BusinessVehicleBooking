from datetime import date, timedelta
from unittest.mock import patch

from .conftest import csrf_token
from werkzeug.security import generate_password_hash


def test_database_connection_and_cleanup(test_db):
    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        assert cursor.fetchone()[0] == test_db.database
    finally:
        cursor.close()


def test_customer_registration_is_removed(app_client, seeded_data):
    assert app_client.get("/register").status_code == 404
    assert app_client.post("/register").status_code == 404


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
    assert b"Booking Request Submitted" in booking.data
    assert app_client.get("/mybookings").status_code == 200

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT status FROM bookings WHERE vehicle_id = %s AND phone = %s",
            (seeded_data["vehicle_id"], "9000000002"),
        )
        assert cursor.fetchone()[0].lower() == "pending"
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
    assert app_client.get("/owner_dashboard").status_code == 200


def test_owner_decision_updates_pending_booking_and_emails_customer(app_client, test_db, seeded_data):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO bookings (vehicle_id, route_id, customer_name, phone, booking_date, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (seeded_data["vehicle_id"], 1, "Integration Customer", "9000000002", "2099-02-01", "Pending"),
        )
        booking_id = cursor.lastrowid
        test_db.commit()
    finally:
        cursor.close()


def test_owner_dashboard_renders_actions_only_for_pending_bookings(app_client, test_db, seeded_data):
    cursor = test_db.cursor()
    try:
        booking_ids = {}
        for status in ("Pending", "Accepted", "Rejected"):
            cursor.execute(
                "INSERT INTO bookings (vehicle_id, customer_name, phone, booking_date, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (seeded_data["vehicle_id"], f"{status} Customer", "9000000002", "2099-03-01", status),
            )
            booking_ids[status] = cursor.lastrowid
        test_db.commit()
    finally:
        cursor.close()

    token = csrf_token(app_client)
    response = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "email": "owner@test.example",
            "password": "owner-password",
        },
    )
    assert response.status_code == 302

    dashboard = app_client.get("/owner_dashboard")
    assert dashboard.status_code == 200
    assert b"customer@test.example" in dashboard.data
    assert f"/update_booking/{booking_ids['Pending']}/Accepted".encode() in dashboard.data
    assert f"/update_booking/{booking_ids['Pending']}/Rejected".encode() in dashboard.data
    assert f"/update_booking/{booking_ids['Accepted']}/Accepted".encode() not in dashboard.data
    assert f"/update_booking/{booking_ids['Rejected']}/Rejected".encode() not in dashboard.data

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

    token = csrf_token(app_client, "/owner_dashboard")
    with patch("app.send_booking_decision_email", return_value=True) as send:
        response = app_client.post(
            f"/update_booking/{booking_id}/Accepted",
            data={"csrf_token": token},
        )

    assert response.status_code == 302
    send.assert_called_once()
    assert send.call_args.args[0]["status"] == "Accepted"
    assert send.call_args.args[0]["customer_email"] == "customer@test.example"

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
        assert cursor.fetchone()[0] == "Accepted"
    finally:
        cursor.close()


def test_demo_payment_flow_for_accepted_booking(app_client, test_db, seeded_data):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO bookings (vehicle_id, route_id, customer_name, phone, booking_date, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (seeded_data["vehicle_id"], 1, "Integration Customer", "9000000002", "2099-04-01", "Accepted"),
        )
        booking_id = cursor.lastrowid
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

    my_bookings = app_client.get("/mybookings")
    assert my_bookings.status_code == 200
    assert b"Pay Now" in my_bookings.data

    pay_page = app_client.get(f"/booking/{booking_id}/pay")
    assert pay_page.status_code == 200
    assert b"Secure Demo Payment" in pay_page.data

    token = csrf_token(app_client, f"/booking/{booking_id}/pay")
    response = app_client.post(
        f"/booking/{booking_id}/pay",
        data={
            "csrf_token": token,
            "payment_method": "Demo Card",
            "cardholder_name": "Demo Customer",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith(f"/booking/{booking_id}/payment-success")

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT status, payment_method, transaction_reference FROM payments WHERE booking_id = %s",
            (booking_id,),
        )
        payment = cursor.fetchone()
        assert payment is not None
        assert payment[0] == "Paid"
        assert payment[1] == "Demo Card"
        assert payment[2].startswith("DEMO-")
    finally:
        cursor.close()

    duplicate = app_client.post(
        f"/booking/{booking_id}/pay",
        data={
            "csrf_token": csrf_token(app_client, f"/booking/{booking_id}/pay"),
            "payment_method": "Demo UPI",
            "demo_upi_id": "demo@upi",
        },
    )
    assert duplicate.status_code in {302, 400, 403}


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
