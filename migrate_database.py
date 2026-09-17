import os
import logging

import mysql.connector
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash


load_dotenv()
logger = logging.getLogger(__name__)


def apply_migrations(connection):
    cursor = connection.cursor()
    try:
        for statement in (
            """CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id INT UNSIGNED NOT NULL,
                token_hash CHAR(64) NOT NULL,
                expires_at DATETIME NOT NULL,
                used_at DATETIME NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_password_reset_token_hash (token_hash),
                KEY idx_password_reset_user (user_id),
                CONSTRAINT fk_password_reset_user
                    FOREIGN KEY (user_id) REFERENCES users (id)
                    ON UPDATE CASCADE ON DELETE CASCADE
            ) ENGINE=InnoDB""",
            "ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NULL",
            "ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL",
            "ALTER TABLE owners ADD COLUMN password_hash VARCHAR(255) NULL",
            "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE owners ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE vehicles ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
        ):
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                if "Duplicate column name" not in str(error):
                    raise

        for table in ("users", "owners"):
            try:
                cursor.execute(
                    f"SELECT id, password, password_hash FROM {table} "
                    "WHERE password IS NOT NULL"
                )
            except mysql.connector.Error as error:
                if "Unknown column 'password'" in str(error):
                    continue
                raise
            for record_id, legacy_password, password_hash in cursor.fetchall():
                migrated_hash = password_hash or (
                    legacy_password
                    if legacy_password.startswith(("scrypt:", "pbkdf2:", "argon2:"))
                    else generate_password_hash(legacy_password)
                )
                cursor.execute(
                    f"UPDATE {table} SET password_hash = %s, password = NULL WHERE id = %s",
                    (migrated_hash, record_id),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


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
    try:
        apply_migrations(connection)
        logger.info("Database migrations completed")
    finally:
        connection.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()