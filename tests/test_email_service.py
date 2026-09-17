from unittest.mock import patch

from email_service import EmailConfigurationError, send_booking_notifications


BOOKING = {
    "booking_id": 17,
    "customer_name": "Test Customer",
    "customer_phone": "9000000002",
    "customer_email": "customer@test.example",
    "vehicle_name": "Test Truck",
    "owner_name": "Test Owner",
    "owner_email": "owner@test.example",
    "booking_date": "2099-01-01",
    "status": "Pending",
    "location": "Test Town",
    "from_location": "Test Town",
    "to_location": "Test City",
}


def configure_mail(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("MAIL_SERVER", "smtp.test.example")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USE_TLS", "true")
    monkeypatch.setenv("MAIL_DEFAULT_SENDER", "BusinessVehicleBooking <no-reply@test.example>")


def test_service_sends_both_notifications_with_booking_details(monkeypatch):
    configure_mail(monkeypatch)
    with patch("email_service._send", side_effect=[True, True]) as send:
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": True, "owner": True}
    assert send.call_count == 2
    assert send.call_args_list[0].args[0] == "customer@test.example"
    assert send.call_args_list[1].args[0] == "owner@test.example"
    assert "Test Customer" in send.call_args_list[0].args[3]["booking"]["customer_name"]
    assert send.call_args_list[0].args[3]["booking"]["booking_id"] == 17


def test_disabled_email_does_not_require_smtp_configuration(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "false")
    with patch("email_service._send") as send:
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}
    send.assert_not_called()


def test_missing_email_configuration_is_handled_without_exposing_secret(monkeypatch):
    configure_mail(monkeypatch)
    monkeypatch.delenv("MAIL_SERVER")

    with patch("email_service.logger.exception") as log:
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}
    assert all("MAIL_PASSWORD" not in str(call) for call in log.call_args_list)


def test_both_delivery_failures_return_without_raising(monkeypatch):
    configure_mail(monkeypatch)
    with patch("email_service._send", side_effect=RuntimeError("smtp unavailable")):
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}
