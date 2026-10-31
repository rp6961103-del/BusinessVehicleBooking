import hashlib
import re
from unittest.mock import patch
from urllib.parse import urlsplit

import app as app_module
from werkzeug.security import check_password_hash

from .conftest import csrf_token


def test_reset_request_and_reset_form_pages_load_for_both_account_types():
    client = app_module.app.test_client()

    customer_login = client.get("/login")
    owner_login = client.get("/ownerlogin")
    assert b"/forgot-password/customer" in customer_login.data
    assert b"/forgot-password/owner" in owner_login.data

    for account_type in ("customer", "owner"):
        forgot_page = client.get(f"/forgot-password/{account_type}")
        reset_page = client.get(f"/reset-password/{account_type}")
        assert forgot_page.status_code == 200
        assert b'name="csrf_token"' in forgot_page.data
        assert reset_page.status_code == 200
        assert b'name="confirm_password"' in reset_page.data
        assert b"password_reset.js" in reset_page.data


def test_production_reset_link_uses_https_public_origin(monkeypatch):
    monkeypatch.setattr(app_module, "APP_ENV", "production")
    monkeypatch.delenv("PASSWORD_RESET_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "http://booking.onrender.com")
    with app_module.app.test_request_context("/"):
        assert app_module.password_reset_url("customer", "A" * 43) is None

    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://booking.onrender.com/")
    with app_module.app.test_request_context("/"):
        link = app_module.password_reset_url("owner", "B" * 43)
    assert link == "https://booking.onrender.com/reset-password/owner#" + "B" * 43


def enable_test_mail(monkeypatch):
    monkeypatch.setenv("MAIL_ENABLED", "true")
    monkeypatch.setenv("MAIL_SERVER", "smtp.test.example")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USE_TLS", "true")
    monkeypatch.setenv("MAIL_USE_SSL", "false")
    monkeypatch.setenv("MAIL_DEFAULT_SENDER", "BusinessVehicleBooking <no-reply@test.example>")


def request_reset(client, account_type, email):
    path = f"/forgot-password/{account_type}"
    token = csrf_token(client, path)
    return client.post(path, data={"csrf_token": token, "email": email})


def submit_reset(client, account_type, token, password, confirmation=None):
    path = f"/reset-password/{account_type}"
    csrf = csrf_token(client, path)
    return client.post(
        path,
        data={
            "csrf_token": csrf,
            "token": token,
            "password": password,
            "confirm_password": password if confirmation is None else confirmation,
        },
    )


def find_account_reset_token(test_db, account_type, account_id):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT token_hash, expires_at, used_at FROM password_reset_tokens "
            "WHERE account_type = %s AND account_id = %s ORDER BY id DESC LIMIT 1",
            (account_type, account_id),
        )
        return cursor.fetchone()
    finally:
        cursor.close()


def test_forgot_password_is_generic_and_rate_limited(app_client, test_db, seeded_data, monkeypatch):
    enable_test_mail(monkeypatch)
    with patch.object(app_module, "send_password_reset_email", return_value=True) as send:
        response = request_reset(app_client, "customer", "missing@test.example")

    assert response.status_code == 302
    follow_up = app_client.get(response.headers["Location"])
    assert follow_up.status_code == 200
    assert b"If an account matches that email address" in follow_up.data
    send.assert_not_called()

    with patch.object(app_module, "send_password_reset_email", return_value=True) as send:
        for _ in range(4):
            response = request_reset(app_client, "customer", "customer@test.example")
            assert response.status_code == 302
    assert send.call_count == 3


def test_reset_mail_is_account_scoped_and_tokens_are_hashed(
    app_client, test_db, seeded_data, monkeypatch
):
    enable_test_mail(monkeypatch)
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "UPDATE users SET email = %s WHERE id = %s",
            ("owner@test.example", seeded_data["customer_id"]),
        )
        test_db.commit()
    finally:
        cursor.close()

    sent_links = {}

    def capture_reset_email(recipient, reset_url, account_type):
        sent_links[account_type] = (recipient, reset_url)
        return True

    with patch.object(app_module, "send_password_reset_email", side_effect=capture_reset_email):
        assert request_reset(app_client, "customer", "owner@test.example").status_code == 302
        assert request_reset(app_client, "owner", "owner@test.example").status_code == 302

    customer_token = urlsplit(sent_links["customer"][1]).fragment
    owner_token = urlsplit(sent_links["owner"][1]).fragment
    assert sent_links["customer"][0] == "owner@test.example"
    assert sent_links["owner"][0] == "owner@test.example"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", customer_token)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", owner_token)
    assert "/reset-password/customer#" in sent_links["customer"][1]
    assert "/reset-password/owner#" in sent_links["owner"][1]

    customer_stored = find_account_reset_token(test_db, "customer", seeded_data["customer_id"])
    owner_stored = find_account_reset_token(test_db, "owner", seeded_data["owner_id"])
    assert customer_stored[0] == hashlib.sha256(customer_token.encode("ascii")).hexdigest()
    assert owner_stored[0] == hashlib.sha256(owner_token.encode("ascii")).hexdigest()
    assert customer_stored[0] != customer_token
    assert customer_stored[2] is None

    assert submit_reset(app_client, "customer", customer_token, "CustomerNew456").status_code == 302
    assert submit_reset(app_client, "owner", owner_token, "OwnerNew789").status_code == 302
    assert submit_reset(app_client, "customer", customer_token, "CustomerNew456").status_code == 400

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT password_hash FROM users WHERE id = %s", (seeded_data["customer_id"],))
        customer_hash = cursor.fetchone()[0]
        cursor.execute("SELECT password_hash FROM owners WHERE id = %s", (seeded_data["owner_id"],))
        owner_hash = cursor.fetchone()[0]
        assert check_password_hash(customer_hash, "CustomerNew456")
        assert not check_password_hash(customer_hash, "customer-password")
        assert check_password_hash(owner_hash, "OwnerNew789")
        assert not check_password_hash(owner_hash, "owner-password")
    finally:
        cursor.close()

    customer_token_for_login = csrf_token(app_client, "/login")
    customer_login = app_client.post(
        "/login",
        data={
            "csrf_token": customer_token_for_login,
            "phone": "9000000002",
            "password": "CustomerNew456",
        },
    )
    assert customer_login.status_code == 302
    with app_client.session_transaction() as session:
        session.clear()
    owner_token_for_login = csrf_token(app_client, "/ownerlogin")
    owner_login = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": owner_token_for_login,
            "email": "owner@test.example",
            "password": "OwnerNew789",
        },
    )
    assert owner_login.status_code == 302

    with app_client.session_transaction() as session:
        session.clear()
    customer_token_for_old_password = csrf_token(app_client, "/login")
    old_customer_login = app_client.post(
        "/login",
        data={
            "csrf_token": customer_token_for_old_password,
            "phone": "9000000002",
            "password": "customer-password",
        },
    )
    assert old_customer_login.status_code == 200
    assert b"incorrect" in old_customer_login.data.lower()

    with app_client.session_transaction() as session:
        session.clear()
    owner_token_for_old_password = csrf_token(app_client, "/ownerlogin")
    old_owner_login = app_client.post(
        "/ownerlogin",
        data={
            "csrf_token": owner_token_for_old_password,
            "email": "owner@test.example",
            "password": "owner-password",
        },
    )
    assert old_owner_login.status_code == 200
    assert b"incorrect" in old_owner_login.data.lower()


def test_expired_invalid_mismatched_and_reused_tokens_are_rejected(
    app_client, test_db, seeded_data
):
    expired_token = "A" * 43
    expired_hash = hashlib.sha256(expired_token.encode("ascii")).hexdigest()
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO password_reset_tokens "
            "(account_type, account_id, token_hash, created_at, expires_at) "
            "VALUES ('customer', %s, %s, UTC_TIMESTAMP() - INTERVAL 1 HOUR, "
            "UTC_TIMESTAMP() - INTERVAL 30 MINUTE)",
            (seeded_data["customer_id"], expired_hash),
        )
        test_db.commit()
    finally:
        cursor.close()

    assert submit_reset(app_client, "customer", expired_token, "CustomerNew456").status_code == 400
    assert submit_reset(app_client, "owner", "B" * 43, "OwnerNew789").status_code == 400

    valid_token = "C" * 43
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO password_reset_tokens "
            "(account_type, account_id, token_hash, created_at, expires_at) "
            "VALUES ('customer', %s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP() + INTERVAL 30 MINUTE)",
            (seeded_data["customer_id"], hashlib.sha256(valid_token.encode("ascii")).hexdigest()),
        )
        test_db.commit()
    finally:
        cursor.close()

    mismatch = submit_reset(app_client, "customer", valid_token, "CustomerNew456", "Different456")
    assert mismatch.status_code == 400
    assert find_account_reset_token(test_db, "customer", seeded_data["customer_id"])[2] is None
    assert submit_reset(app_client, "customer", valid_token, "CustomerNew456").status_code == 302
    assert submit_reset(app_client, "customer", valid_token, "AnotherNew789").status_code == 400


def test_password_reset_requires_strong_matching_passwords(app_client, test_db, seeded_data):
    token = "D" * 43
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO password_reset_tokens "
            "(account_type, account_id, token_hash, created_at, expires_at) "
            "VALUES ('customer', %s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP() + INTERVAL 30 MINUTE)",
            (seeded_data["customer_id"], hashlib.sha256(token.encode("ascii")).hexdigest()),
        )
        test_db.commit()
    finally:
        cursor.close()

    assert submit_reset(app_client, "customer", token, "allletters").status_code == 400
    assert submit_reset(app_client, "customer", token, "Short1", "Short2").status_code == 400
    assert find_account_reset_token(test_db, "customer", seeded_data["customer_id"])[2] is None


def test_missing_mail_configuration_keeps_generic_response_and_sends_nothing(
    app_client, seeded_data, monkeypatch
):
    monkeypatch.setenv("MAIL_ENABLED", "false")
    for setting in ("MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER"):
        monkeypatch.delenv(setting, raising=False)

    with patch.object(app_module, "send_password_reset_email") as send:
        response = request_reset(app_client, "customer", "customer@test.example")

    assert response.status_code == 302
    page = app_client.get(response.headers["Location"])
    assert page.status_code == 200
    assert b"If an account matches that email address" in page.data
    send.assert_not_called()
