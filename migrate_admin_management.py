import os

import mysql.connector
from dotenv import load_dotenv


load_dotenv()


MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS admins ("
    "id INT UNSIGNED NOT NULL AUTO_INCREMENT,"
    "username VARCHAR(100) NOT NULL,"
    "password_hash VARCHAR(255) NOT NULL,"
    "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
    "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
    "PRIMARY KEY (id),"
    "UNIQUE KEY uq_admins_username (username)"
    ") ENGINE=InnoDB",
    "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE owners ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE vehicles ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
)


def main():
    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "vehicle_booking"),
    )
    cursor = connection.cursor()
    try:
        for statement in MIGRATIONS:
            try:
                cursor.execute(statement)
            except mysql.connector.Error as error:
                if "Duplicate column name" not in str(error):
                    raise
        connection.commit()
        print("Admin management schema is ready.")
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()