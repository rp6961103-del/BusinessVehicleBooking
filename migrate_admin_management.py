import os

import mysql.connector
from dotenv import load_dotenv


load_dotenv()


MIGRATIONS = (
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