import unittest
from unittest.mock import patch
import app as app_module

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

class AuthAndLanguageTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app_module.app.test_client()

    def csrf_token(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_language_selection_persists_in_session_and_cookie(self):
        token = self.csrf_token()
        response = self.client.post("/set_language/te", data={"csrf_token": token}, headers={"Referer": "/login"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("language"), "te")
        self.assertIn("language=te", response.headers.get("Set-Cookie", ""))

    def test_language_persists_across_logout(self):
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["user_name"] = "Test Customer"
            session["user_role"] = "customer"
            session["language"] = "hi"
            session["csrf_token"] = token

        response = self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("user_id"))
            self.assertEqual(session.get("language"), "hi")

    def test_owner_logout_preserves_language(self):
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["owner_id"] = 10
            session["owner_name"] = "Test Owner"
            session["user_role"] = "owner"
            session["language"] = "ta"
            session["csrf_token"] = token

        response = self.client.post("/owner_logout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIsNone(session.get("owner_id"))
            self.assertEqual(session.get("language"), "ta")

    def test_logged_in_customer_redirected_away_from_login_and_register(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["user_role"] = "customer"

        res_login = self.client.get("/login")
        self.assertEqual(res_login.status_code, 302)
        self.assertEqual(res_login.location, "/vehicles")

        res_register = self.client.get("/register")
        self.assertEqual(res_register.status_code, 302)
        self.assertEqual(res_register.location, "/vehicles")

    def test_logged_in_owner_redirected_away_from_ownerlogin_and_ownerregister(self):
        with self.client.session_transaction() as session:
            session["owner_id"] = 2
            session["user_role"] = "owner"

        res_login = self.client.get("/ownerlogin")
        self.assertEqual(res_login.status_code, 302)
        self.assertEqual(res_login.location, "/owner_dashboard")

        res_register = self.client.get("/ownerregister")
        self.assertEqual(res_register.status_code, 302)
        self.assertEqual(res_register.location, "/owner_dashboard")

    def test_customer_cannot_access_owner_routes(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["user_role"] = "customer"

        response = self.client.get("/owner_dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/vehicles")

    def test_owner_cannot_access_customer_routes(self):
        with self.client.session_transaction() as session:
            session["owner_id"] = 2
            session["user_role"] = "owner"

        response = self.client.get("/vehicles")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/owner_dashboard")

    def test_add_route_get_renders_for_owner(self):
        with self.client.session_transaction() as session:
            session["owner_id"] = 2
            session["user_role"] = "owner"

        fake_rows = [(1, "Tractor", "Tractor", "9876543210", "Active", "Hyderabad")]
        fake_cursor = FakeCursor(all_rows=fake_rows)
        with patch.object(app_module, "cursor", fake_cursor):
            response = self.client.get("/addroute")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add Route", response.data)
