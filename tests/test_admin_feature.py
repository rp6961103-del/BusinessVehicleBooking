import app as app_module
from werkzeug.security import generate_password_hash


ADMIN = (7, "admin-user", generate_password_hash("valid-admin-password"))


class AdminCursor:
    def __init__(self):
        self.executed = []
        self.fetchone_rows = [ADMIN]
        self.fetchone_rows.extend((0,) for _ in range(6))

    def execute(self, query, values=None):
        self.executed.append((query, values))

    def fetchone(self):
        return self.fetchone_rows.pop(0) if self.fetchone_rows else (0,)

    def fetchall(self):
        return []


def csrf_for(client, path="/"):
    assert client.get(path).status_code == 200
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_admin_login_route_and_dashboard_gate_are_registered(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    client = app_module.app.test_client()

    login_page = client.get("/adminlogin")
    dashboard = client.get("/admin_dashboard")

    assert login_page.status_code == 200
    assert dashboard.status_code == 302
    assert dashboard.location.endswith("/adminlogin")


def test_admin_login_shows_setup_error_when_admin_table_is_unavailable(monkeypatch):
    class MissingAdminTableCursor:
        def execute(self, query, values=None):
            raise app_module.Error("admin table is unavailable")

    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    monkeypatch.setattr(app_module, "cursor", MissingAdminTableCursor())
    monkeypatch.setattr(app_module, "db", None)
    client = app_module.app.test_client()
    token = csrf_for(client, "/adminlogin")

    response = client.post(
        "/adminlogin",
        data={
            "csrf_token": token,
            "username": "admin-user",
            "password": "valid-admin-password",
        },
    )

    assert response.status_code == 503
    assert b"Admin login is not configured yet" in response.data


def test_admin_dashboard_rejects_customer_and_owner_sessions(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    for session_key in ("user_id", "owner_id"):
        with app_module.app.test_client() as client:
            with client.session_transaction() as session:
                session[session_key] = 123
            response = client.get("/admin_dashboard")
            assert response.status_code == 403


def test_admin_login_dashboard_navigation_and_logout(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    fake_cursor = AdminCursor()
    monkeypatch.setattr(app_module, "cursor", fake_cursor)
    with app_module.app.test_client() as client:
        assert b'href="/adminlogin"' in client.get("/").data
        token = csrf_for(client, "/adminlogin")

        login = client.post(
            "/adminlogin",
            data={
                "csrf_token": token,
                "username": "admin-user",
                "password": "valid-admin-password",
            },
        )

        assert login.status_code == 302
        assert login.location.endswith("/admin_dashboard")
        with client.session_transaction() as session:
            assert session["admin_id"] == ADMIN[0]
            assert session["admin_username"] == ADMIN[1]

        dashboard = client.get("/admin_dashboard")
        assert dashboard.status_code == 200
        assert b"Admin Dashboard" in dashboard.data

        logout_token = csrf_for(client, "/admin_dashboard")
        logout = client.post("/admin_logout", data={"csrf_token": logout_token})
        assert logout.status_code == 302
        assert logout.location.endswith("/adminlogin")
        with client.session_transaction() as session:
            assert "admin_id" not in session


def test_admin_logout_requires_csrf(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["admin_id"] = ADMIN[0]
        response = client.post("/admin_logout")
        assert response.status_code == 400
        assert response.data == b"Invalid CSRF token"
