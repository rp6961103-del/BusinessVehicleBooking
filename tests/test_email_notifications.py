from unittest.mock import patch

from .conftest import csrf_token, login_customer


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


def test_pending_booking_notifies_owner_only(app_client, seeded_data, monkeypatch):
    enable_email(monkeypatch)
    login_customer(app_client, {"phone": "9000000002"})

    with patch("app.send_booking_notifications", return_value={"customer": True, "owner": True}) as send:
        response = create_booking(app_client, seeded_data["vehicle_id"])

    assert response.status_code == 200
    send.assert_called_once()
    assert send.call_args.kwargs["include_customer"] is False
    booking = send.call_args.args[0]
    assert booking["customer_email"] == "customer@test.example"
    assert booking["owner_email"] == "owner@test.example"
    assert booking["vehicle_name"] == "Integration Truck"
    assert booking["booking_id"]


def test_email_failure_does_not_cancel_booking(app_client, test_db, seeded_data, monkeypatch):
    enable_email(monkeypatch)
    login_customer(app_client, {"phone": "9000000002"})

    with patch("app.send_booking_notifications", side_effect=RuntimeError("smtp unavailable")):
        response = create_booking(app_client, seeded_data["vehicle_id"])

    assert response.status_code == 200
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