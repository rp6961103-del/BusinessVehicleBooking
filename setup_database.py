"""Create/update the BusinessVehicleBooking MySQL schema."""
import os
from pathlib import Path
import mysql.connector
from dotenv import load_dotenv

load_dotenv()
DB_NAME = os.getenv("MYSQL_DATABASE", "vehicle_booking")
config = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
}

sql_path = Path(__file__).parent / "database" / "vehicle_booking.sql"
sql = sql_path.read_text(encoding="utf-8")
cnx = mysql.connector.connect(**config)
cur = cnx.cursor()
try:
    # Execute statements one at a time; comments are removed for simple local schema setup.
    cleaned = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for statement in cleaned.split(";"):
        statement = statement.strip()
        if statement:
            cur.execute(statement)
    cnx.commit()
    print(f"Database '{DB_NAME}' is ready.")
finally:
    cur.close()
    cnx.close()
