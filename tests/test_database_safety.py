import pytest

from .conftest import read_test_config


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
