import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

cnx = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST", "localhost"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", ""),
    database=os.getenv("MYSQL_DATABASE", "vehicle_booking"),
)

cur = cnx.cursor()


def column_info(table, column):
    cur.execute(
        """
        SELECT COLUMN_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table, column),
    )
    return cur.fetchone()


def column_exists(table, column):
    return column_info(table, column) is not None


def constraint_exists(table, constraint_name):
    cur.execute(
        """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = %s
        """,
        (table, constraint_name),
    )
    return cur.fetchone()[0] > 0


# Make sure bookings.route_id exists
if not column_exists("bookings", "route_id"):
    route_type = column_info("vehicle_routes", "id")[0]

    cur.execute(
        f"ALTER TABLE bookings "
        f"ADD COLUMN route_id {route_type} NULL AFTER vehicle_id"
    )

    cur.execute(
        "ALTER TABLE bookings ADD INDEX idx_bookings_route_id (route_id)"
    )


# Drop an old/incompatible route foreign key before changing route_id
if constraint_exists("bookings", "fk_bookings_route"):
    cur.execute(
        "ALTER TABLE bookings DROP FOREIGN KEY fk_bookings_route"
    )


# Make route_id exactly match vehicle_routes.id
route_type = column_info("vehicle_routes", "id")[0]

cur.execute(
    f"ALTER TABLE bookings MODIFY COLUMN route_id {route_type} NULL"
)


# Create ratings table
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS ratings (
        id INT UNSIGNED NOT NULL AUTO_INCREMENT,
        booking_id INT UNSIGNED NOT NULL,
        vehicle_id INT UNSIGNED NOT NULL,
        customer_phone VARCHAR(20) NOT NULL,
        owner_id INT UNSIGNED NOT NULL,
        rating TINYINT UNSIGNED NOT NULL,
        review VARCHAR(500) NOT NULL DEFAULT '',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uq_ratings_booking (booking_id),
        KEY idx_ratings_vehicle (vehicle_id),
        KEY idx_ratings_owner (owner_id)
    ) ENGINE=InnoDB
    """
)


# Make ratings foreign-key columns match the existing table IDs
booking_type = column_info("bookings", "id")[0]
vehicle_type = column_info("vehicles", "id")[0]
owner_type = column_info("owners", "id")[0]

cur.execute(
    f"ALTER TABLE ratings MODIFY COLUMN booking_id {booking_type} NOT NULL"
)

cur.execute(
    f"ALTER TABLE ratings MODIFY COLUMN vehicle_id {vehicle_type} NOT NULL"
)

cur.execute(
    f"ALTER TABLE ratings MODIFY COLUMN owner_id {owner_type} NOT NULL"
)


# Add route foreign key
if not constraint_exists("bookings", "fk_bookings_route"):
    cur.execute(
        """
        ALTER TABLE bookings
        ADD CONSTRAINT fk_bookings_route
        FOREIGN KEY (route_id)
        REFERENCES vehicle_routes(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
        """
    )


cnx.commit()

cur.close()
cnx.close()

print("Database migration completed successfully.")
print("Route booking and customer rating features are ready.")