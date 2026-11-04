from unittest.mock import patch

from .conftest import csrf_token, login_customer, login_owner


def enable_email(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("MAIL_SERVER", "smtp.test.example")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USE_TLS", "true")
    monkeypatch.setenv("MAIL_DEFAULT_SENDER", "BusinessVehicleBooking <no-reply@test.example>")


def create_booking(client, vehicle_id):
    token = csrf_token(client, f"/book/{vehicle_id}")
    return client.post(
        f"/book/{vehicle_id}",
        data={"csrf_token": token, "booking_date": "2099-01-01"},
    )


def test_booking_attempts_customer_and_owner_email(app_client, seeded_data, monkeypatch):
    enable_email(monkeypatch)
    login_customer(app_client, {"phone": "9000000002"})

    with patch("app.send_booking_notifications", return_value={"customer": True, "owner": True}) as send:
        response = create_booking(app_client, seeded_data["vehicle_id"])

    assert response.status_code == 200
    send.assert_called_once()
    booking = send.call_args.args[0]
    assert booking["customer_email"] == "customer@test.example"
    assert booking["owner_email"] == "owner@test.example"
    assert booking["vehicle_name"] == "Integration Truck"
    assert booking["booking_id"]
    assert b"Customer confirmation:" in response.data
    assert b"Owner notification:" in response.data
    assert b"inbox delivery is not verified" in response.data


def test_email_failure_does_not_cancel_booking(app_client, test_db, seeded_data, monkeypatch):
    enable_email(monkeypatch)
    login_customer(app_client, {"phone": "9000000002"})

    with patch("app.send_booking_notifications", side_effect=RuntimeError("smtp unavailable")):
        response = create_booking(app_client, seeded_data["vehicle_id"])

    assert response.status_code == 200
    assert b"booking is saved in the system" in response.data
    assert b"not submitted successfully" in response.data
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM bookings WHERE vehicle_id = %s AND booking_date = %s",
            (seeded_data["vehicle_id"], "2099-01-01"),
        )
        assert cursor.fetchone()[0] == 1
    finally:
        cursor.close()


def test_disabled_email_does_not_attempt_delivery(app_client, seeded_data, monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "false")
    login_customer(app_client, {"phone": "9000000002"})

    with patch("app.send_booking_notifications") as send:
        response = create_booking(app_client, seeded_data["vehicle_id"])

    assert response.status_code == 200
    assert b"Email notifications are disabled" in response.data
    send.assert_called_once()


def test_booking_validation_still_rejects_invalid_date(app_client, seeded_data, monkeypatch):
    enable_email(monkeypatch)
    login_customer(app_client, {"phone": "9000000002"})
    token = csrf_token(app_client, f"/book/{seeded_data['vehicle_id']}")

    with patch("app.send_booking_notifications") as send:
        response = app_client.post(
            f"/book/{seeded_data['vehicle_id']}",
            data={"csrf_token": token, "booking_date": "not-a-date"},
        )

    assert response.status_code == 400
    send.assert_not_called()


def test_owner_status_update_sends_email_to_customer(app_client, test_db, seeded_data, monkeypatch):
    enable_email(monkeypatch)
    login_customer(app_client, {"phone": "9000000002"})
    with patch("app.send_booking_notifications", return_value={"customer": True, "owner": True}):
        create_booking(app_client, seeded_data["vehicle_id"])

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT id FROM bookings WHERE vehicle_id = %s AND booking_date = %s",
            (seeded_data["vehicle_id"], "2099-01-01"),
        )
        booking_id = cursor.fetchone()[0]
    finally:
        cursor.close()

    owner_client = app_client.application.test_client()
    login_owner(
        owner_client,
        {"email": "owner@test.example"},
    )
    with owner_client.session_transaction() as session:
        token = session["csrf_token"]
    with patch("app.send_booking_status_notification_to_customer", return_value=True) as send:
        response = owner_client.post(
            f"/update_booking/{booking_id}/Accepted",
            data={"csrf_token": token},
        )

    assert response.status_code == 302
    send.assert_called_once()
    booking = send.call_args.args[0]
    assert booking["customer_email"] == "customer@test.example"
    assert booking["booking_id"] == booking_id
    assert booking["status"] == "Accepted"