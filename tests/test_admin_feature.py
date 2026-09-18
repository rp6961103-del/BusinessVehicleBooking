import os
import unittest
from unittest.mock import patch, MagicMock

import app as app_module


class AdminFeatureTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app_module.app.test_client()
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "MyAdmin@2026"

    def csrf_token(self, path="/admin/login"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session.get("csrf_token", "")

    def test_home_page_has_admin_dashboard_button(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Admin Dashboard", response.data)
        self.assertIn(b'/admin/login', response.data)

    def test_admin_login_page_loads_with_required_elements(self):
        response = self.client.get("/admin/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ADMIN LOGIN", response.data)
        self.assertIn(b"Username", response.data)
        self.assertIn(b"Password", response.data)
        self.assertIn(b"Login", response.data)

    def test_admin_dashboard_redirects_when_not_authenticated(self):
        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/login"))

    def test_customer_cannot_access_admin_dashboard(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 10
            session["user_name"] = "Regular Customer"
        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/login"))

    def test_owner_cannot_access_admin_dashboard(self):
        with self.client.session_transaction() as session:
            session["owner_id"] = 20
            session["owner_name"] = "Vehicle Owner"
        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/login"))

    def test_admin_login_rejects_invalid_credentials(self):
        token = self.csrf_token()
        response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "wrongpassword",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid admin username or password", response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("admin_logged_in", session)

    def test_admin_login_success(self):
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["user_id"] = 99  # should be cleared on admin login

        response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": token,
                "username": "admin",
                "password": "MyAdmin@2026",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/dashboard"))

        with self.client.session_transaction() as session:
            self.assertTrue(session.get("admin_logged_in"))
            self.assertEqual(session.get("admin_username"), "admin")
            self.assertNotIn("user_id", session)

    def test_admin_login_redirects_if_already_authenticated(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
        response = self.client.get("/admin/login")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/dashboard"))

    def test_admin_dashboard_loads_with_all_sections_and_tables(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            session["admin_username"] = "admin"

        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Admin Dashboard", response.data)

        # 9 Metrics
        self.assertIn(b"Total Customers", response.data)
        self.assertIn(b"Total Vehicle Owners", response.data)
        self.assertIn(b"Total Vehicles", response.data)
        self.assertIn(b"Total Bookings", response.data)
        self.assertIn(b"Pending Bookings", response.data)
        self.assertIn(b"Accepted Bookings", response.data)
        self.assertIn(b"Rejected Bookings", response.data)
        self.assertIn(b"Total Ratings", response.data)
        self.assertIn(b"Total Payments", response.data)

        # All Table Sections
        self.assertIn(b"Recent Bookings", response.data)
        self.assertIn(b"Registered Customers", response.data)
        self.assertIn(b"Registered Vehicle Owners", response.data)
        self.assertIn(b"Fleet Vehicles", response.data)
        self.assertIn(b"Processed Payments", response.data)
        self.assertIn(b"Ratings &amp; Reviews", response.data)

        # Header Navigation & Logout
        self.assertIn(b"Admin Logout", response.data)

    def test_admin_logout(self):
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True
            session["admin_username"] = "admin"

        token = self.csrf_token("/admin/dashboard")
        response = self.client.post("/admin/logout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/login"))

        with self.client.session_transaction() as session:
            self.assertNotIn("admin_logged_in", session)
            self.assertNotIn("admin_username", session)


if __name__ == "__main__":
    unittest.main()

