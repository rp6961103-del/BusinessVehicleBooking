import getpass
import os
import re

import mysql.connector
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash


load_dotenv()


def valid_username(value):
    return bool(re.fullmatch(r"[a-z0-9_.-]{3,100}", value.strip().lower()))


def main():
    username = input("Admin username: ").strip().lower()
    if not valid_username(username):
        raise SystemExit("Username must be 3-100 characters: lowercase letters, numbers, ., _, or -.")

    password = getpass.getpass("Admin password: ")
    confirmation = getpass.getpass("Confirm admin password: ")
    if password != confirmation or not 8 <= len(password) <= 128:
        raise SystemExit("Passwords must match and be 8-128 characters long.")

    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "vehicle_booking"),
    )
    cursor = connection.cursor()
    try:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS admins ("
            "id INT UNSIGNED NOT NULL AUTO_INCREMENT,"
            "username VARCHAR(100) NOT NULL,"
            "password_hash VARCHAR(255) NOT NULL,"
            "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "PRIMARY KEY (id),"
            "UNIQUE KEY uq_admins_username (username)"
            ") ENGINE = InnoDB"
        )
        connection.commit()
        cursor.execute("SELECT id FROM admins WHERE username = %s", (username,))
        if cursor.fetchone():
            raise SystemExit("That admin username already exists.")

        cursor.execute(
            "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
            (username, generate_password_hash(password)),
        )
        connection.commit()
        print("Admin provisioned successfully.")
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()
