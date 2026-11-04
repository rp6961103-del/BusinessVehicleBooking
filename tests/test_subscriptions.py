from datetime import datetime
from decimal import Decimal
from pathlib import Path

import app as app_module


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
        rows = [
            (
                3, "pending-id", "monthly", Decimal("299.00"), 1,
                "pending_verification", None, datetime(2026, 1, 1),
                None, None, None, None,
            ),
            (
                2, "expired-id", "annual", Decimal("2499.00"), 12,
                "approved", "OLD-REF", datetime(2025, 1, 1),
                datetime(2025, 1, 1), datetime(2025, 12, 31),
                datetime(2025, 1, 1), None,
            ),
            (
                1, "active-id", "six_month", Decimal("1499.00"), 6,
                "approved", "ACTIVE-REF", datetime(2025, 12, 1),
                datetime(2025, 12, 1), datetime(2026, 6, 1),
                datetime(2025, 12, 1), None,
            ),
        ]

        def execute(self, _query, _values=None):
            pass

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return None

    monkeypatch.setattr(app_module, "cursor", SubscriptionCursor())
    monkeypatch.setattr(app_module, "utc_now", lambda: datetime(2026, 1, 15))

    context = app_module.load_owner_subscription_context(10)

    assert context["active_subscription"]["plan_name"] == "Six-month"
    assert context["has_pending_subscription_request"] is True
    assert context["subscription_status"] == "Active"
    assert [item["status"] for item in context["subscription_history"]] == [
        "Pending Verification",
        "Expired",
        "Active",
    ]
