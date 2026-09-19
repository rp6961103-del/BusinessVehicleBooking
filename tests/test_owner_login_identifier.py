import unittest
from unittest.mock import patch

import app as app_module
from werkzeug.security import generate_password_hash


class FakeCursor:
    def __init__(self, rows=(), all_rows=()):
        self.rows = list(rows)
        self.all_rows = list(all_rows)
        self.executed = []
        self.lastrowid = 1

    def execute(self, sql, values=None):
        self.executed.append((sql, values))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return list(self.all_rows)


class OwnerLoginIdentifierTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app_module.app.test_client()

    def csrf_token(self):
        response = self.client.get("/ownerlogin")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_owner_login_with_registered_email(self):
        """Test 1: Owner can log in using registered email + correct password."""
        password_hash = generate_password_hash("owner-secure-password")
        fake_cursor = FakeCursor([
            (10, "Test Fleet Owner", "7993347143", "owner@business.example", password_hash, True)
        ])
        token = self.csrf_token()

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "owner@business.example",
                    "password": "owner-secure-password",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")
        self.assertEqual(len(fake_cursor.executed), 1)
        self.assertEqual(fake_cursor.executed[0][1], ("owner@business.example", "owner@business.example"))
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("owner_id"), 10)
            self.assertEqual(session.get("user_role"), "owner")
            self.assertTrue(session.get("logged_in"))

    def test_owner_login_with_registered_phone(self):
        """Test 2: Owner can log in using registered phone + correct password."""
        password_hash = generate_password_hash("owner-secure-password")
        fake_cursor = FakeCursor([
            (10, "Test Fleet Owner", "7993347143", "owner@business.example", password_hash, True)
        ])
        token = self.csrf_token()

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "7993347143",
                    "password": "owner-secure-password",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")
        self.assertEqual(fake_cursor.executed[0][1], ("7993347143", "7993347143"))
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("owner_id"), 10)

    def test_owner_login_with_phone_surrounding_whitespace_normalized(self):
        """Test 3: Phone number with accidental surrounding whitespace still works and is normalized."""
        password_hash = generate_password_hash("owner-secure-password")
        fake_cursor = FakeCursor([
            (10, "Test Fleet Owner", "7993347143", "owner@business.example", password_hash, True)
        ])
        token = self.csrf_token()

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": " 7993347143 ",
                    "password": "owner-secure-password",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")
        # Ensure the query received the stripped/normalized string
        self.assertEqual(fake_cursor.executed[0][1], ("7993347143", "7993347143"))

    def test_owner_login_wrong_password_fails(self):
        """Test 4: Wrong password fails."""
        password_hash = generate_password_hash("owner-secure-password")
        fake_cursor = FakeCursor([
            (10, "Test Fleet Owner", "7993347143", "owner@business.example", password_hash, True)
        ])
        token = self.csrf_token()

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "7993347143",
                    "password": "incorrect-password",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid Owner Login", response.data)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("owner_id"))

    def test_password_passed_unchanged_without_trimming(self):
        """Test 5: Password is passed unchanged to password verification and not trimmed."""
        # Password with deliberate leading/trailing spaces
        raw_password_with_spaces = "  secret password with spaces  "
        password_hash = generate_password_hash(raw_password_with_spaces)
        fake_cursor = FakeCursor([
            (10, "Test Fleet Owner", "7993347143", "owner@business.example", password_hash, True)
        ])
        token = self.csrf_token()

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "7993347143",
                    "password": raw_password_with_spaces,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")

    def test_owner_login_unknown_email_or_phone_fails(self):
        """Test 6: Unknown email/phone fails."""
        fake_cursor = FakeCursor([None])
        token = self.csrf_token()

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "unknown@business.example",
                    "password": "some-password",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid Owner Login", response.data)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("owner_id"))


if __name__ == "__main__":
    unittest.main()
