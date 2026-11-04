"""
Live Vehicle Location Tracking - Flask Blueprint Module.
Provides dedicated routes, templates, and API endpoints for live vehicle GPS tracking.
Completely separate from existing app routes.
"""

import logging
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from mysql.connector import Error
from tracking_service import (
    get_owner_tracking_options,
    start_tracking_session,
    stop_tracking_session,
    record_live_location,
    get_live_tracking_data_by_token,
    get_live_tracking_data_by_booking,
    get_road_route,
    geocode_location,
    validate_coordinates,
    get_db_connection,
)

logger = logging.getLogger("tracking_routes")

# Create Flask Blueprint with unique prefix /live-tracking
tracking_bp = Blueprint(
    "live_tracking",
    __name__,
    url_prefix="/live-tracking",
    template_folder="templates",
    static_folder="static",
)


@tracking_bp.route("/")
def tracking_hub():
    """
    Tracking Feature Hub & Test Dashboard.
    Provides entry points for both Vehicle Owners and Customers, plus direct test links.
    """
    owner_id = session.get("owner_id")
    user_id = session.get("user_id")
    user_phone = session.get("user_phone")

    owner_options = None
    if owner_id:
        try:
            owner_options = get_owner_tracking_options(owner_id)
        except Exception as exc:
            logger.warning("Could not fetch owner options: %s", exc)

    # Show booking links only to the customer or owner who can access them.
    sample_bookings = []
    sample_sessions = []
    try:
        cnx = get_db_connection()
        cur = cnx.cursor(dictionary=True)
        if owner_id:
            cur.execute(
                """
                SELECT b.id AS booking_id, b.customer_name, b.phone, b.booking_date, b.status,
                       v.vehicle_name, v.vehicle_type, v.owner_id,
                       vr.from_location, vr.to_location
                FROM bookings b
                JOIN vehicles v ON b.vehicle_id = v.id
                LEFT JOIN vehicle_routes vr ON b.route_id = vr.id
                WHERE v.owner_id = %s
                ORDER BY b.id DESC
                LIMIT 6
                """,
                (owner_id,),
            )
            sample_bookings = cur.fetchall()
            cur.execute(
                """
                SELECT s.id, s.vehicle_id, s.booking_id, s.registration_number,
                       s.status, s.started_at,
                       v.vehicle_name, v.vehicle_type,
                       loc.latitude, loc.longitude, loc.recorded_at
                FROM vehicle_tracking_sessions s
                JOIN vehicles v ON s.vehicle_id = v.id
                LEFT JOIN vehicle_live_locations loc
                    ON loc.vehicle_id = s.vehicle_id AND loc.session_id = s.id
                WHERE s.owner_id = %s
                ORDER BY s.id DESC
                LIMIT 5
                """,
                (owner_id,),
            )
            sample_sessions = cur.fetchall()
        elif user_phone:
            cur.execute(
                """
                SELECT b.id AS booking_id, b.customer_name, b.phone, b.booking_date, b.status,
                       v.vehicle_name, v.vehicle_type, v.owner_id,
                       vr.from_location, vr.to_location
                FROM bookings b
                JOIN vehicles v ON b.vehicle_id = v.id
                LEFT JOIN vehicle_routes vr ON b.route_id = vr.id
                WHERE b.phone = %s
                ORDER BY b.id DESC
                LIMIT 6
                """,
                (user_phone,),
            )
            sample_bookings = cur.fetchall()
        cur.close()
        cnx.close()
    except Exception as exc:
        logger.warning("Could not fetch sample bookings/sessions: %s", exc)

    return render_template(
        "tracking_hub.html",
        owner_id=owner_id,
        owner_name=session.get("owner_name"),
        user_id=user_id,
        user_name=session.get("user_name"),
        user_phone=user_phone,
        owner_options=owner_options,
        sample_bookings=sample_bookings,
        sample_sessions=sample_sessions,
    )


@tracking_bp.route("/owner")
def owner_tracking_page():
    """
    Vehicle Owner Live GPS Broadcasting Page.
    Allows vehicle owner to grant GPS permission, start live location broadcasting,
    view real-time GPS coordinates on an interactive Leaflet map, and stop sharing.
    """
    owner_id = session.get("owner_id")
    if not owner_id:
        return render_template(
            "owner_live_tracking.html",
            is_authenticated=False,
            login_url="/ownerlogin",
            error_message="Please log in as a Vehicle Owner to access the live location broadcaster.",
        )

    try:
        options = get_owner_tracking_options(owner_id)
        return render_template(
            "owner_live_tracking.html",
            is_authenticated=True,
            owner_id=owner_id,
            owner_name=session.get("owner_name", "Vehicle Owner"),
            vehicles=options.get("vehicles", []),
            bookings=options.get("bookings", []),
            active_session=options.get("active_session"),
        )
    except Exception as exc:
        logger.exception("Error loading owner tracking page")
        return render_template(
            "owner_live_tracking.html",
            is_authenticated=True,
            owner_id=owner_id,
            error_message=f"Unable to load vehicles: {exc}",
            vehicles=[],
            bookings=[],
        )


@tracking_bp.route("/customer")
def customer_tracking_page():
    """
    Customer Live Vehicle Tracking Page.
    Displays Leaflet map with vehicle's latest real GPS position, pickup and destination
    markers, vehicle registration and booking details, updated time, and status.
    """
    access_token = request.args.get("token", "").strip()
    booking_id = request.args.get("booking_id", "").strip()

    initial_data = None
    error_message = None

    if access_token:
        try:
            data, err = get_live_tracking_data_by_token(access_token)
            if err:
                error_message = err
            else:
                initial_data = data
        except Exception as exc:
            error_message = f"Error fetching tracking data: {exc}"
    elif booking_id and booking_id.isdigit():
        customer_phone = session.get("user_phone") if session.get("user_id") else None
        customer_name = session.get("user_name")
        owner_id = session.get("owner_id")
        try:
            data, err = get_live_tracking_data_by_booking(
                int(booking_id),
                customer_phone=customer_phone,
                customer_name=customer_name,
                owner_id=owner_id,
            )
            if err:
                error_message = err
            else:
                initial_data = data
        except Exception as exc:
            error_message = f"Error fetching booking tracking: {exc}"

    return render_template(
        "customer_live_tracking.html",
        access_token=access_token,
        booking_id=booking_id,
        initial_data=initial_data,
        error_message=error_message,
        logged_in_phone=session.get("user_phone"),
    )


@tracking_bp.route("/booking/<int:booking_id>")
def booking_tracking_shortcut(booking_id):
    """
    Full-screen live map for a booking owned by the authenticated customer.
    """
    if not session.get("user_id"):
        return redirect(url_for("login"))

    customer_phone = session.get("user_phone")
    if not customer_phone:
        logger.warning(
            "Booking tracking denied because authenticated customer session has no phone"
        )
        return render_template(
            "customer_tracking_fullscreen.html",
            booking_id=booking_id,
            error_message="Your customer session could not be verified. Please sign in again.",
            initial_data=None,
        ), 403

    try:
        data, error = get_live_tracking_data_by_booking(
            booking_id=booking_id,
            customer_phone=customer_phone,
            customer_name=session.get("user_name"),
        )
    except Error as exc:
        logger.error(
            "Could not load full-screen tracking data "
            "(error_type=%s, errno=%s, booking_id=%s)",
            type(exc).__name__,
            getattr(exc, "errno", None),
            booking_id,
        )
        return render_template(
            "customer_tracking_fullscreen.html",
            booking_id=booking_id,
            error_message="Tracking is temporarily unavailable. Please try again shortly.",
            initial_data=None,
        ), 503
    except Exception:
        logger.exception(
            "Unexpected error loading booking tracking (booking_id=%s)",
            booking_id,
        )
        return render_template(
            "customer_tracking_fullscreen.html",
            booking_id=booking_id,
            error_message="Tracking is temporarily unavailable. Please try again shortly.",
            initial_data=None,
        ), 503

    if error:
        status_code = 403 if "Unauthorized" in error else 404
        message = (
            "You are not authorized to view this booking."
            if status_code == 403
            else "This booking could not be found."
        )
        return render_template(
            "customer_tracking_fullscreen.html",
            booking_id=booking_id,
            error_message=message,
            initial_data=None,
        ), status_code

    if not data or not data.get("booking") or data["booking"].get("id") != booking_id:
        logger.error(
            "Tracking service returned data for a different booking (requested_booking_id=%s)",
            booking_id,
        )
        return render_template(
            "customer_tracking_fullscreen.html",
            booking_id=booking_id,
            error_message="Tracking information could not be verified for this booking.",
            initial_data=None,
        ), 404

    return render_template(
        "customer_tracking_fullscreen.html",
        booking_id=booking_id,
        initial_data=data,
        error_message=None,
    )


# =========================================================================
# API ENDPOINTS FOR LIVE TRACKING
# =========================================================================


@tracking_bp.route("/api/session/start", methods=["POST"])
def api_start_session():
    """
    API: Owner initiates a live tracking session for one of their vehicles.
    """
    owner_id = session.get("owner_id")
    if not owner_id:
        return jsonify({"success": False, "error": "Unauthorized. Owner login required."}), 403

    payload = request.get_json(silent=True) or request.form
    vehicle_id = payload.get("vehicle_id")
    booking_id = payload.get("booking_id") or None
    registration_number = payload.get("registration_number") or ""

    if not vehicle_id:
        return jsonify({"success": False, "error": "Vehicle selection is required."}), 400

    try:
        vehicle_id = int(vehicle_id)
        if booking_id:
            booking_id = int(booking_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid vehicle or booking identifier."}), 400

    try:
        session_data, err = start_tracking_session(
            owner_id=owner_id,
            vehicle_id=vehicle_id,
            booking_id=booking_id,
            registration_number=registration_number,
        )
    except Error as exc:
        logger.error(
            "Tracking session start database failure (owner_id=%s, errno=%s)",
            owner_id,
            getattr(exc, "errno", None),
        )
        return jsonify({
            "success": False,
            "error": "Unable to start tracking right now. Please try again shortly.",
        }), 503

    if err:
        if "Database error" in err:
            logger.error("Tracking session start failed because of a database error (owner_id=%s)", owner_id)
            return jsonify({
                "success": False,
                "error": "Unable to start tracking right now. Please try again shortly.",
            }), 503
        status_code = 403 if "Unauthorized" in err else 500 if "Database error" in err else 400
        return jsonify({"success": False, "error": err}), status_code

    # Build full shareable customer link
    customer_tracking_url = url_for(
        "live_tracking.customer_tracking_page",
        token=session_data["access_token"],
        _external=True,
    )
    session_data["customer_url"] = customer_tracking_url

    return jsonify({"success": True, "session": session_data})


@tracking_bp.route("/api/session/stop", methods=["POST"])
def api_stop_session():
    """
    API: Owner stops broadcasting live location.
    """
    owner_id = session.get("owner_id")
    if not owner_id:
        return jsonify({"success": False, "error": "Unauthorized. Owner login required."}), 403

    payload = request.get_json(silent=True) or request.form
    session_token = payload.get("session_token")
    session_id = payload.get("session_id")
    if not session_token and not session_id:
        return jsonify({"success": False, "error": "A tracking session identifier is required."}), 400

    try:
        success, message = stop_tracking_session(
            owner_id=owner_id,
            session_token=session_token,
            session_id=session_id,
        )
    except Error as exc:
        logger.error(
            "Tracking session stop database failure (owner_id=%s, errno=%s)",
            owner_id,
            getattr(exc, "errno", None),
        )
        return jsonify({
            "success": False,
            "error": "Unable to stop tracking right now. Please try again shortly.",
        }), 503

    if not success:
        if "Database error" in message:
            logger.error("Tracking session stop failed because of a database error (owner_id=%s)", owner_id)
            return jsonify({
                "success": False,
                "error": "Unable to stop tracking right now. Please try again shortly.",
            }), 503
        status_code = 403
        return jsonify({"success": False, "error": message}), status_code

    return jsonify({"success": True, "message": message})


@tracking_bp.route("/api/location/update", methods=["POST"])
def api_update_location():
    """
    API: Vehicle owner's browser transmits actual real-time GPS coordinates.
    Validates coordinates, checks session validity, and writes position to DB.
    """
    owner_id = session.get("owner_id")
    if not owner_id:
        return jsonify({"success": False, "error": "Forbidden. Owner login required."}), 403

    payload = request.get_json(silent=True) or request.form
    session_token = payload.get("session_token")
    lat = payload.get("latitude")
    lng = payload.get("longitude")
    accuracy = payload.get("accuracy")
    speed = payload.get("speed")
    heading = payload.get("heading")
    altitude = payload.get("altitude")

    if not session_token:
        return jsonify({"success": False, "error": "Missing tracking session token."}), 400

    try:
        success, result = record_live_location(
            owner_id=owner_id,
            session_token=session_token,
            lat=lat,
            lng=lng,
            accuracy=accuracy,
            speed=speed,
            heading=heading,
            altitude=altitude,
        )
    except Error as exc:
        logger.error(
            "GPS location database failure (owner_id=%s, errno=%s)",
            owner_id,
            getattr(exc, "errno", None),
        )
        return jsonify({
            "success": False,
            "error": "Unable to save GPS coordinates right now. Please retry shortly.",
        }), 503

    if not success:
        status_code = 403 if "Unauthorized" in result else 400
        if "Database error" in result:
            logger.error("Database rejected a live location update for owner_id=%s", owner_id)
            result = "Unable to save GPS coordinates right now. Please retry shortly."
            status_code = 503
        return jsonify({"success": False, "error": result}), status_code

    return jsonify({"success": True, "data": result})


@tracking_bp.route("/api/location/poll", methods=["GET"])
def api_poll_location():
    """
    API: Customer browser polls latest vehicle location every 5-10 seconds.
    Supports polling via access_token or booking_id (with authorization).
    """
    token = request.args.get("token", "").strip()
    booking_id = request.args.get("booking_id", "").strip()

    try:
        if token:
            data, err = get_live_tracking_data_by_token(token)
            if err:
                return jsonify({"success": False, "error": err}), 404
            return jsonify({"success": True, "data": data})

        if booking_id and booking_id.isdigit():
            customer_phone = session.get("user_phone") if session.get("user_id") else None
            customer_name = session.get("user_name")
            owner_id = session.get("owner_id")

            data, err = get_live_tracking_data_by_booking(
                booking_id=int(booking_id),
                customer_phone=customer_phone,
                customer_name=customer_name,
                owner_id=owner_id,
            )
            if err:
                status_code = 403 if "Unauthorized" in err else 404
                return jsonify({"success": False, "error": err}), status_code
            return jsonify({"success": True, "data": data})
    except Error as exc:
        logger.error(
            "Tracking location poll database failure (booking_id=%s, errno=%s)",
            booking_id or None,
            getattr(exc, "errno", None),
        )
        return jsonify({
            "success": False,
            "error": "Tracking data is temporarily unavailable.",
        }), 503
    except Exception:
        logger.exception(
            "Unexpected tracking location poll failure (booking_id=%s)",
            booking_id or None,
        )
        return jsonify({
            "success": False,
            "error": "Tracking data is temporarily unavailable.",
        }), 503

    return jsonify({"success": False, "error": "Either token or booking_id is required."}), 400


@tracking_bp.route("/api/route", methods=["GET"])
def api_get_route():
    """
    API: Returns actual driving road route from vehicle GPS position to destination using OSRM.
    Query params:
      start_lat (float), start_lng (float): driver's current position
      dest_lat (float), dest_lng (float): destination coordinates (optional if destination name given)
      destination (str): destination place name (optional if dest_lat/dest_lng given)
    """
    start_lat = request.args.get("start_lat")
    start_lng = request.args.get("start_lng")

    if not start_lat or not start_lng:
        return jsonify({"success": False, "error": "Start coordinates (start_lat, start_lng) are required."}), 400

    dest_lat = request.args.get("dest_lat")
    dest_lng = request.args.get("dest_lng")
    destination = request.args.get("destination")

    if dest_lat and dest_lng:
        d_lat = dest_lat
        d_lng = dest_lng
    elif destination:
        coords = geocode_location(destination)
        if not coords:
            return jsonify({"success": False, "error": f"Could not geocode destination '{destination}'."}), 400
        d_lat, d_lng = coords[0], coords[1]
    else:
        return jsonify({"success": False, "error": "Destination coordinates (dest_lat, dest_lng) or destination name required."}), 400

    try:
        success, result = get_road_route(start_lat, start_lng, d_lat, d_lng)
    except Exception:
        logger.exception("Unexpected road routing failure")
        return jsonify({
            "success": False,
            "error": "Route distance temporarily unavailable.",
        }), 502
    if not success:
        return jsonify({"success": False, "error": result}), 502

    return jsonify({"success": True, "route": result})


@tracking_bp.route("/api/owner/status", methods=["GET"])
def api_owner_status():
    """
    API: Retrieve owner's active tracking session status and vehicles.
    """
    owner_id = session.get("owner_id")
    if not owner_id:
        return jsonify({"success": False, "error": "Not logged in"}), 403

    try:
        options = get_owner_tracking_options(owner_id)
        return jsonify({"success": True, "data": options})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@tracking_bp.route("/api/status", methods=["GET"])
def api_health():
    """
    Health check diagnostic for the tracking module.
    """
    try:
        cnx = get_db_connection()
        cur = cnx.cursor()
        cur.execute("SELECT COUNT(*) FROM vehicle_tracking_sessions")
        sessions_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM vehicle_live_locations")
        locations_count = cur.fetchone()[0]
        cur.close()
        cnx.close()
        return jsonify({
            "status": "healthy",
            "feature": "Live Vehicle Location Tracking",
            "sessions_count": sessions_count,
            "locations_count": locations_count,
        })
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500
