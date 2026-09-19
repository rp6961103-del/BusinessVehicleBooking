import unittest
from unittest.mock import patch, MagicMock
import app as app_module


class GlobalLanguageSystemTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app_module.app.test_client()

    def csrf_token(self, path="/login"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session.get("csrf_token", "")

    def test_all_languages_have_exact_key_parity(self):
        langs = ['en', 'te', 'ta', 'hi']
        en_keys = set(app_module.TRANSLATIONS['en'].keys())
        self.assertGreaterEqual(len(en_keys), 300)
        for lang in langs:
            lang_keys = set(app_module.TRANSLATIONS[lang].keys())
            diff = en_keys - lang_keys
            self.assertEqual(len(diff), 0, f"Language {lang} is missing keys: {diff}")
            extra = lang_keys - en_keys
            self.assertEqual(len(extra), 0, f"Language {lang} has extra keys: {extra}")

    def test_set_language_sets_session_and_cookie(self):
        for lang in ['te', 'ta', 'hi', 'en']:
            token = self.csrf_token()
            response = self.client.post(
                f"/set_language/{lang}",
                data={"csrf_token": token},
                headers={"Referer": "/"}
            )
            self.assertEqual(response.status_code, 302)
            cookies = response.headers.getlist('Set-Cookie')
            language_cookie_found = any(f"language={lang}" in c for c in cookies)
            self.assertTrue(language_cookie_found, f"Cookie for {lang} not found in {cookies}")

            with self.client.session_transaction() as session:
                self.assertEqual(session.get("language"), lang)

    def test_language_persists_across_pages(self):
        token = self.csrf_token()
        self.client.post("/set_language/te", data={"csrf_token": token})

        public_pages = [
            "/",
            "/login",
            "/register",
            "/ownerlogin",
            "/ownerregister",
            "/admin/login",
            "/farmer-ai",
        ]
        for page in public_pages:
            res = self.client.get(page)
            self.assertEqual(res.status_code, 200, f"Page {page} failed to load")
            self.assertIn(b'lang="te"', res.data, f"Page {page} does not have lang='te'")

    def test_tamil_persists_across_pages(self):
        token = self.csrf_token()
        self.client.post("/set_language/ta", data={"csrf_token": token})

        public_pages = ["/", "/login", "/register", "/ownerlogin", "/ownerregister", "/admin/login", "/farmer-ai"]
        for page in public_pages:
            res = self.client.get(page)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b'lang="ta"', res.data)

    def test_hindi_persists_across_pages(self):
        token = self.csrf_token()
        self.client.post("/set_language/hi", data={"csrf_token": token})

        public_pages = ["/", "/login", "/register", "/ownerlogin", "/ownerregister", "/admin/login", "/farmer-ai"]
        for page in public_pages:
            res = self.client.get(page)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b'lang="hi"', res.data)

    def test_authenticated_customer_pages_persist_language(self):
        token = self.csrf_token()
        self.client.post("/set_language/te", data={"csrf_token": token})
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["user_name"] = "Test Customer"
            session["user_role"] = "customer"

        # Now /vehicles and /mybookings load with 200 (mocking DB calls)
        with patch("app.cursor") as mock_cursor:
            mock_cursor.fetchall.return_value = []
            mock_cursor.fetchone.return_value = None
            res = self.client.get("/vehicles")
            self.assertEqual(res.status_code, 200)
            self.assertIn(b'lang="te"', res.data)

    def test_authenticated_owner_pages_persist_language(self):
        token = self.csrf_token()
        self.client.post("/set_language/te", data={"csrf_token": token})
        with self.client.session_transaction() as session:
            session["owner_id"] = 1
            session["owner_name"] = "Test Owner"
            session["user_role"] = "owner"

        with patch("app.cursor") as mock_cursor:
            mock_cursor.fetchall.return_value = []
            mock_cursor.fetchone.return_value = (0,)
            res = self.client.get("/owner_dashboard")
            self.assertEqual(res.status_code, 200)
            self.assertIn(b'lang="te"', res.data)

    def test_logout_preserves_selected_language(self):
        token = self.csrf_token()
        self.client.post("/set_language/te", data={"csrf_token": token})
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["user_name"] = "Test Customer"
            session["csrf_token"] = token

        # Customer logout
        res = self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(res.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("language"), "te")
            self.assertNotIn("user_id", session)

        # Owner logout
        token = self.csrf_token()
        with self.client.session_transaction() as session:
            session["owner_id"] = 1
            session["owner_name"] = "Test Owner"
            session["csrf_token"] = token
        res = self.client.post("/owner_logout", data={"csrf_token": token})
        self.assertEqual(res.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("language"), "te")
            self.assertNotIn("owner_id", session)


if __name__ == "__main__":
    unittest.main()
