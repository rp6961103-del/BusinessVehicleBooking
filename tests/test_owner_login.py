import pytest
from werkzeug.security import generate_password_hash

import app as app_module


OWNER_PASSWORD = "owner-password"
OWNER = (
    9,
    "Test Owner",
    "9876543210",
    "owner@example.test",
    generate_password_hash(OWNER_PASSWORD),
)


class OwnerCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []

    def execute(self, query, values):
        self.executed.append((query, values))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


@pytest.fixture
def client(monkeypatch):
    app_module.app.config.update(TESTING=True)
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    with app_module.app.test_client() as test_client:
        yield test_client


def get_csrf_token(client):
    response = client.get("/ownerlogin")
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session["csrf_token"]


@pytest.mark.parametrize(
    ("identifier", "expected_column", "expected_value"),
    [
        ("owner@example.test", "email", "owner@example.test"),
        ("  owner@example.test  ", "email", "owner@example.test"),
        ("9876543210", "phone", "9876543210"),
        ("  +91 (98765) 43210  ", "phone", "9876543210"),
    ],
)
def test_owner_login_accepts_email_or_phone(
    client, monkeypatch, identifier, expected_column, expected_value
):
    cursor = OwnerCursor([OWNER])
    monkeypatch.setattr(app_module, "cursor", cursor)
    token = get_csrf_token(client)

    response = client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "identifier": identifier,
            "password": OWNER_PASSWORD,
        },
    )

    assert response.status_code == 302
    assert response.location == "/owner_dashboard"
    query, params = cursor.executed[0]
    assert f"WHERE {expected_column} = %s" in query
    assert "AND is_active = TRUE" in query
    assert params == (expected_value,)
    with client.session_transaction() as session:
        assert session["owner_id"] == OWNER[0]
        assert session["owner_name"] == OWNER[1]
        assert session["owner_phone"] == OWNER[2]
        assert session["owner_email"] == OWNER[3]
        assert session["csrf_token"] != token


def test_owner_login_does_not_trim_password_and_preserves_language(client, monkeypatch):
    password = "  padded owner password  "
    owner = (*OWNER[:4], generate_password_hash(password))
    cursor = OwnerCursor([owner])
    monkeypatch.setattr(app_module, "cursor", cursor)
    with client.session_transaction() as session:
        session["language"] = "te"
    token = get_csrf_token(client)

    response = client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "identifier": "owner@example.test",
            "password": password,
        },
    )

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["language"] == "te"
        assert session["owner_id"] == OWNER[0]


def test_owner_login_accepts_legacy_email_field(client, monkeypatch):
    cursor = OwnerCursor([OWNER])
    monkeypatch.setattr(app_module, "cursor", cursor)
    token = get_csrf_token(client)

    response = client.post(
        "/ownerlogin",
        data={"csrf_token": token, "email": OWNER[3], "password": OWNER_PASSWORD},
    )

    assert response.status_code == 302
    assert cursor.executed[0][1] == (OWNER[3],)


@pytest.mark.parametrize(
    ("identifier", "password", "rows"),
    [
        ("owner@example.test", "wrong-password", [OWNER]),
        ("unknown@example.test", OWNER_PASSWORD, []),
        ("123", OWNER_PASSWORD, []),
        ("", OWNER_PASSWORD, []),
        ("owner@example.test", "short", []),
        ("owner@example.test", OWNER_PASSWORD, []),  # Inactive owners are excluded by the query.
    ],
)
def test_owner_login_failures_are_generic_and_do_not_create_sessions(
    client, monkeypatch, identifier, password, rows
):
    cursor = OwnerCursor(rows)
    monkeypatch.setattr(app_module, "cursor", cursor)
    token = get_csrf_token(client)

    response = client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "identifier": identifier,
            "password": password,
        },
    )

    assert response.status_code == 200
    assert b"email or phone number or password is incorrect" in response.data
    with client.session_transaction() as session:
        assert "owner_id" not in session
    if identifier and password and len(password) >= 8 and (
        "@" in identifier or app_module.valid_phone(identifier)
    ):
        query, _params = cursor.executed[0]
        assert "AND is_active = TRUE" in query


def test_owner_login_requires_csrf(client, monkeypatch):
    cursor = OwnerCursor([OWNER])
    monkeypatch.setattr(app_module, "cursor", cursor)

    response = client.post(
        "/ownerlogin",
        data={"identifier": OWNER[3], "password": OWNER_PASSWORD},
    )

    assert response.status_code == 400
    assert b"Invalid CSRF token" in response.data
    assert cursor.executed == []


def test_owner_login_form_has_identifier_and_shared_password_toggle(client):
    response = client.get("/ownerlogin")

    assert response.status_code == 200
    assert b"Email or Phone Number" in response.data
    assert b'name="identifier"' in response.data
    assert b'name="email"' not in response.data
    assert b'name="csrf_token"' in response.data
    assert b"script.js" in response.data
    assert b'<html lang="en">' in response.data


def test_owner_login_form_uses_selected_language(client):
    with client.session_transaction() as session:
        session["language"] = "te"

    response = client.get("/ownerlogin")

    assert response.status_code == 200
    assert b'<html lang="te">' in response.data
    assert "ఇమెయిల్ లేదా ఫోన్ నంబర్".encode() in response.data


def test_owner_login_invalid_input_does_not_query_database(client, monkeypatch):
    cursor = OwnerCursor([OWNER])
    monkeypatch.setattr(app_module, "cursor", cursor)
    token = get_csrf_token(client)

    response = client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "identifier": "not-an-email-or-phone",
            "password": OWNER_PASSWORD,
        },
    )

    assert response.status_code == 200
    assert cursor.executed == []
