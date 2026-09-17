import os
from pathlib import Path

import mysql.connector
import pytest
from werkzeug.security import generate_password_hash


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "database" / "vehicle_booking.sql"
TEST_CONFIG_KEYS = (
    "TEST_MYSQL_HOST",
    "TEST_MYSQL_PORT",
    "TEST_MYSQL_DATABASE",
    "TEST_MYSQL_USER",
    "TEST_MYSQL_PASSWORD",
)


def read_test_config():
    values = {key: os.getenv(key) for key in TEST_CONFIG_KEYS}
    if not all(values.values()):
        return None

    application_database = os.getenv(
        "_APPLICATION_MYSQL_DATABASE",
        os.getenv("MYSQL_DATABASE", "vehicle_booking"),
    )
    test_database = values["TEST_MYSQL_DATABASE"]
    if test_database == application_database:
        raise RuntimeError("TEST_MYSQL_DATABASE must differ from MYSQL_DATABASE")
    if "test" not in test_database.lower():
        raise RuntimeError("TEST_MYSQL_DATABASE must identify a test database")

    return {
        "host": values["TEST_MYSQL_HOST"],
        "port": int(values["TEST_MYSQL_PORT"]),
        "database": test_database,
        "user": values["TEST_MYSQL_USER"],
        "password": values["TEST_MYSQL_PASSWORD"],
    }


def pytest_configure(config):
    test_config = read_test_config()
    if not test_config:
        return

    # Ensure app.py uses the explicitly selected test database during pytest collection.
    os.environ.setdefault(
        "_APPLICATION_MYSQL_DATABASE",
        os.getenv("MYSQL_DATABASE", "vehicle_booking"),
    )
    os.environ.update(
        MYSQL_HOST=test_config["host"],
        MYSQL_PORT=str(test_config["port"]),
        MYSQL_DATABASE=test_config["database"],
        MYSQL_USER=test_config["user"],
        MYSQL_PASSWORD=test_config["password"],
    )


def schema_statements():
    statements = []
    for statement in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        statement = statement.strip()
        if statement and statement.upper().startswith("CREATE TABLE"):
            statements.append(statement)
    return statements


def management_schema_statements():
    return (
        "ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL",
        "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE owners ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE vehicles ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
    )


def clear_test_data(connection):
    cursor = connection.cursor()
    try:
        for table in ("payments", "bookings", "vehicle_routes", "vehicles", "password_reset_tokens", "owners", "users", "admins"):
            cursor.execute(f"DELETE FROM {table}")
        connection.commit()
    finally:
        cursor.close()


@pytest.fixture
def test_db():
    config = read_test_config()
    if not config:
        pytest.skip(
            "Integration tests require TEST_MYSQL_HOST, TEST_MYSQL_PORT, "
            "TEST_MYSQL_DATABASE, TEST_MYSQL_USER, and TEST_MYSQL_PASSWORD"
        )

    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(**config)
        cursor = connection.cursor()
        for statement in schema_statements():
            cursor.execute(statement)
        for statement in management_schema_statements():
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                if "Duplicate column name" not in str(error):
                    raise
        connection.commit()
        clear_test_data(connection)
        yield connection
    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        pytest.fail(f"Test database setup failed: {type(error).__name__}")
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            clear_test_data(connection)
            connection.close()


@pytest.fixture
def app_client(test_db, monkeypatch):
    import app as app_module

    connection_cursor = test_db.cursor()
    previous_db = app_module.db
    previous_cursor = app_module.cursor
    app_module.db = test_db
    app_module.cursor = connection_cursor
    app_module.app.config.update(TESTING=True)

    try:
        yield app_module.app.test_client()
    finally:
        app_module.db = previous_db
        app_module.cursor = previous_cursor
        connection_cursor.close()


@pytest.fixture
def seeded_data(test_db):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO owners (owner_name, phone, email, password_hash) "
            "VALUES (%s, %s, %s, %s)",
            ("Integration Owner", "9000000001", "owner@test.example", generate_password_hash("owner-password")),
        )
        owner_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO users (name, phone, password_hash, email) VALUES (%s, %s, %s, %s)",
            ("Integration Customer", "9000000002", generate_password_hash("customer-password"), "customer@test.example"),
        )
        customer_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicles (vehicle_name, vehicle_type, owner_id, contact_number, location) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("Integration Truck", "Pickup", owner_id, "9000000001", "Test Town"),
        )
        vehicle_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicle_routes (vehicle_id, from_location, to_location, rent) "
            "VALUES (%s, %s, %s, %s)",
            (vehicle_id, "Test Town", "Test City", "2500.00"),
        )
        test_db.commit()
        return {"owner_id": owner_id, "customer_id": customer_id, "vehicle_id": vehicle_id}
    finally:
        cursor.close()


def csrf_token(client, path="/login"):
    response = client.get(path)
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session["csrf_token"]


def login_customer(client, customer):
    token = csrf_token(client)
    response = client.post(
        "/login",
        data={
            "csrf_token": token,
            "phone": customer["phone"],
            "password": "customer-password",
        },
    )
    assert response.status_code == 302
    return response


def login_owner(client, owner):
    token = csrf_token(client)
    response = client.post(
        "/ownerlogin",
        data={
            "csrf_token": token,
            "email": owner["email"],
            "password": "owner-password",
        },
    )
    assert response.status_code == 302
    return response
