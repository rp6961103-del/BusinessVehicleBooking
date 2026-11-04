import app as app_module


def test_language_selection_persists_across_login_and_navigation(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    client = app_module.app.test_client()
    home = client.get("/")
    assert home.status_code == 200
    with client.session_transaction() as session:
        token = session["csrf_token"]

    changed = client.post(
        "/set_language/hi",
        data={"csrf_token": token},
        headers={"Referer": "http://localhost/"},
    )
    assert changed.status_code == 302
    owner_login = client.get("/ownerlogin")

    assert owner_login.status_code == 200
    assert b'<html lang="hi">' in owner_login.data
    with client.session_transaction() as session:
        assert session["language"] == "hi"


def test_language_selection_requires_csrf(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    client = app_module.app.test_client()

    response = client.post("/set_language/te")

    assert response.status_code == 400
    assert response.data == b"Invalid CSRF token"


def test_home_page_exposes_admin_entry_without_bypassing_login(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    client = app_module.app.test_client()

    home = client.get("/")
    admin_entry = client.get("/adminlogin")

    assert home.status_code == 200
    assert b'href="/adminlogin"' in home.data
    assert b"Admin Dashboard" in home.data
    assert admin_entry.status_code == 200
