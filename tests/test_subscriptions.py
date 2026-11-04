from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from werkzeug.security import generate_password_hash

import app as app_module
from .conftest import csrf_token


def set_owner_session(client, owner_id):
    with client.session_transaction() as session:
        session["owner_id"] = owner_id
        session["owner_name"] = "Integration Owner"


def create_admin(test_db):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
            ("subscription-admin", generate_password_hash("admin-password")),
        )
        test_db.commit()
        return cursor.lastrowid
    finally:
        cursor.close()


def set_admin_session(client, admin_id):
    with client.session_transaction() as session:
        session.pop("owner_id", None)
        session["admin_id"] = admin_id
        session["admin_username"] = "subscription-admin"


def test_subscription_plans_and_month_end_dates_are_exact():
    assert [
        (plan["code"], plan["amount"], plan["duration_months"])
        for plan in app_module.SUBSCRIPTION_PLANS
    ] == [
        ("monthly", Decimal("299.00"), 1),
        ("six_month", Decimal("1499.00"), 6),
        ("annual", Decimal("2499.00"), 12),
    ]
    assert app_module.add_subscription_months(datetime(2024, 1, 31), 1) == datetime(2024, 2, 29)
    assert app_module.add_subscription_months(datetime(2025, 1, 31), 1) == datetime(2025, 2, 28)


def test_subscription_schema_contains_auditable_request_fields():
    schema_path = Path(__file__).resolve().parents[1] / "database" / "vehicle_booking.sql"
    schema = schema_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app_settings (" in schema
    assert "CREATE TABLE IF NOT EXISTS subscription_requests (" in schema
    for column in (
        "request_id CHAR(36) NOT NULL",
        "owner_id INT UNSIGNED NOT NULL",
        "plan_code VARCHAR(20) NOT NULL",
        "amount DECIMAL(8, 2) NOT NULL",
        "payment_reference VARCHAR(100) NULL",
        "starts_at DATETIME NULL",
        "expires_at DATETIME NULL",
        "verified_by INT UNSIGNED NULL",
        "verified_at DATETIME NULL",
        "verification_reason VARCHAR(500) NULL",
        "UNIQUE KEY uq_subscription_requests_payment_reference (payment_reference)",
    ):
        assert column in schema


def test_owner_subscription_status_is_recomputed_against_expiration(monkeypatch):
    class SubscriptionCursor:
        def __init__(self):
            self.rows = [
                (3, "pending-id", "monthly", Decimal("299.00"), 1,
                 "pending_verification", None, datetime(2026, 1, 1),
                 None, None, None, None),
                (2, "expired-id", "annual", Decimal("2499.00"), 12,
                 "approved", "OLD-REF", datetime(2025, 1, 1),
                 datetime(2025, 1, 1), datetime(2025, 12, 31),
                 datetime(2025, 1, 1), None),
                (1, "active-id", "six_month", Decimal("1499.00"), 6,
                 "approved", "ACTIVE-REF", datetime(2025, 12, 1),
                 datetime(2025, 12, 1), datetime(2026, 6, 1),
                 datetime(2025, 12, 1), None),
            ]
            self.query = ""

        def execute(self, query, _values=None):
            self.query = query

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return None

    fake_cursor = SubscriptionCursor()
    monkeypatch.setattr(app_module, "cursor", fake_cursor)
    monkeypatch.setattr(app_module, "utc_now", lambda: datetime(2026, 1, 15))

    context = app_module.load_owner_subscription_context(10)

    assert context["active_subscription"]["plan_name"] == "Six-month"
    assert context["has_pending_subscription_request"] is True
    assert context["subscription_status"] == "Active"
    assert context["has_approved_subscription"] is True
    assert [item["status"] for item in context["subscription_history"]] == [
        "Pending Verification",
        "Expired",
        "Active",
    ]


def test_owner_request_payment_reference_and_admin_approval(
    app_client, test_db, seeded_data
):
    admin_id = create_admin(test_db)
    set_owner_session(app_client, seeded_data["owner_id"])
    csrf = csrf_token(app_client)

    dashboard = app_client.get("/owner_dashboard")
    assert dashboard.status_code == 200
    assert "₹299".encode() in dashboard.data
    assert "₹1,499".encode() in dashboard.data
    assert "₹2,499".encode() in dashboard.data
    assert b"Premium-only platform features have not been designated" in dashboard.data

    response = app_client.post(
        "/owner/subscriptions/request",
        data={"csrf_token": csrf, "plan_code": "six_month"},
    )
    assert response.status_code == 302
    duplicate_request = app_client.post(
        "/owner/subscriptions/request",
        data={"csrf_token": csrf, "plan_code": "annual"},
    )
    assert duplicate_request.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT id, request_id, plan_code, amount, duration_months, status "
            "FROM subscription_requests WHERE owner_id = %s",
            (seeded_data["owner_id"],),
        )
        request_id, public_id, plan_code, amount, duration, status = cursor.fetchone()
        assert str(UUID(public_id)) == public_id
        assert (plan_code, amount, duration, status) == (
            "six_month",
            Decimal("1499.00"),
            6,
            "pending_verification",
        )
    finally:
        cursor.close()

    set_admin_session(app_client, admin_id)
    admin_dashboard = app_client.get("/admin_dashboard")
    assert admin_dashboard.status_code == 200
    assert public_id.encode() in admin_dashboard.data
    assert b"Pending subscription payment requests" in admin_dashboard.data

    set_owner_session(app_client, seeded_data["owner_id"])
    reference_response = app_client.post(
        f"/owner/subscriptions/{request_id}/payment-reference",
        data={"csrf_token": csrf, "payment_reference": "UTR-2026-001234"},
    )
    assert reference_response.status_code == 302
    repeated_reference = app_client.post(
        f"/owner/subscriptions/{request_id}/payment-reference",
        data={"csrf_token": csrf, "payment_reference": "UTR-2026-001234"},
    )
    assert repeated_reference.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT payment_reference, status FROM subscription_requests WHERE id = %s",
            (request_id,),
        )
        assert cursor.fetchone() == ("UTR-2026-001234", "pending_verification")
    finally:
        cursor.close()

    set_admin_session(app_client, admin_id)
    no_confirmation = app_client.post(
        f"/admin/subscriptions/{request_id}/verify",
        data={"csrf_token": csrf, "decision": "approve"},
    )
    assert no_confirmation.status_code == 302
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT status, starts_at, expires_at FROM subscription_requests WHERE id = %s",
            (request_id,),
        )
        assert cursor.fetchone() == ("pending_verification", None, None)
    finally:
        cursor.close()

    approved = app_client.post(
        f"/admin/subscriptions/{request_id}/verify",
        data={
            "csrf_token": csrf,
            "decision": "approve",
            "payment_received": "yes",
            "reason": "Verified against bank statement",
        },
    )
    assert approved.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT status, starts_at, expires_at, verified_by, verified_at, "
            "verification_reason FROM subscription_requests WHERE id = %s",
            (request_id,),
        )
        row = cursor.fetchone()
        assert row[0] == "approved"
        assert row[1] is not None
        assert row[2] > row[1]
        assert row[3] == admin_id
        assert row[4] is not None
        assert row[5] == "Verified against bank statement"
    finally:
        cursor.close()

    repeated = app_client.post(
        f"/admin/subscriptions/{request_id}/verify",
        data={
            "csrf_token": csrf,
            "decision": "approve",
            "payment_received": "yes",
        },
    )
    assert repeated.status_code == 302
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM subscription_requests "
            "WHERE owner_id = %s AND status = 'approved'",
            (seeded_data["owner_id"],),
        )
        assert cursor.fetchone()[0] == 1
    finally:
        cursor.close()


def test_subscription_request_is_owner_scoped_and_payment_instructions_are_configurable(
    app_client, test_db, seeded_data
):
    admin_id = create_admin(test_db)
    set_owner_session(app_client, seeded_data["owner_id"])
    csrf = csrf_token(app_client)
    assert app_client.post(
        "/owner/subscriptions/request",
        data={"csrf_token": csrf, "plan_code": "monthly"},
    ).status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT id FROM subscription_requests WHERE owner_id = %s",
            (seeded_data["owner_id"],),
        )
        subscription_id = cursor.fetchone()[0]
    finally:
        cursor.close()

    with app_client.session_transaction() as session:
        session["owner_id"] = seeded_data["owner_id"] + 100
    unauthorized = app_client.post(
        f"/owner/subscriptions/{subscription_id}/payment-reference",
        data={"csrf_token": csrf, "payment_reference": "PAYMENT-123"},
    )
    assert unauthorized.status_code == 404

    set_admin_session(app_client, admin_id)
    saved = app_client.post(
        "/admin/subscriptions/payment-instructions",
        data={
            "csrf_token": csrf,
            "payment_instructions": "Contact the platform office for verified payment instructions.",
        },
    )
    assert saved.status_code == 302

    set_owner_session(app_client, seeded_data["owner_id"])
    owner_dashboard = app_client.get("/owner_dashboard")
    assert owner_dashboard.status_code == 200
    assert b"Contact the platform office for verified payment instructions." in owner_dashboard.data


def test_admin_can_reject_request_with_reason(app_client, test_db, seeded_data):
    admin_id = create_admin(test_db)
    set_owner_session(app_client, seeded_data["owner_id"])
    csrf = csrf_token(app_client)
    app_client.post(
        "/owner/subscriptions/request",
        data={"csrf_token": csrf, "plan_code": "annual"},
    )

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT id FROM subscription_requests WHERE owner_id = %s",
            (seeded_data["owner_id"],),
        )
        subscription_id = cursor.fetchone()[0]
    finally:
        cursor.close()

    set_admin_session(app_client, admin_id)
    response = app_client.post(
        f"/admin/subscriptions/{subscription_id}/verify",
        data={
            "csrf_token": csrf,
            "decision": "reject",
            "reason": "Payment could not be verified",
        },
    )
    assert response.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT status, verified_by, verified_at, verification_reason, starts_at "
            "FROM subscription_requests WHERE id = %s",
            (subscription_id,),
        )
        row = cursor.fetchone()
        assert row[0] == "rejected"
        assert row[1] == admin_id
        assert row[2] is not None
        assert row[3] == "Payment could not be verified"
        assert row[4] is None
    finally:
        cursor.close()
