from .conftest import csrf_token
from werkzeug.security import generate_password_hash


ADMIN_USERNAME = "phase8-admin"
ADMIN_PASSWORD = "phase8-admin-password"


def create_admin(test_db):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD)),
        )
        test_db.commit()
    finally:
        cursor.close()


def login_admin(client):
    token = csrf_token(client, "/adminlogin")
    response = client.post(
        "/adminlogin",
        data={
            "csrf_token": token,
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 302
    return response


def test_admin_login_success_and_session(app_client, test_db):
    create_admin(test_db)

    response = login_admin(app_client)

    assert response.location.endswith("/admin_dashboard")
    with app_client.session_transaction() as session:
        assert session["admin_id"]
        assert session["admin_username"] == ADMIN_USERNAME


def test_invalid_admin_login(app_client, test_db):
    create_admin(test_db)
    token = csrf_token(app_client, "/adminlogin")

    response = app_client.post(
        "/adminlogin",
        data={
            "csrf_token": token,
            "username": ADMIN_USERNAME,
            "password": "wrong-password",
        },
    )

    assert response.status_code == 200
    assert b"Invalid admin login" in response.data


def test_admin_routes_require_admin_session(app_client):
    assert app_client.get("/admin_dashboard").location.endswith("/adminlogin")
    assert app_client.get("/admin/customers").location.endswith("/adminlogin")


def test_customer_cannot_access_admin_dashboard(app_client):
    with app_client.session_transaction() as session:
        session["user_id"] = 1

    assert app_client.get("/admin_dashboard").status_code == 403


def test_owner_cannot_access_admin_dashboard(app_client):
    with app_client.session_transaction() as session:
        session["owner_id"] = 1

    assert app_client.get("/admin_dashboard").status_code == 403


def test_admin_logout(app_client, test_db):
    create_admin(test_db)
    login_admin(app_client)
    token = csrf_token(app_client, "/admin_dashboard")

    response = app_client.post("/admin_logout", data={"csrf_token": token})

    assert response.status_code == 302
    assert response.location.endswith("/adminlogin")
    with app_client.session_transaction() as session:
        assert "admin_id" not in session


def test_admin_dashboard_loads(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = app_client.get("/admin_dashboard")

    assert response.status_code == 200
    assert b"Admin Dashboard" in response.data
    assert b"Customers" in response.data


def test_admin_customer_list_loads(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = app_client.get("/admin/customers?q=Integration")

    assert response.status_code == 200
    assert b"Integration Customer" in response.data


def test_admin_owner_list_loads(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = app_client.get("/admin/owners?q=Integration")

    assert response.status_code == 200
    assert b"Integration Owner" in response.data


def test_admin_vehicle_list_loads(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = app_client.get("/admin/vehicles?q=Integration")

    assert response.status_code == 200
    assert b"Integration Truck" in response.data


def test_admin_booking_list_loads(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = app_client.get("/admin/bookings?status=Pending")

    assert response.status_code == 200


def test_admin_get_views_do_not_change_counts(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM users, owners, vehicles, vehicle_routes, bookings, admins"
        )
        before = cursor.fetchone()[0]
    finally:
        cursor.close()

    for path in (
        "/admin_dashboard",
        "/admin/customers",
        "/admin/owners",
        "/admin/vehicles",
        "/admin/bookings",
    ):
        assert app_client.get(path).status_code == 200

    cursor = test_db.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM users, owners, vehicles, vehicle_routes, bookings, admins"
        )
        after = cursor.fetchone()[0]
    finally:
        cursor.close()

    assert after == before


def test_admin_logout_requires_csrf(app_client, test_db):
    create_admin(test_db)
    login_admin(app_client)

    assert app_client.post("/admin_logout").status_code == 400


def test_admin_booking_status_filter_rejects_invalid_value(app_client, test_db):
    create_admin(test_db)
    login_admin(app_client)

    assert app_client.get("/admin/bookings?status=Unknown").status_code == 400


def post_admin_status(client, path, status):
    token = csrf_token(client, "/admin_dashboard")
    return client.post(
        path,
        data={"csrf_token": token, "status": status},
    )


def test_admin_can_deactivate_and_reactivate_customer(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = post_admin_status(
        app_client,
        f"/admin/customers/{seeded_data['customer_id']}/status",
        "inactive",
    )
    assert response.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT is_active FROM users WHERE id = %s", (seeded_data["customer_id"],))
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()

    response = post_admin_status(
        app_client,
        f"/admin/customers/{seeded_data['customer_id']}/status",
        "active",
    )
    assert response.status_code == 302


def test_admin_can_deactivate_and_reactivate_owner(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = post_admin_status(
        app_client,
        f"/admin/owners/{seeded_data['owner_id']}/status",
        "inactive",
    )
    assert response.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT is_active FROM owners WHERE id = %s", (seeded_data["owner_id"],))
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()


def test_admin_can_deactivate_and_reactivate_vehicle(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    response = post_admin_status(
        app_client,
        f"/admin/vehicles/{seeded_data['vehicle_id']}/status",
        "inactive",
    )
    assert response.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT is_active FROM vehicles WHERE id = %s", (seeded_data["vehicle_id"],))
        assert cursor.fetchone()[0] == 0
    finally:
        cursor.close()


def create_booking(test_db, seeded_data):
    cursor = test_db.cursor()
    try:
        cursor.execute(
            "INSERT INTO bookings "
            "(vehicle_id, customer_name, phone, booking_date, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (seeded_data["vehicle_id"], "Integration Customer", "9000000002", "2099-01-01", "Pending"),
        )
        test_db.commit()
        return cursor.lastrowid
    finally:
        cursor.close()


def test_admin_can_update_pending_booking_status(app_client, test_db, seeded_data):
    create_admin(test_db)
    booking_id = create_booking(test_db, seeded_data)
    login_admin(app_client)

    response = post_admin_status(
        app_client,
        f"/admin/bookings/{booking_id}/status",
        "Accepted",
    )
    assert response.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
        assert cursor.fetchone()[0] == "Accepted"
    finally:
        cursor.close()


def test_admin_rejects_invalid_booking_transition(app_client, test_db, seeded_data):
    create_admin(test_db)
    booking_id = create_booking(test_db, seeded_data)
    cursor = test_db.cursor()
    try:
        cursor.execute("UPDATE bookings SET status = 'Accepted' WHERE id = %s", (booking_id,))
        test_db.commit()
    finally:
        cursor.close()
    login_admin(app_client)

    response = post_admin_status(
        app_client,
        f"/admin/bookings/{booking_id}/status",
        "Rejected",
    )
    assert response.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT status FROM bookings WHERE id = %s", (booking_id,))
        assert cursor.fetchone()[0] == "Accepted"
    finally:
        cursor.close()


def test_admin_actions_require_post_and_csrf(app_client, test_db, seeded_data):
    create_admin(test_db)
    login_admin(app_client)

    assert app_client.get(f"/admin/customers/{seeded_data['customer_id']}/status").status_code == 405
    assert app_client.post(
        f"/admin/customers/{seeded_data['customer_id']}/status",
        data={"status": "inactive"},
    ).status_code == 400


def test_non_admin_cannot_submit_admin_action(app_client, seeded_data):
    with app_client.session_transaction() as session:
        session["user_id"] = seeded_data["customer_id"]

    token = csrf_token(app_client, "/adminlogin")
    assert app_client.post(
        f"/admin/customers/{seeded_data['customer_id']}/status",
        data={"csrf_token": token, "status": "inactive"},
    ).status_code == 403


def test_admin_rejects_invalid_management_ids(app_client, test_db):
    create_admin(test_db)
    login_admin(app_client)

    for path in (
        "/admin/customers/999999/status",
        "/admin/owners/999999/status",
        "/admin/vehicles/999999/status",
        "/admin/bookings/999999/status",
    ):
        assert post_admin_status(app_client, path, "inactive").status_code in {400, 404}


def test_vehicle_status_change_preserves_booking_history(app_client, test_db, seeded_data):
    create_admin(test_db)
    booking_id = create_booking(test_db, seeded_data)
    login_admin(app_client)

    response = post_admin_status(
        app_client,
        f"/admin/vehicles/{seeded_data['vehicle_id']}/status",
        "inactive",
    )
    assert response.status_code == 302

    cursor = test_db.cursor()
    try:
        cursor.execute("SELECT id, status FROM bookings WHERE id = %s", (booking_id,))
        assert cursor.fetchone() == (booking_id, "Pending")
    finally:
        cursor.close()
