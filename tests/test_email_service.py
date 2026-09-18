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


class MockResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        if self._json_data is None:
            raise ValueError("Invalid JSON")
        return self._json_data


def configure_google_script(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("EMAIL_PROVIDER", "google_script")
    monkeypatch.setenv("GOOGLE_EMAIL_WEBHOOK_URL", "https://script.google.com/macros/s/TEST_SCRIPT/exec")
    monkeypatch.setenv("GOOGLE_EMAIL_WEBHOOK_TOKEN", "test-secret-token-123")


def test_google_script_send_success(monkeypatch):
    from app import app

    configure_google_script(monkeypatch)

    with app.test_request_context(), patch("email_service.requests.post") as mock_post:
        mock_post.return_value = MockResponse(status_code=200, json_data={"success": True})
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": True, "owner": True}
    assert mock_post.call_count == 2
    # Verify the first call (customer)
    first_call = mock_post.call_args_list[0]
    assert first_call.args[0] == "https://script.google.com/macros/s/TEST_SCRIPT/exec"
    payload = first_call.kwargs["json"]
    assert payload["token"] == "test-secret-token-123"
    assert payload["to"] == "customer@test.example"
    assert "Vehicle Booking Confirmation" in payload["subject"]
    assert "Test Customer" in payload["html"]
    assert first_call.kwargs["timeout"] == 10


def test_google_script_send_http_failure(monkeypatch):
    from app import app

    configure_google_script(monkeypatch)

    with app.test_request_context(), patch("email_service.requests.post") as mock_post:
        mock_post.return_value = MockResponse(status_code=500, text="Internal Server Error")
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}


def test_google_script_send_invalid_json(monkeypatch):
    from app import app

    configure_google_script(monkeypatch)

    with app.test_request_context(), patch("email_service.requests.post") as mock_post:
        mock_post.return_value = MockResponse(status_code=200, json_data=None, text="not json")
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}


def test_google_script_send_missing_success_true(monkeypatch):
    from app import app

    configure_google_script(monkeypatch)

    with app.test_request_context(), patch("email_service.requests.post") as mock_post:
        mock_post.return_value = MockResponse(status_code=200, json_data={"success": False, "error": "Invalid token"})
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}


def test_google_script_send_timeout_handled(monkeypatch):
    import requests
    from app import app

    configure_google_script(monkeypatch)

    with app.test_request_context(), patch("email_service.requests.post", side_effect=requests.exceptions.Timeout("Connection timed out")):
        results = send_booking_notifications(BOOKING)

    assert results == {"customer": False, "owner": False}


def test_google_script_missing_credentials(monkeypatch):
    from app import app

    configure_google_script(monkeypatch)
    monkeypatch.delenv("GOOGLE_EMAIL_WEBHOOK_URL")

    with app.test_request_context():
        results = send_booking_notifications(BOOKING)
    assert results == {"customer": False, "owner": False}


def test_google_script_is_mail_configured(monkeypatch):
    from email_service import is_mail_configured

    configure_google_script(monkeypatch)
    assert is_mail_configured() is True

    monkeypatch.delenv("GOOGLE_EMAIL_WEBHOOK_TOKEN")
    assert is_mail_configured() is False

    monkeypatch.setenv("GOOGLE_EMAIL_WEBHOOK_TOKEN", "token")
    monkeypatch.setenv("MAIL_ENABLED", "false")
    assert is_mail_configured() is False


def test_google_script_decision_email(monkeypatch):
    from app import app
    from email_service import send_booking_decision_email

    configure_google_script(monkeypatch)
    decision_booking = dict(BOOKING, status="Accepted")

    with app.test_request_context(), patch("email_service.requests.post") as mock_post:
        mock_post.return_value = MockResponse(status_code=200, json_data={"success": True})
        result = send_booking_decision_email(decision_booking)

    assert result is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["to"] == "customer@test.example"
    assert "Accepted" in payload["subject"]


def test_google_script_password_reset_email(monkeypatch):
    from app import app
    from email_service import send_password_reset_email

    configure_google_script(monkeypatch)

    with app.test_request_context(), patch("email_service.requests.post") as mock_post:
        mock_post.return_value = MockResponse(status_code=200, json_data={"success": True})
        result = send_password_reset_email("customer@test.example", "http://test.example/reset-password/abc123token")

    assert result is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["to"] == "customer@test.example"
    assert "Password Reset" in payload["subject"]
    assert "abc123token" in payload["html"]


def test_google_script_logging_never_logs_token_and_masks_email(monkeypatch):
    from app import app

    configure_google_script(monkeypatch)

    with app.test_request_context(), patch("email_service.requests.post") as mock_post, patch("email_service.logger.info") as mock_info:
        mock_post.return_value = MockResponse(status_code=200, json_data={"success": True})
        send_booking_notifications(BOOKING)

    for call in mock_info.call_args_list:
        log_str = str(call)
        assert "test-secret-token-123" not in log_str
        # Recipient should be masked like cu***r@test.example
        if "recipient=" in log_str:
            assert "cu***r@test.example" in log_str or "ow***r@test.example" in log_str

