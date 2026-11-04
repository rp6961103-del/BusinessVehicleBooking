"""Create owner subscription request and payment-instruction tables."""
import os

import mysql.connector
from dotenv import load_dotenv


load_dotenv()


def subscription_requests_migration(owner_id_type):
    if owner_id_type not in {"int", "int unsigned"}:
        raise RuntimeError(
            "owners.id must be an INT or INT UNSIGNED column before subscriptions can be installed"
        )

    return f"""
    CREATE TABLE IF NOT EXISTS subscription_requests (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        request_id CHAR(36) NOT NULL,
        owner_id {owner_id_type.upper()} NOT NULL,
        plan_code VARCHAR(20) NOT NULL,
        amount DECIMAL(8, 2) NOT NULL,
        duration_months SMALLINT UNSIGNED NOT NULL,
        status VARCHAR(30) NOT NULL DEFAULT 'pending_verification',
        payment_reference VARCHAR(100) NULL,
        requested_at DATETIME NOT NULL,
        starts_at DATETIME NULL,
        expires_at DATETIME NULL,
        verified_by INT UNSIGNED NULL,
        verified_at DATETIME NULL,
        verification_reason VARCHAR(500) NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_subscription_requests_request_id (request_id),
        UNIQUE KEY uq_subscription_requests_payment_reference (payment_reference),
        KEY idx_subscription_requests_owner_status (owner_id, status, expires_at),
        KEY idx_subscription_requests_status_requested (status, requested_at),
        CONSTRAINT fk_subscription_requests_owner
            FOREIGN KEY (owner_id) REFERENCES owners (id)
            ON UPDATE CASCADE
            ON DELETE RESTRICT,
        CONSTRAINT fk_subscription_requests_admin
            FOREIGN KEY (verified_by) REFERENCES admins (id)
            ON UPDATE CASCADE
            ON DELETE SET NULL
    ) ENGINE=InnoDB
    """


def main():
    required = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing migration configuration: " + ", ".join(missing))

    connection = mysql.connector.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ["MYSQL_PORT"]),
        database=os.environ["MYSQL_DATABASE"],
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
    )
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'owners' "
            "AND COLUMN_NAME = 'id'"
        )
        owner_id_column = cursor.fetchone()
        if owner_id_column is None:
            raise RuntimeError(
                "The owners table is missing; create the existing application schema first"
            )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key VARCHAR(100) NOT NULL,
                setting_value TEXT NOT NULL,
                updated_by INT UNSIGNED NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (setting_key),
                KEY idx_app_settings_updated_by (updated_by),
                CONSTRAINT fk_app_settings_admin
                    FOREIGN KEY (updated_by) REFERENCES admins (id)
                    ON UPDATE CASCADE
                    ON DELETE SET NULL
            ) ENGINE=InnoDB
            """
        )
        cursor.execute(subscription_requests_migration(owner_id_column[0].lower()))
        connection.commit()
        print("Owner subscription schema is ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()
