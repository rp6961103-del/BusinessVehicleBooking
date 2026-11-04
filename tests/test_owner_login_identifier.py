import unittest
from unittest.mock import patch

import app as app_module
from mysql.connector import Error
from werkzeug.security import generate_password_hash


class FakeCursor:
    def __init__(self, rows=(), all_rows=()):
        self.rows = list(rows)
        self.all_rows = list(all_rows)
        self.executed = []
        self.lastrowid = 1
        self.last_query = ""

    def execute(self, sql, values=None):
        self.executed.append((sql, values))
        self.last_query = sql

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        if "subscription_requests" in self.last_query:
            return []
        return list(self.all_rows)


class MissingBookingCreatedAtCursor(FakeCursor):
    def execute(self, sql, values=None):
        super().execute(sql, values)
        if "b.created_at" in sql:
            raise Error(
                errno=1054,
                msg="Unknown column 'b.created_at' in 'field list'",
            )


class MissingUnrelatedColumnCursor(FakeCursor):
    def execute(self, sql, values=None):
        super().execute(sql, values)
        if "b.created_at" in sql:
            self.error = Error(
                errno=1054,
                msg="Unknown column 'v.missing_column' in 'field list'",
            )
            raise self.error


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
        self.assertEqual(fake_cursor.executed[0][1], ("owner@business.example",))
        self.assertIn("WHERE email = %s", fake_cursor.executed[0][0])
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
        self.assertEqual(fake_cursor.executed[0][1], ("7993347143",))
        self.assertIn("WHERE phone = %s", fake_cursor.executed[0][0])
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
        self.assertEqual(fake_cursor.executed[0][1], ("7993347143",))

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
        self.assertIn(b"email or phone number or password is incorrect", response.data)
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
        self.assertIn(b"email or phone number or password is incorrect", response.data)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("owner_id"))



    def test_owner_login_with_country_code_phone_normalized(self):
        """Test 7: Phone number with +91 country code prefix is normalized and authenticates."""
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
                    "identifier": "+917993347143",
                    "password": "owner-secure-password",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")
        self.assertEqual(fake_cursor.executed[0][1], ("7993347143",))
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("owner_id"), 10)

    def test_owner_login_email_case_insensitive(self):
        """Test 8: Mixed-case or uppercase email is normalized to lowercase."""
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
                    "identifier": "OWNER@BUSINESS.EXAMPLE",
                    "password": "owner-secure-password",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")
        self.assertEqual(fake_cursor.executed[0][1], ("owner@business.example",))
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("owner_id"), 10)

    def test_owner_login_empty_identifier_or_password(self):
        """Test 9: Blank identifier or password fails validation without querying DB."""
        fake_cursor = FakeCursor()
        token = self.csrf_token()

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "",
                    "password": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"email or phone number or password is incorrect", response.data)
        self.assertEqual(len(fake_cursor.executed), 0)

    def test_owner_login_database_error_returns_503(self):
        """Test 10: Database Error returns 503 instead of wrong password."""
        token = self.csrf_token()

        class FailingCursor:
            def execute(self, sql, values=None):
                from mysql.connector import Error as MySQLError
                raise MySQLError("OperationalError 2006: MySQL server has gone away")

        with patch.object(app_module, "cursor", FailingCursor()):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "owner@business.example",
                    "password": "owner-secure-password",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"temporarily unavailable", response.data.lower())
        self.assertNotIn(b"password is incorrect", response.data.lower())
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("owner_id"))

    def test_owner_login_cursor_none_returns_503(self):
        """Test 11: Uninitialized database cursor returns 503."""
        token = self.csrf_token()

        with patch.object(app_module, "cursor", None), patch.object(app_module, "ensure_db_connection", lambda: None):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "owner@business.example",
                    "password": "owner-secure-password",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"temporarily unavailable", response.data.lower())
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("owner_id"))

    def test_post_login_redirect_to_owner_dashboard(self):
        """Test 12: Successful owner login follows redirect to owner dashboard cleanly."""
        password_hash = generate_password_hash("owner-secure-password")
        token = self.csrf_token()

        # Mock cursor for owner login then owner dashboard queries:
        # 1. ownerlogin: fetchone -> owner record
        # 2. owner_dashboard bookings query: fetchall -> bookings
        # 3. total bookings count: fetchone -> (1,)
        # 4. pending bookings count: fetchone -> (1,)
        # 5. accepted bookings count: fetchone -> (0,)
        # 6. rejected bookings count: fetchone -> (0,)
        # 7. vehicle count: fetchone -> (2,)
        # 8. active vehicle count: fetchone -> (1,)
        from datetime import date
        fake_cursor = FakeCursor(
            rows=[
                (10, "Test Fleet Owner", "7993347143", "owner@business.example", password_hash, True),
                (1,),
                (1,),
                (0,),
                (0,),
                (2,),
                (1,),
            ],
            all_rows=[
                (1, "Customer Ram", "9866862006", date(2026, 11, 2), "Pending",
                 "Truck A", "Truck", "FromA", "ToB", 1500, None)
            ]
        )

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "identifier": "owner@business.example",
                    "password": "owner-secure-password",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Fleet Owner", response.data)
        self.assertIn(b"Truck A", response.data)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("owner_id"), 10)
            self.assertEqual(session.get("user_role"), "owner")
            self.assertTrue(session.get("logged_in"))

    def test_owner_dashboard_falls_back_when_booking_created_at_is_missing(self):
        from datetime import date

        fake_cursor = MissingBookingCreatedAtCursor(
            rows=[(1,), (1,), (0,), (0,), (2,), (1,)],
            all_rows=[
                (1, "Customer Ram", "9866862006", date(2026, 11, 2), "Pending",
                 "Truck A", "Truck", "FromA", "ToB", 1500, None)
            ],
        )
        with self.client.session_transaction() as session:
            session["owner_id"] = 10

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.get("/owner_dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Booking Date", response.data)
        self.assertIn(b"2026-11-02", response.data)
        self.assertIn(b"Booked on: Booking time unavailable", response.data)
        bookings_query, bookings_params = fake_cursor.executed[0]
        self.assertIn("b.booking_date", bookings_query)
        self.assertIn("b.created_at", bookings_query)
        self.assertIn("ORDER BY b.id DESC", bookings_query)
        self.assertEqual(bookings_params, (10,))
        fallback_query, fallback_params = fake_cursor.executed[1]
        self.assertIn("NULL as created_at", fallback_query)
        self.assertEqual(fallback_params, (10,))

    def test_owner_dashboard_does_not_fallback_for_unrelated_missing_column(self):
        fake_cursor = MissingUnrelatedColumnCursor()
        with self.client.session_transaction() as session:
            session["owner_id"] = 10

        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.get("/owner_dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Owner Dashboard", response.data)
        self.assertNotIn(b"Unknown column", response.data)
        self.assertIn("Unknown column 'v.missing_column'", str(fake_cursor.error))
        self.assertEqual(
            sum("b.created_at" in sql for sql, _values in fake_cursor.executed),
            1,
        )
        self.assertTrue(
            any("subscription_requests" in sql for sql, _values in fake_cursor.executed)
        )
        bookings_query, bookings_params = fake_cursor.executed[0]
        self.assertIn("b.created_at", bookings_query)
        self.assertEqual(bookings_params, (10,))
        self.assertNotIn(
            "NULL as created_at",
            "\n".join(query for query, _params in fake_cursor.executed),
        )


if __name__ == "__main__":
    unittest.main()
