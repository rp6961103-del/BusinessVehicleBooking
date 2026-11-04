import ast
from http.cookies import SimpleCookie
from pathlib import Path

import app as app_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def csrf_for(client):
    response = client.get("/")
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_language_selection_sets_session_and_safe_root_cookie(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    client = app_module.app.test_client()
    token = csrf_for(client)

    response = client.post(
        "/set_language/te",
        data={"csrf_token": token},
        headers={"Referer": "http://localhost/"},
    )

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["language"] == "te"
    cookie = SimpleCookie()
    cookie.load(response.headers["Set-Cookie"])
    language_cookie = cookie["language"]
    assert language_cookie.value == "te"
    assert language_cookie["path"] == "/"
    assert language_cookie["httponly"]
    assert language_cookie["samesite"] == "Lax"


def test_language_cookie_restores_preference_to_a_new_session(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    client = app_module.app.test_client()
    client.set_cookie("language", "hi")

    response = client.get("/ownerlogin")

    assert response.status_code == 200
    assert b'<html lang="hi">' in response.data
    assert "ईमेल या फोन नंबर".encode() in response.data
    with client.session_transaction() as session:
        assert session["language"] == "hi"


def test_customer_logout_preserves_selected_language_and_csrf(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    with app_module.app.test_client() as test_client:
        token = csrf_for(test_client)
        with test_client.session_transaction() as session:
            session["language"] = "te"
            session["user_id"] = 17

        response = test_client.post("/logout", data={"csrf_token": token})

        assert response.status_code == 302
        assert response.location == "/"
        with test_client.session_transaction() as session:
            assert session["language"] == "te"
            assert session["csrf_token"] != token
            assert "user_id" not in session


def test_supported_translation_dictionaries_have_identical_keys():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    translations_assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "TRANSLATIONS"
            for target in node.targets
        )
    )
    translations = ast.literal_eval(translations_assignment.value)

    assert set(translations) == set(app_module.SUPPORTED_LANGUAGES)
    expected_keys = set(translations["en"])
    for language, dictionary in translations.items():
        assert set(dictionary) == expected_keys, language


def test_application_templates_use_the_current_language_attribute():
    template_directory = PROJECT_ROOT / "templates"
    for template_path in template_directory.rglob("*.html"):
        if "emails" in template_path.parts:
            continue
        source = template_path.read_text(encoding="utf-8")
        has_direct_language_attribute = '<html lang="{{ current_language or \'en\' }}">' in source
        has_conditional_language_attribute = (
            "{% if current_language == 'en' %}" in source
            and '<html lang="en">' in source
            and '<html lang="{{ current_language }}">' in source
        )
        assert has_direct_language_attribute or has_conditional_language_attribute, template_path


def test_unsupported_language_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_db_connection", lambda: None)
    client = app_module.app.test_client()
    client.set_cookie("language", "xx")

    response = client.get("/ownerlogin")

    assert response.status_code == 200
    assert b'<html lang="en">' in response.data
    with client.session_transaction() as session:
        assert session["language"] == "en"
