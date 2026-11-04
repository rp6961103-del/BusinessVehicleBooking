from pathlib import Path

import pytest

from .conftest import read_test_config


def test_password_reset_tokens_schema_matches_application_queries():
    schema_path = Path(__file__).resolve().parents[1] / "database" / "vehicle_booking.sql"
    schema = schema_path.read_text(encoding="utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS password_reset_tokens")
    end = schema.index(";", start)
    definition = schema[start:end]

    assert "user_id" not in definition
    for column in (
        "account_type VARCHAR(10) NOT NULL",
        "account_id INT UNSIGNED NOT NULL",
        "token_hash CHAR(64) NOT NULL",
        "created_at DATETIME NOT NULL",
        "expires_at DATETIME NOT NULL",
        "used_at DATETIME NULL",
        "UNIQUE KEY uq_password_reset_token_hash (token_hash)",
        "KEY idx_password_reset_account (account_type, account_id, expires_at)",
        "KEY idx_password_reset_expiry (expires_at)",
    ):
        assert column in definition

    for table in ("users", "owners", "admins"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in schema

    app_source = (schema_path.parents[1] / "app.py").read_text(encoding="utf-8")
    assert "INSERT INTO password_reset_tokens" in app_source
    assert (
        "(account_type, account_id, token_hash, created_at, expires_at)"
        in app_source
    )
    assert "SELECT id, account_id FROM password_reset_tokens" in app_source


def test_test_database_configuration_rejects_application_database(monkeypatch):
    values = {
        "TEST_MYSQL_HOST": "localhost",
        "TEST_MYSQL_PORT": "3306",
        "TEST_MYSQL_DATABASE": "vehicle_booking",
        "TEST_MYSQL_USER": "test_user",
        "TEST_MYSQL_PASSWORD": "not-displayed",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MYSQL_DATABASE", "vehicle_booking")

    with pytest.raises(RuntimeError, match="differ"):
        read_test_config()


def test_test_database_configuration_requires_test_name(monkeypatch):
    values = {
        "TEST_MYSQL_HOST": "localhost",
        "TEST_MYSQL_PORT": "3306",
        "TEST_MYSQL_DATABASE": "vehicle_booking_backup",
        "TEST_MYSQL_USER": "test_user",
        "TEST_MYSQL_PASSWORD": "not-displayed",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MYSQL_DATABASE", "vehicle_booking")

    with pytest.raises(RuntimeError, match="identify a test database"):
        read_test_config()


def test_test_database_configuration_is_optional(monkeypatch):
    for key in (
        "TEST_MYSQL_HOST",
        "TEST_MYSQL_PORT",
        "TEST_MYSQL_DATABASE",
        "TEST_MYSQL_USER",
        "TEST_MYSQL_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    assert read_test_config() is None
