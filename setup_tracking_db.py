"""
Database setup script for Live Vehicle Location Tracking.
Creates new tracking tables without altering or modifying any existing tables.
"""

import os
import logging
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("setup_tracking_db")


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST") or "localhost",
        port=int(os.getenv("MYSQL_PORT") or "3306"),
        user=os.getenv("MYSQL_USER") or "root",
        password=os.getenv("MYSQL_PASSWORD") or "",
        database=os.getenv("MYSQL_DATABASE") or "vehicle_booking",
    )


def create_tracking_tables():
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        # Table 1: vehicle_tracking_sessions
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicle_tracking_sessions (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT,
                owner_id INT UNSIGNED NOT NULL,
                vehicle_id INT UNSIGNED NOT NULL,
                booking_id INT UNSIGNED NULL,
                registration_number VARCHAR(50) NULL,
                session_token VARCHAR(64) NOT NULL,
                access_token VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                stopped_at TIMESTAMP NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_vts_session_token (session_token),
                UNIQUE KEY uq_vts_access_token (access_token),
                KEY idx_vts_owner_id (owner_id),
                KEY idx_vts_vehicle_id (vehicle_id),
                KEY idx_vts_booking_id (booking_id),
                KEY idx_vts_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        logger.info("Checked/created vehicle_tracking_sessions table.")

        # Table 2: vehicle_live_locations (latest position cache per vehicle)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicle_live_locations (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT,
                session_id INT UNSIGNED NOT NULL,
                vehicle_id INT UNSIGNED NOT NULL,
                latitude DECIMAL(10, 7) NOT NULL,
                longitude DECIMAL(10, 7) NOT NULL,
                accuracy DECIMAL(8, 2) NULL,
                heading DECIMAL(5, 2) NULL,
                speed DECIMAL(6, 2) NULL,
                altitude DECIMAL(7, 2) NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_vll_vehicle_id (vehicle_id),
                KEY idx_vll_session_id (session_id),
                KEY idx_vll_recorded_at (recorded_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        logger.info("Checked/created vehicle_live_locations table.")

        # Table 3: vehicle_location_history (breadcrumb trail for trip history)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicle_location_history (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT,
                session_id INT UNSIGNED NOT NULL,
                vehicle_id INT UNSIGNED NOT NULL,
                latitude DECIMAL(10, 7) NOT NULL,
                longitude DECIMAL(10, 7) NOT NULL,
                accuracy DECIMAL(8, 2) NULL,
                heading DECIMAL(5, 2) NULL,
                speed DECIMAL(6, 2) NULL,
                recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_vlh_session_id (session_id),
                KEY idx_vlh_vehicle_id (vehicle_id),
                KEY idx_vlh_recorded_at (recorded_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        logger.info("Checked/created vehicle_location_history table.")

        connection.commit()
        logger.info("All live tracking tables created or verified successfully!")
        return True

    except Exception as exc:
        connection.rollback()
        logger.error("Error creating live tracking tables: %s", exc)
        raise
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    create_tracking_tables()
