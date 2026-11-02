from unittest.mock import MagicMock, patch

from email_service import (
    _send,
    email_configuration_status,
    send_password_reset_email,
    send_booking_notifications,
    send_booking_decision_to_customer,
)


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
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("MAIL_SERVER", "smtp.test.example")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USE_TLS", "true")
    monkeypatch.setenv("MAIL_DEFAULT_SENDER", "BusinessVehicleBooking <no-reply@test.example>")


def configure_google_script(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("EMAIL_PROVIDER", "google_script")
    monkeypatch.setenv("GOOGLE_EMAIL_WEBHOOK_URL", "https://script.google.com/macros/s/test/exec")
    monkeypatch.setenv("GOOGLE_EMAIL_WEBHOOK_TOKEN", "test-webhook-token")


class MockWebhookResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("Invalid JSON")
        return self._json_data


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


def test_booking_decision_notifications_can_be_sent_repeatedly(monkeypatch):
    configure_mail(monkeypatch)
    with patch("email_service._send", return_value=True) as send:
        results = [
            send_booking_decision_to_customer({**BOOKING, "status": "Accepted"})
            for _ in range(3)
        ]

    assert results == [True, True, True]
    assert send.call_count == 3
    assert all(call.args[0] == "customer@test.example" for call in send.call_args_list)
    assert all(call.args[1] == "BusinessVehicleBooking - Booking #17 Accepted" for call in send.call_args_list)


def test_disabled_email_does_not_require_smtp_configuration(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "false")
    with patch("email_service._send") as send:
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}
    send.assert_not_called()


def test_missing_email_configuration_is_handled_without_exposing_secret(monkeypatch):
    configure_mail(monkeypatch)
    monkeypatch.delenv("MAIL_SERVER")

    with patch("email_service.logger.error") as log:
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}
    assert log.call_count == 1
    assert all("MAIL_PASSWORD" not in str(call) for call in log.call_args_list)


def test_both_delivery_failures_return_without_raising(monkeypatch):
    configure_mail(monkeypatch)
    with patch("email_service._send", side_effect=RuntimeError("smtp unavailable")):
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}


def test_configuration_status_reports_disabled_and_missing_settings(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "false")
    disabled = email_configuration_status()
    assert disabled["message"].startswith(
        "Email notifications are disabled. Set MAIL_ENABLED=true to enable them."
    )
    assert all(name in disabled["message"] for name in ("MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER"))

    monkeypatch.setenv("MAIL_ENABLED", "true")
    for name in ("MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER"):
        monkeypatch.delenv(name, raising=False)
    status = email_configuration_status()
    assert status["configured"] is False
    assert all(name in status["message"] for name in ("MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER"))


def test_configuration_rejects_invalid_port_tls_and_auth_pair(monkeypatch):
    configure_mail(monkeypatch)
    monkeypatch.setenv("MAIL_PORT", "not-a-port")
    assert "MAIL_PORT" in email_configuration_status()["message"]

    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USE_SSL", "true")
    assert "not both" in email_configuration_status()["message"]

    monkeypatch.setenv("MAIL_USE_SSL", "false")
    monkeypatch.setenv("MAIL_USERNAME", "test-user")
    monkeypatch.delenv("MAIL_PASSWORD", raising=False)
    assert "MAIL_USERNAME and MAIL_PASSWORD" in email_configuration_status()["message"]


def test_google_script_configuration_does_not_require_smtp(monkeypatch):
    configure_google_script(monkeypatch)
    for name in (
        "MAIL_SERVER",
        "MAIL_PORT",
        "MAIL_DEFAULT_SENDER",
        "MAIL_USERNAME",
        "MAIL_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    assert email_configuration_status() == {
        "enabled": True,
        "configured": True,
        "message": "",
    }


def test_google_script_sends_customer_and_owner_notifications_over_https(monkeypatch):
    configure_google_script(monkeypatch)
    with patch("email_service.requests.post") as post:
        post.return_value = MockWebhookResponse(200, {"success": True})
        with patch("email_service.render_template", return_value="<p>Booking details</p>"):
            results = send_booking_notifications(BOOKING)

    assert results == {"customer": True, "owner": True}
    assert post.call_count == 2
    assert post.call_args_list[0].args[0] == "https://script.google.com/macros/s/test/exec"
    assert post.call_args_list[0].kwargs["json"] == {
        "token": "test-webhook-token",
        "to": "customer@test.example",
        "subject": "Vehicle Booking Confirmation - Booking #17",
        "html": "<p>Booking details</p>",
    }
    assert post.call_args_list[1].kwargs["json"]["to"] == "owner@test.example"
    assert all(call.kwargs["timeout"] == 10 for call in post.call_args_list)


def test_google_script_rejects_failed_http_response_and_invalid_success(monkeypatch):
    configure_google_script(monkeypatch)
    with patch("email_service.requests.post", side_effect=[
        MockWebhookResponse(503, {"success": False}),
        MockWebhookResponse(200, {"success": False}),
    ]):
        with patch("email_service.render_template", return_value="Booking details"):
            results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}


def test_google_script_configuration_requires_https_and_credentials(monkeypatch):
    configure_google_script(monkeypatch)
    monkeypatch.setenv("GOOGLE_EMAIL_WEBHOOK_URL", "http://script.google.com/macros/s/test/exec")
    assert email_configuration_status()["message"] == (
        "GOOGLE_EMAIL_WEBHOOK_URL must be an HTTPS URL."
    )

    monkeypatch.delenv("GOOGLE_EMAIL_WEBHOOK_TOKEN")
    assert "GOOGLE_EMAIL_WEBHOOK_TOKEN" in email_configuration_status()["message"]


def test_smtp_submission_uses_starttls_and_reports_smtp_acceptance(monkeypatch):
    configure_mail(monkeypatch)
    monkeypatch.setenv("MAIL_USERNAME", "smtp-user")
    monkeypatch.setenv("MAIL_PASSWORD", "smtp-secret")
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.send_message.return_value = {}

    with patch("email_service.smtplib.SMTP", return_value=smtp) as smtp_factory:
        with patch("email_service.render_template", return_value="Booking details"):
            accepted = _send(
                "customer@test.example",
                "Booking confirmation",
                "emails/booking_confirmation.txt",
                {"booking": BOOKING},
            )

    assert accepted is True
    smtp_factory.assert_called_once_with("smtp.test.example", 587, timeout=10)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("smtp-user", "smtp-secret")
    smtp.send_message.assert_called_once()


def test_smtp_ssl_mode_and_refused_recipient(monkeypatch):
    configure_mail(monkeypatch)
    monkeypatch.setenv("MAIL_PORT", "465")
    monkeypatch.setenv("MAIL_USE_TLS", "false")
    monkeypatch.setenv("MAIL_USE_SSL", "true")
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.send_message.return_value = {"customer@test.example": (550, b"rejected")}

    with patch("email_service.smtplib.SMTP_SSL", return_value=smtp) as smtp_factory:
        with patch("email_service.render_template", return_value="Booking details"):
            accepted = _send(
                "customer@test.example",
                "Booking confirmation",
                "emails/booking_confirmation.txt",
                {"booking": BOOKING},
            )

    assert accepted is False
    assert smtp_factory.call_args.args[:2] == ("smtp.test.example", 465)
    smtp.starttls.assert_not_called()


def test_password_reset_email_uses_existing_mail_sender_without_logging_token(monkeypatch):
    configure_mail(monkeypatch)
    reset_link = "https://booking.example/reset-password/customer#one-time-secret-token"

    with patch("email_service._send", return_value=True) as send:
        accepted = send_password_reset_email(
            "customer@test.example",
            reset_link,
            "customer",
        )

    assert accepted is True
    assert send.call_args.args[:3] == (
        "customer@test.example",
        "Business Vehicle Booking - Password Reset",
        "emails/password_reset.txt",
    )
    assert send.call_args.args[3] == {
        "reset_url": reset_link,
        "account_type": "customer",
    }


def test_password_reset_email_respects_disabled_mail_configuration(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "false")
    with patch("email_service._send") as send:
        accepted = send_password_reset_email(
            "customer@test.example",
            "https://booking.example/reset",
            "customer",
        )

    assert accepted is False
    send.assert_not_called()
