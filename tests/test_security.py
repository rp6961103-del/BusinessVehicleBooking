import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import mysql.connector
import app as app_module
from werkzeug.security import check_password_hash, generate_password_hash


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


class SecurityTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def csrf_token(self):
        response = self.client.get("/register")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_development_session_secret_is_persistent(self):
        with tempfile.TemporaryDirectory() as instance_path:
            first_secret = app_module.load_development_secret_key(instance_path)
            second_secret = app_module.load_development_secret_key(instance_path)
        self.assertEqual(first_secret, second_secret)
        self.assertGreaterEqual(len(first_secret), 32)

    def test_authentication_forms_render_the_current_session_token(self):
        for path in ("/login", "/ownerlogin", "/ownerregister"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                rendered_tokens = re.findall(
                    rb'name="csrf_token"\s+value="([^"]+)"',
                    response.data,
                )
                self.assertTrue(rendered_tokens)
                with self.client.session_transaction() as session:
                    self.assertEqual(
                        set(rendered_tokens),
                        {session["csrf_token"].encode()},
                    )

    def test_https_session_cookie_and_csrf_token_are_host_scoped(self):
        previous_secure_setting = app_module.app.config["SESSION_COOKIE_SECURE"]
        app_module.app.config["SESSION_COOKIE_SECURE"] = True
        try:
            response = self.client.get("/login", base_url="https://booking.example")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Secure", response.headers["Set-Cookie"])
            token = re.search(
                rb'name="csrf_token"\s+value="([^"]+)"',
                response.data,
            ).group(1).decode()

            different_host = self.client.post(
                "/logout",
                data={"csrf_token": token},
                base_url="https://different.example",
            )
            self.assertEqual(different_host.status_code, 400)
            self.assertIn(b"Invalid CSRF token", different_host.data)

            same_host = self.client.post(
                "/logout",
                data={"csrf_token": token},
                base_url="https://booking.example",
            )
            self.assertEqual(same_host.status_code, 302)
        finally:
            app_module.app.config["SESSION_COOKIE_SECURE"] = previous_secure_setting

    def test_password_hashing(self):
        password_hash = generate_password_hash("correct-password")
        self.assertNotEqual(password_hash, "correct-password")
        self.assertTrue(check_password_hash(password_hash, "correct-password"))
        self.assertFalse(check_password_hash(password_hash, "wrong-password"))

    def test_customer_mobile_normalization_accepts_indian_country_code_and_formatting(self):
        for submitted in (
            "9876543210",
            " 9876543210 ",
            "+91 98765 43210",
            "+91-98765-43210",
            "0091 (98765) 43210",
            "919876543210",
        ):
            with self.subTest(submitted=submitted):
                self.assertEqual(app_module.valid_phone(submitted), "9876543210")

        for submitted in (
            "+1 9876543210",
            "91 987654321",
            "9876543210abc",
            "++91 9876543210",
        ):
            with self.subTest(submitted=submitted):
                self.assertIsNone(app_module.valid_phone(submitted))

    @patch.object(app_module, "db", Mock())
    def test_customer_registration_stores_hash(self):
        fake_cursor = FakeCursor([None])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/register",
                data={
                    "csrf_token": token,
                    "name": "Test Customer",
                    "phone": "9876543210",
                    "password": "customer-password"
                }
            )
        self.assertEqual(response.status_code, 200)
        insert_values = fake_cursor.executed[-1][1]
        self.assertNotEqual(insert_values[2], "customer-password")
        self.assertTrue(check_password_hash(insert_values[2], "customer-password"))

    @patch.object(app_module, "db", Mock())
    def test_customer_registration_stores_normalized_phone(self):
        fake_cursor = FakeCursor([None])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/register",
                data={
                    "csrf_token": token,
                    "name": "Test Customer",
                    "phone": "+91 (98765) 43210",
                    "password": "customer-password",
                },
            )
        self.assertEqual(response.status_code, 200)
        insert_values = fake_cursor.executed[-1][1]
        self.assertEqual(insert_values[1], "9876543210")

    def test_customer_login(self):
        password_hash = generate_password_hash("customer-password")
        fake_cursor = FakeCursor(all_rows=[
            (7, "Test Customer", "+91 98765-43210", password_hash, "customer@example.test")
        ])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "+91 98765-43210",
                    "password": "customer-password"
                }
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/vehicles")
        self.assertEqual(fake_cursor.executed[0][1], ("9876543210", "919876543210", "00919876543210"))
        self.assertIn("FROM users", fake_cursor.executed[0][0])
        self.assertIn("REGEXP_REPLACE(phone", fake_cursor.executed[0][0])
        self.assertIn("password_hash", fake_cursor.executed[0][0])
        with self.client.session_transaction() as session:
            self.assertTrue(session["csrf_token"])
            self.assertNotEqual(session["csrf_token"], token)

    def test_customer_login_rejects_missing_csrf_token(self):
        response = self.client.post(
            "/login",
            data={
                "phone": "9876543210",
                "password": "customer-password",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid CSRF token", response.data)

    def test_customer_login_rejects_invalid_csrf_token(self):
        self.csrf_token()
        response = self.client.post(
            "/login",
            data={
                "csrf_token": "invalid-token",
                "phone": "9876543210",
                "password": "customer-password",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid CSRF token", response.data)

    def test_invalid_customer_login(self):
        fake_cursor = FakeCursor(all_rows=[])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "9876543210",
                    "password": "wrong-password"
                }
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mobile number or password is incorrect.", response.data)

    def test_customer_login_rejects_wrong_password(self):
        password_hash = generate_password_hash("customer-password")
        fake_cursor = FakeCursor(all_rows=[
            (7, "Test Customer", "9876543210", password_hash, "customer@example.test")
        ])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "9876543210",
                    "password": "wrong-password",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mobile number or password is incorrect.", response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_customer_login_accepts_stored_number_without_country_code(self):
        password_hash = generate_password_hash("customer-password")
        fake_cursor = FakeCursor(all_rows=[
            (7, "Test Customer", "9876543210", password_hash, "customer@example.test")
        ])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "+91 (98765) 43210",
                    "password": "customer-password",
                },
            )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_phone"], "9876543210")

    def test_customer_login_rejects_ambiguous_normalized_phone(self):
        password_hash = generate_password_hash("customer-password")
        fake_cursor = FakeCursor(all_rows=[
            (7, "Test Customer", "9876543210", password_hash, "first@example.test"),
            (8, "Duplicate Customer", "+91 9876543210", password_hash, "second@example.test"),
        ])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor), \
                self.assertLogs("app", level="ERROR"):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "9876543210",
                    "password": "customer-password",
                },
            )
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_customer_login_reports_database_failure_separately(self):
        class FailingCursor:
            def execute(self, *_args, **_kwargs):
                raise mysql.connector.Error(
                    "connection failed; private-password-must-not-be-logged",
                    errno=2003,
                )

        token = self.csrf_token()
        with patch.object(app_module, "ensure_db_connection", return_value=None), \
                patch.object(app_module, "cursor", FailingCursor()), \
                self.assertLogs("app", level="ERROR") as captured:
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "9876543210",
                    "password": "customer-password",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"temporarily unavailable", response.data)
        self.assertNotIn(b"private-password-must-not-be-logged", response.data)
        self.assertNotIn("private-password-must-not-be-logged", "\n".join(captured.output))

    def test_customer_login_with_missing_cursor_reports_unavailable(self):
        token = self.csrf_token()
        with patch.object(app_module, "ensure_db_connection", return_value=None), \
                patch.object(app_module, "cursor", None), \
                self.assertLogs("app", level="ERROR") as captured:
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "9876543210",
                    "password": "customer-password",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"temporarily unavailable", response.data)
        self.assertNotIn("customer-password", "\n".join(captured.output))

    def test_owner_login_uses_password_hash(self):
        password_hash = generate_password_hash("owner-password")
        fake_cursor = FakeCursor([
            (9, "Test Owner", "9876543210", "owner@example.com", password_hash)
        ])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "email": "owner@example.com",
                    "password": "owner-password"
                }
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")
        with self.client.session_transaction() as session:
            self.assertTrue(session["csrf_token"])
            self.assertNotEqual(session["csrf_token"], token)

    @patch.object(app_module, "db", Mock())
    def test_owner_registration_requires_and_accepts_session_csrf_token(self):
        fake_cursor = FakeCursor([None])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerregister",
                data={
                    "csrf_token": token,
                    "owner_name": "Test Owner",
                    "phone": "9876543210",
                    "email": "owner@example.com",
                    "password": "owner-password",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("INSERT INTO owners" in sql for sql, _ in fake_cursor.executed))

    def test_owner_registration_rejects_missing_and_invalid_csrf_tokens(self):
        self.csrf_token()
        for submitted_token in (None, "invalid-token"):
            with self.subTest(submitted_token=submitted_token):
                data = {
                    "owner_name": "Test Owner",
                    "phone": "9876543210",
                    "email": "owner@example.com",
                    "password": "owner-password",
                }
                if submitted_token is not None:
                    data["csrf_token"] = submitted_token
                response = self.client.post("/ownerregister", data=data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(b"Invalid CSRF token", response.data)

    def test_invalid_owner_login(self):
        fake_cursor = FakeCursor([None])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/ownerlogin",
                data={
                    "csrf_token": token,
                    "email": "owner@example.com",
                    "password": "wrong-password"
                }
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid Owner Login", response.data)

    def test_csrf_rejects_missing_token(self):
        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 400)

    def test_customer_route_requires_login(self):
        self.assertEqual(self.client.get("/mybookings").location, "/login")
        self.assertEqual(self.client.get("/book/1").location, "/login")

    def test_owner_route_requires_login(self):
        self.assertEqual(self.client.get("/addvehicle").location, "/ownerlogin")
        self.assertEqual(self.client.get("/myvehicles").location, "/ownerlogin")

    def test_authorized_vehicle_edit_is_owner_scoped(self):
        fake_cursor = FakeCursor([(4, "Truck", "Pickup", "9876543210", "Town")])
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["owner_id"] = 3
        with patch.object(app_module, "cursor", fake_cursor):
            with patch.object(app_module, "db", Mock()):
                response = self.client.post(
                    "/editvehicle/4",
                    data={
                        "csrf_token": token,
                        "vehicle_name": "Updated Truck",
                        "vehicle_type": "Pickup",
                        "contact_number": "9876543210",
                        "location": "Town"
                    }
                )
        self.assertEqual(response.status_code, 302)
        self.assertIn("UPDATE vehicles", fake_cursor.executed[-1][0])

    def test_unauthorized_vehicle_edit_is_rejected(self):
        fake_cursor = FakeCursor([None])
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["owner_id"] = 3
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/editvehicle/4",
                data={
                    "csrf_token": token,
                    "vehicle_name": "Updated Truck",
                    "vehicle_type": "Pickup",
                    "contact_number": "9876543210",
                    "location": "Town"
                }
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/myvehicles")
        self.assertFalse(any("UPDATE vehicles" in sql for sql, _ in fake_cursor.executed))

    def test_unauthorized_booking_status_is_rejected(self):
        fake_cursor = FakeCursor([None])
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["owner_id"] = 3
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/update_booking/1/Accepted",
                data={"csrf_token": token}
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Access Denied", response.data)
        ownership_query, ownership_params = fake_cursor.executed[0]
        self.assertIn("INNER JOIN owners o", ownership_query)
        self.assertIn("AND o.id = %s", ownership_query)
        self.assertEqual(ownership_params, (1, 3))
        self.assertFalse(any("UPDATE bookings" in sql for sql, _ in fake_cursor.executed))

    @patch.object(app_module, "db", Mock())
    def test_owner_can_update_own_booking_status(self):
        for status in ("Accepted", "Rejected"):
            with self.subTest(status=status):
                fake_cursor = FakeCursor([(
                    1,
                    "Test Customer",
                    "9000000002",
                    "customer@test.example",
                    "2099-01-01",
                    "Test Truck",
                    "Pickup",
                    "Test Owner",
                    "owner@test.example",
                    "9000000001",
                    "Test Town",
                    "Test City",
                    "2500.00",
                )])
                token = self.csrf_token()
                with self.client.session_transaction() as session:
                    session["owner_id"] = 9
                with (
                    patch.object(app_module, "cursor", fake_cursor),
                    patch.object(app_module, "send_booking_decision_to_customer") as send_email,
                ):
                    response = self.client.post(
                        f"/update_booking/1/{status}",
                        data={"csrf_token": token},
                    )

                self.assertEqual(response.status_code, 302)
                send_email.assert_called_once()
                self.assertEqual(send_email.call_args.args[0]["customer_email"], "customer@test.example")
                self.assertEqual(send_email.call_args.args[0]["status"], status)
                ownership_query, ownership_params = fake_cursor.executed[0]
                self.assertIn("INNER JOIN owners o", ownership_query)
                self.assertIn("AND o.id = %s", ownership_query)
                self.assertEqual(ownership_params, (1, 9))
                update_query, update_params = fake_cursor.executed[1]
                self.assertIn("UPDATE bookings", update_query)
                self.assertEqual(update_params, (status, 1))

    def test_favicon_redirects_to_svg_asset(self):
        response = self.client.get("/favicon.ico")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/static/favicon.svg")
        favicon = self.client.get(response.location)
        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(favicon.mimetype, "image/svg+xml")

    def test_health_check_requires_database_connection(self):
        with (
            patch.object(app_module, "ensure_db_connection", return_value=None),
            patch.object(app_module, "db", None),
        ):
            response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {"status": "unavailable"})

    def test_invalid_booking_date_is_rejected_server_side(self):
        fake_cursor = FakeCursor([
            (1, "Truck", "Pickup", 8, "9876543210", "Town", "A", "B", 2500)
        ])
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["user_name"] = "Customer"
            session["user_phone"] = "9876543210"
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/book/1",
                data={"csrf_token": token, "booking_date": "not-a-date"}
            )
        self.assertEqual(response.status_code, 400)

    @patch.object(app_module, "db", Mock())
    def test_booking_creation_accepts_current_session_csrf_token(self):
        fake_cursor = FakeCursor(
            [
                (
                    1, "Truck", "Pickup", 8, "9876543210", "Town", "Town", "City",
                    "2500.00", 4, "Owner", "owner@example.com", "9876543210",
                ),
                ("customer@example.com",),
                None,
            ]
        )
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["user_name"] = "Customer"
            session["user_phone"] = "9876543210"
        with patch.object(app_module, "cursor", fake_cursor):
            with patch.object(
                app_module,
                "send_booking_notifications",
                return_value={"customer": True, "owner": True},
            ):
                with patch.object(
                    app_module,
                    "email_configuration_status",
                    return_value={"enabled": True, "configured": True, "message": ""},
                ):
                    response = self.client.post(
                        "/book/1",
                        data={"csrf_token": token, "booking_date": "2099-01-01"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Booking Successful", response.data)
        insert_query, insert_values = next(
            (sql, values)
            for sql, values in fake_cursor.executed
            if "INSERT INTO bookings" in sql
        )
        self.assertIn("INSERT INTO bookings", insert_query)
        self.assertEqual(
            insert_values,
            (1, 4, "Customer", "9876543210", "2099-01-01", "Pending"),
        )

    def test_booking_status_requires_post(self):
        with self.client.session_transaction() as session:
            session["owner_id"] = 3
        self.assertEqual(self.client.get("/update_booking/1/Accepted").status_code, 405)

    def test_language_change_requires_post(self):
        self.assertEqual(self.client.get("/set_language/te").status_code, 405)

    def test_vehicle_details_page(self):
        fake_cursor = FakeCursor(
            all_rows=[
                (4, "Truck", "Pickup", 9, "9876543210", "Town", "A", "B", 2500, 12, "Owner", 1)
            ]
        )
        with self.client.session_transaction() as session:
            session["user_id"] = 7
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.get("/vehicle/4")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Truck", response.data)
        self.assertIn(b"Book This Vehicle", response.data)

    def test_invalid_vehicle_id_returns_not_found(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 7
        with patch.object(app_module, "cursor", FakeCursor([])):
            response = self.client.get("/vehicle/999")
        self.assertEqual(response.status_code, 404)

    def test_vehicle_search_filter_is_server_side(self):
        fake_cursor = FakeCursor([])
        with self.client.session_transaction() as session:
            session["user_id"] = 7
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.get("/vehicles?q=pickup&location=Town")
        self.assertEqual(response.status_code, 200)
        sql, values = fake_cursor.executed[-1]
        self.assertIn("LIKE", sql)
        self.assertIn("%pickup%", values)
        self.assertIn("%Town%", values)

    def test_invalid_vehicle_filter_is_rejected(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 7
        response = self.client.get("/vehicles?min_rent=not-a-number")
        self.assertEqual(response.status_code, 400)

    def test_booking_success(self):
        fake_cursor = FakeCursor([
            (
                1, "Truck", "Pickup", 8, "9876543210", "Town", "A", "B",
                2500, 4, "Owner", "owner@example.com", "9876543210",
            ),
            ("customer@example.com",),
            None,
        ])
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["user_name"] = "Customer"
            session["user_phone"] = "9876543210"
        with patch.object(app_module, "cursor", fake_cursor):
            with patch.object(app_module, "db", Mock()):
                response = self.client.post(
                    "/book/1",
                    data={"csrf_token": token, "booking_date": "2099-01-01"}
                )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Booking Successful", response.data)
        self.assertTrue(any("INSERT INTO bookings" in sql for sql, _ in fake_cursor.executed))

    def test_duplicate_booking_is_rejected(self):
        fake_cursor = FakeCursor([
            (
                1, "Truck", "Pickup", 8, "9876543210", "Town", "A", "B",
                2500, 4, "Owner", "owner@example.com", "9876543210",
            ),
            ("customer@example.com",),
            (42,),
        ])
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["user_name"] = "Customer"
            session["user_phone"] = "9876543210"
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/book/1",
                data={"csrf_token": token, "booking_date": "2099-01-01"}
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"already booked", response.data)

    def test_vehicle_details_requires_customer_login(self):
        response = self.client.get("/vehicle/1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/login")

    def test_owner_dashboard_includes_vehicle_metrics(self):
        fake_cursor = FakeCursor([
            (1,), (1,), (0,), (0,), (3,), (2,)
        ])
        with self.client.session_transaction() as session:
            session["owner_id"] = 9
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.get("/owner_dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Active Vehicles", response.data)
        bookings_query, bookings_params = fake_cursor.executed[0]
        self.assertIn("INNER JOIN vehicles v", bookings_query)
        self.assertIn("ON b.vehicle_id = v.id", bookings_query)
        self.assertIn("INNER JOIN owners o", bookings_query)
        self.assertIn("WHERE o.id = %s", bookings_query)
        self.assertIn("vr.vehicle_id = b.vehicle_id", bookings_query)
        self.assertEqual(bookings_params, (9,))


if __name__ == "__main__":
    unittest.main()
