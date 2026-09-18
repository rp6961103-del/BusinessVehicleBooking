import unittest
from unittest.mock import Mock, patch

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
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app_module.app.test_client()

    def csrf_token(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_password_hashing(self):
        password_hash = generate_password_hash("correct-password")
        self.assertNotEqual(password_hash, "correct-password")
        self.assertTrue(check_password_hash(password_hash, "correct-password"))
        self.assertFalse(check_password_hash(password_hash, "wrong-password"))

    def test_customer_login(self):
        password_hash = generate_password_hash("customer-password")
        fake_cursor = FakeCursor([(7, "Test Customer", "9876543210", password_hash)])
        token = self.csrf_token()
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "phone": "9876543210",
                    "password": "customer-password"
                }
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/vehicles")

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
        fake_cursor = FakeCursor([None])
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
        self.assertIn(b"Invalid Customer Login", response.data)
        self.assertIn(b"mobile number or password is incorrect", response.data)

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
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/owner_dashboard"))
        update_statements = [sql for sql, _ in fake_cursor.executed if "UPDATE bookings" in sql]
        self.assertEqual(len(update_statements), 1)
        self.assertIn("v.owner_id", update_statements[0])
        self.assertIn("'pending'", update_statements[0])

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

    def test_booking_status_requires_post(self):
        with self.client.session_transaction() as session:
            session["owner_id"] = 3
        self.assertEqual(self.client.get("/update_booking/1/Accepted").status_code, 405)

    def test_language_change_requires_post(self):
        self.assertEqual(self.client.get("/set_language/te").status_code, 405)

    def test_language_change_supports_tamil(self):
        token = self.csrf_token()
        response = self.client.post(
            "/set_language/ta",
            data={"csrf_token": token},
            headers={"Referer": "/login"},
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["language"], "ta")

        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("தமிழ்".encode("utf-8"), response.data)

    def test_vehicle_details_page(self):
        fake_cursor = FakeCursor(
            all_rows=[(4, "Truck", "Pickup", 9, "9876543210", "Town", "A", "B", 2500)]
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
            (1, "Truck", "Pickup", 8, "9876543210", "Town", "A", "B", 2500),
            None,
            ("Truck", "Pickup", "A", "B", 2500, "Customer", "9876543210", "2099-01-01", "Pending"),
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
        self.assertIn(b"Booking Request Submitted", response.data)
        self.assertTrue(any("INSERT INTO bookings" in sql for sql, _ in fake_cursor.executed))

    def test_duplicate_booking_is_rejected(self):
        fake_cursor = FakeCursor([
            (1, "Truck", "Pickup", 8, "9876543210", "Town", "A", "B", 2500),
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


if __name__ == "__main__":
    unittest.main()
