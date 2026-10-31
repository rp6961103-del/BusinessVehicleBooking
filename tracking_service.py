"""
Tracking Service Module for Live Vehicle Location Tracking.
Handles database queries, coordinate validation, authorization, and session lifecycle.
Completely isolated; does not modify any existing tables or files.
"""

import os
import re
import math
import secrets
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
import requests
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("tracking_service")


_GEOCODE_CACHE = {}
_GEOCODE_CACHE_POSITIVE_TTL = 86400
_GEOCODE_CACHE_NEGATIVE_TTL = 60
_GEOCODE_PLACE_ALIASES = {
    "anatapur": "Anantapur",
    "banglore": "Bangalore",
}
_CITY_RESULT_TYPES = {
    "administrative",
    "borough",
    "city",
    "county",
    "hamlet",
    "locality",
    "municipality",
    "neighbourhood",
    "quarter",
    "suburb",
    "town",
    "village",
}


def format_distance(meters):
    """Format meters into human readable distance (km or m)."""
    if meters is None:
        return "--"
    try:
        m = float(meters)
        if m < 1000:
            return f"{int(round(m))} m"
        return f"{m / 1000.0:.1f} km"
    except (ValueError, TypeError):
        return "--"


def format_duration(seconds):
    """Format seconds into human readable duration (mins or hr mins)."""
    if seconds is None:
        return "--"
    try:
        s = float(seconds)
        if s < 60:
            return "< 1 min"
        mins = int(round(s / 60.0))
        if mins < 60:
            return f"{mins} mins"
        hrs = mins // 60
        rem_mins = mins % 60
        if rem_mins == 0:
            return f"{hrs} hr" if hrs == 1 else f"{hrs} hrs"
        return f"{hrs} hr {rem_mins} mins"
    except (ValueError, TypeError):
        return "--"


def _canonicalize_known_place_spelling(place_name):
    if not place_name:
        return place_name
    clean_name = str(place_name).strip()
    lookup_name = re.sub(r",?\s*india\s*$", "", clean_name, flags=re.IGNORECASE).strip()
    return _GEOCODE_PLACE_ALIASES.get(lookup_name.casefold(), clean_name)


def geocode_location(place_name):
    """
    Resolve a place name or string to (latitude, longitude) tuple.
    Returns (lat, lng) as floats, or None if unresolved.
    """
    if not place_name:
        return None

    clean_name = str(place_name).strip()
    if not clean_name:
        return None

    # Check if place_name is already a coordinate pair "lat,lng"
    coord_match = re.match(
        r"^[-+]?([1-8]?\d(\.\d+)?|90(\.0+)?)[,\s]+[-+]?(180(\.0+)?|((1[0-7]\d)|([1-9]?\d))(\.\d+)?)$",
        clean_name,
    )
    if coord_match:
        parts = [p.strip() for p in clean_name.replace(" ", ",").split(",") if p.strip()]
        if len(parts) >= 2:
            try:
                lat = float(parts[0])
                lng = float(parts[1])
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    return (lat, lng)
            except ValueError:
                pass

    place_name_without_country = re.sub(r",?\s*india\s*$", "", clean_name, flags=re.IGNORECASE).strip()
    lookup_name = _GEOCODE_PLACE_ALIASES.get(
        place_name_without_country.casefold(),
        place_name_without_country,
    )
    query = f"{lookup_name}, India"
    lookup_key = query.lower()
    cached = _GEOCODE_CACHE.get(lookup_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    is_simple_place = len(re.findall(r"[^\W_]+", place_name_without_country)) <= 2
    try:
        url = "https://nominatim.openstreetmap.org/search"
        resp = requests.get(
            url,
            params={
                "format": "json",
                "q": query,
                "countrycodes": "in",
                "addressdetails": 1,
                "limit": 5,
            },
            headers={
                "User-Agent": "BusinessVehicleBooking/2.0",
                "Accept-Language": "en",
            },
            timeout=5.0,
        )
        if resp.status_code == 200:
            items = resp.json()
            for item in items if isinstance(items, list) else []:
                address = item.get("address") or {}
                if str(address.get("country_code", "")).lower() != "in":
                    continue
                if is_simple_place and item.get("type") not in _CITY_RESULT_TYPES:
                    continue
                if is_simple_place:
                    result_name = f"{item.get('name', '')} {item.get('display_name', '')}".casefold()
                    expected_tokens = re.findall(r"[^\W_]+", lookup_name.casefold())
                    if any(token not in result_name for token in expected_tokens):
                        continue

                try:
                    lat = float(item["lat"])
                    lng = float(item["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180:
                    _GEOCODE_CACHE[lookup_key] = (
                        time.monotonic() + _GEOCODE_CACHE_POSITIVE_TTL,
                        (lat, lng),
                    )
                    return (lat, lng)
            logger.warning(
                "Nominatim returned no confident India place result "
                "(query_length=%s)",
                len(query),
            )
        else:
            logger.warning(
                "Nominatim geocoding returned HTTP status %s",
                resp.status_code,
            )
    except Exception as exc:
        logger.warning(
            "Nominatim geocoding failed for destination lookup (error_type=%s)",
            type(exc).__name__,
        )

    _GEOCODE_CACHE[lookup_key] = (
        time.monotonic() + _GEOCODE_CACHE_NEGATIVE_TTL,
        None,
    )
    return None


def get_road_route(origin_lat, origin_lng, dest_lat, dest_lng, timeout=5.0):
    """
    Calculate actual driving road route between origin and destination coordinates using OSRM.
    Returns (True, result_dict) or (False, error_message).
    result_dict contains:
      - coordinates: list of [lat, lng] waypoints for Leaflet polyline
      - distance_meters: float
      - duration_seconds: float
      - distance_text: e.g. '14.2 km'
      - duration_text: e.g. '22 mins'
    """
    valid_orig, orig_res = validate_coordinates(origin_lat, origin_lng)
    if not valid_orig:
        return False, f"Invalid start coordinates: {orig_res}"

    valid_dest, dest_res = validate_coordinates(dest_lat, dest_lng)
    if not valid_dest:
        return False, f"Invalid destination coordinates: {dest_res}"

    o_lat = orig_res["latitude"]
    o_lng = orig_res["longitude"]
    d_lat = dest_res["latitude"]
    d_lng = dest_res["longitude"]

    # OSRM coordinate order: {lon},{lat};{lon},{lat}
    url = f"https://router.project-osrm.org/route/v1/driving/{o_lng:.6f},{o_lat:.6f};{d_lng:.6f},{d_lat:.6f}?overview=full&geometries=geojson"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "BusinessVehicleBooking/2.0"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning("OSRM routing returned HTTP status %s", resp.status_code)
            return False, f"OSRM routing service responded with HTTP status {resp.status_code}."

        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            reason = data.get("message") or data.get("code") or "No route returned"
            logger.warning("OSRM did not return a road route (reason=%s)", reason)
            return False, data.get("message") or "No driving road route found between these locations."

        route = data["routes"][0]
        distance_m = float(route.get("distance", 0.0))
        duration_s = float(route.get("duration", 0.0))
        raw_coords = route.get("geometry", {}).get("coordinates", [])

        # Convert [lng, lat] to [lat, lng] for Leaflet
        leaflet_coords = [[pt[1], pt[0]] for pt in raw_coords if len(pt) >= 2]
        if (
            len(leaflet_coords) < 2
            or not math.isfinite(distance_m)
            or distance_m < 0
            or not math.isfinite(duration_s)
            or duration_s < 0
            or any(
                not math.isfinite(point[0])
                or not math.isfinite(point[1])
                or not -90 <= point[0] <= 90
                or not -180 <= point[1] <= 180
                for point in leaflet_coords
            )
        ):
            logger.warning("OSRM returned invalid route geometry or metrics")
            return False, "Routing service returned invalid route data."

        return True, {
            "code": "Ok",
            "coordinates": leaflet_coords,
            "waypoints_count": len(leaflet_coords),
            "distance_meters": distance_m,
            "duration_seconds": duration_s,
            "distance_text": format_distance(distance_m),
            "duration_text": format_duration(duration_s),
            "origin": [o_lat, o_lng],
            "destination": [d_lat, d_lng],
        }
    except requests.Timeout:
        logger.warning("OSRM request timed out after %s seconds", timeout)
        return False, "Routing service request timed out."
    except requests.RequestException as exc:
        logger.warning("OSRM routing service network error: %s", exc)
        return False, f"Routing service network error: {exc}"
    except Exception as exc:
        logger.exception("Unexpected error in get_road_route")
        return False, f"Routing calculation error: {exc}"



def get_db_connection():
    """Create a new MySQL database connection using existing environment variables."""
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST") or "localhost",
        port=int(os.getenv("MYSQL_PORT") or "3306"),
        user=os.getenv("MYSQL_USER") or "root",
        password=os.getenv("MYSQL_PASSWORD") or "",
        database=os.getenv("MYSQL_DATABASE") or "vehicle_booking",
    )


def validate_coordinates(lat, lng, accuracy=None, speed=None, heading=None, altitude=None):
    """
    Validate GPS coordinates and telemetry.
    Returns (True, dict_of_values) if valid, or (False, error_message).
    """
    try:
        if lat is None or lng is None:
            return False, "Latitude and longitude are required."

        lat_f = float(lat)
        lng_f = float(lng)

        if math.isnan(lat_f) or math.isinf(lat_f) or not (-90.0 <= lat_f <= 90.0):
            return False, "Latitude must be a valid number between -90 and 90 degrees."

        if math.isnan(lng_f) or math.isinf(lng_f) or not (-180.0 <= lng_f <= 180.0):
            return False, "Longitude must be a valid number between -180 and 180 degrees."

        acc_f = None
        if accuracy is not None and accuracy != "":
            if isinstance(accuracy, bool):
                return False, "Accuracy must be a finite, non-negative number."
            try:
                acc_f = float(accuracy)
            except (ValueError, TypeError, OverflowError):
                return False, "Accuracy must be a finite, non-negative number."
            if not math.isfinite(acc_f) or acc_f < 0:
                return False, "Accuracy must be a finite, non-negative number."
            if acc_f > 999999.99:
                return False, "Accuracy exceeds the maximum storable value of 999999.99 meters."

        spd_f = None
        if speed is not None and speed != "":
            spd_f = float(speed)
            if math.isnan(spd_f) or spd_f < 0 or spd_f > 500:
                return False, "Speed must be a realistic non-negative number."

        hdg_f = None
        if heading is not None and heading != "":
            hdg_f = float(heading)
            if math.isnan(hdg_f) or not (0.0 <= hdg_f <= 360.0):
                return False, "Heading must be between 0 and 360 degrees."

        alt_f = None
        if altitude is not None and altitude != "":
            alt_f = float(altitude)
            if math.isnan(alt_f) or not (-1000.0 <= alt_f <= 15000.0):
                return False, "Altitude must be within realistic bounds."

        return True, {
            "latitude": lat_f,
            "longitude": lng_f,
            "accuracy": acc_f,
            "speed": spd_f,
            "heading": hdg_f,
            "altitude": alt_f,
        }
    except (ValueError, TypeError, OverflowError) as err:
        return False, f"Invalid numeric format for coordinates: {err}"


def get_owner_tracking_options(owner_id):
    """
    Retrieve owner's active vehicles and any bookings eligible for live tracking.
    """
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    try:
        # 1. Vehicles owned by this owner
        cur.execute(
            """
            SELECT id, vehicle_name, vehicle_type, contact_number, location, status, is_active
            FROM vehicles
            WHERE owner_id = %s AND is_active = TRUE
            ORDER BY id ASC
            """,
            (owner_id,),
        )
        vehicles = cur.fetchall()

        # 2. Bookings for these vehicles
        cur.execute(
            """
            SELECT b.id AS booking_id, b.vehicle_id, b.customer_name, b.phone, b.booking_date, b.status,
                   v.vehicle_name, v.vehicle_type,
                   vr.from_location, vr.to_location
            FROM bookings b
            JOIN vehicles v ON b.vehicle_id = v.id
            LEFT JOIN vehicle_routes vr ON b.route_id = vr.id
            WHERE v.owner_id = %s AND b.status IN ('Accepted', 'Confirmed')
            ORDER BY b.booking_date DESC, b.id DESC
            LIMIT 20
            """,
            (owner_id,),
        )
        bookings = cur.fetchall()

        # 3. Check if owner already has an active session
        cur.execute(
            """
            SELECT s.id, s.vehicle_id, s.booking_id, s.registration_number, s.session_token,
                   s.access_token, s.status, s.started_at,
                   v.vehicle_name, v.vehicle_type
            FROM vehicle_tracking_sessions s
            JOIN vehicles v ON s.vehicle_id = v.id
            WHERE s.owner_id = %s AND s.status = 'ACTIVE'
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (owner_id,),
        )
        active_session = cur.fetchone()

        return {
            "vehicles": vehicles,
            "bookings": bookings,
            "active_session": active_session,
        }
    finally:
        cur.close()
        cnx.close()


def start_tracking_session(owner_id, vehicle_id, booking_id=None, registration_number=None):
    """
    Start a live tracking session for a vehicle owned by owner_id.
    Generates secure session_token for the driver and access_token for customers.
    """
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    try:
        # Validate that owner owns this vehicle
        cur.execute(
            "SELECT id, vehicle_name, vehicle_type, contact_number, location FROM vehicles WHERE id = %s AND owner_id = %s AND is_active = TRUE",
            (vehicle_id, owner_id),
        )
        vehicle = cur.fetchone()
        if not vehicle:
            return None, "Unauthorized: You do not own this vehicle or it is inactive."

        # If booking_id is provided, verify it belongs to this vehicle
        booking = None
        if booking_id:
            cur.execute(
                """
                SELECT b.id, b.customer_name, b.phone, b.booking_date, b.status,
                       vr.from_location, vr.to_location
                FROM bookings b
                LEFT JOIN vehicle_routes vr ON b.route_id = vr.id
                WHERE b.id = %s AND b.vehicle_id = %s
                  AND b.status IN ('Accepted', 'Confirmed')
                """,
                (booking_id, vehicle_id),
            )
            booking = cur.fetchone()
            if not booking:
                return None, "Unauthorized: selected booking is unavailable for this vehicle."

        # Mark any previous active sessions for this vehicle or owner as STOPPED
        cur.execute(
            """
            UPDATE vehicle_tracking_sessions
            SET status = 'STOPPED', stopped_at = CURRENT_TIMESTAMP
            WHERE vehicle_id = %s AND status = 'ACTIVE'
            """,
            (vehicle_id,),
        )

        session_token = secrets.token_urlsafe(32)
        access_token = secrets.token_urlsafe(24)
        clean_reg = (registration_number or "").strip()[:50] or None

        cur.execute(
            """
            INSERT INTO vehicle_tracking_sessions
            (owner_id, vehicle_id, booking_id, registration_number, session_token, access_token, status, started_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'ACTIVE', CURRENT_TIMESTAMP)
            """,
            (owner_id, vehicle_id, booking_id or None, clean_reg, session_token, access_token),
        )
        session_id = cur.lastrowid
        cnx.commit()

        return {
            "session_id": session_id,
            "session_token": session_token,
            "access_token": access_token,
            "vehicle_id": vehicle_id,
            "vehicle_name": vehicle["vehicle_name"],
            "vehicle_type": vehicle["vehicle_type"],
            "registration_number": clean_reg or "Not Specified",
            "booking_id": booking_id,
            "status": "ACTIVE",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }, None
    except Exception as exc:
        cnx.rollback()
        logger.exception("Failed to start tracking session")
        return None, f"Database error: {exc}"
    finally:
        cur.close()
        cnx.close()


def stop_tracking_session(owner_id, session_token=None, session_id=None):
    """
    Stop an active tracking session.
    """
    if not session_token and not session_id:
        return False, "A tracking session identifier is required."

    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    try:
        query = (
            """
            SELECT s.id, s.vehicle_id, s.session_token
            FROM vehicle_tracking_sessions s
            JOIN vehicles v ON v.id = s.vehicle_id AND v.owner_id = s.owner_id
            LEFT JOIN bookings b ON b.id = s.booking_id AND b.vehicle_id = s.vehicle_id
            WHERE s.owner_id = %s AND s.status = 'ACTIVE'
              AND (s.booking_id IS NULL OR b.id IS NOT NULL)
            """
        )
        params = [owner_id]
        if session_token:
            query += "AND s.session_token = %s"
            params.append(session_token)
        elif session_id:
            query += "AND s.id = %s"
            params.append(session_id)

        cur.execute(query, tuple(params))
        session_row = cur.fetchone()
        if not session_row:
            return False, "Active tracking session not found or already stopped."

        # Mark session stopped
        cur.execute(
            """
            UPDATE vehicle_tracking_sessions
            SET status = 'STOPPED', stopped_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (session_row["id"],),
        )

        # Update live location status to STOPPED
        cur.execute(
            """
            UPDATE vehicle_live_locations
            SET status = 'STOPPED'
            WHERE vehicle_id = %s AND session_id = %s
            """,
            (session_row["vehicle_id"], session_row["id"]),
        )
        cnx.commit()
        return True, "Tracking session stopped successfully."
    except Exception as exc:
        cnx.rollback()
        logger.exception("Failed to stop tracking session")
        return False, f"Database error: {exc}"
    finally:
        cur.close()
        cnx.close()


def record_live_location(owner_id, session_token, lat, lng, accuracy=None, speed=None, heading=None, altitude=None):
    """
    Record real GPS location update from vehicle owner browser.
    Ensures session is valid and active before writing to DB.
    """
    valid, validated_data = validate_coordinates(lat, lng, accuracy, speed, heading, altitude)
    if not valid:
        return False, validated_data

    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT s.id, s.owner_id, s.vehicle_id, s.booking_id, s.status
            FROM vehicle_tracking_sessions s
            JOIN vehicles v
                ON v.id = s.vehicle_id AND v.owner_id = s.owner_id AND v.is_active = TRUE
            LEFT JOIN bookings b ON b.id = s.booking_id AND b.vehicle_id = s.vehicle_id
            WHERE s.session_token = %s AND s.owner_id = %s
              AND (s.booking_id IS NULL OR b.id IS NOT NULL)
              AND (s.booking_id IS NULL OR b.status IN ('Accepted', 'Confirmed'))
            """,
            (session_token, owner_id),
        )
        session_row = cur.fetchone()
        if not session_row:
            return False, "Unauthorized: tracking session does not belong to this owner."
        if session_row["status"] != "ACTIVE":
            return False, "Tracking session is not active."

        session_id = session_row["id"]
        vehicle_id = session_row["vehicle_id"]

        # Insert or update current live location
        cur.execute(
            """
            INSERT INTO vehicle_live_locations
            (session_id, vehicle_id, latitude, longitude, accuracy, heading, speed, altitude, status, recorded_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                session_id = VALUES(session_id),
                latitude = VALUES(latitude),
                longitude = VALUES(longitude),
                accuracy = VALUES(accuracy),
                heading = VALUES(heading),
                speed = VALUES(speed),
                altitude = VALUES(altitude),
                status = 'ACTIVE',
                recorded_at = CURRENT_TIMESTAMP
            """,
            (
                session_id,
                vehicle_id,
                validated_data["latitude"],
                validated_data["longitude"],
                validated_data["accuracy"],
                validated_data["heading"],
                validated_data["speed"],
                validated_data["altitude"],
            ),
        )

        # Append to breadcrumb history table
        cur.execute(
            """
            INSERT INTO vehicle_location_history
            (session_id, vehicle_id, latitude, longitude, accuracy, heading, speed, recorded_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """,
            (
                session_id,
                vehicle_id,
                validated_data["latitude"],
                validated_data["longitude"],
                validated_data["accuracy"],
                validated_data["heading"],
                validated_data["speed"],
            ),
        )

        cnx.commit()
        return True, {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "latitude": validated_data["latitude"],
            "longitude": validated_data["longitude"],
            "accuracy": validated_data["accuracy"],
        }
    except Exception as exc:
        cnx.rollback()
        logger.exception("Error saving live GPS location")
        return False, f"Database error: {exc}"
    finally:
        cur.close()
        cnx.close()


def get_live_tracking_data_by_token(access_token):
    """
    Fetch live location and trip information for a customer viewing via secure access_token.
    """
    if not access_token:
        return None, "Tracking token is required."

    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT s.id AS session_id, s.owner_id, s.vehicle_id, s.booking_id, s.registration_number,
                   s.status AS session_status, s.started_at, s.stopped_at,
                   v.vehicle_name, v.vehicle_type, v.contact_number, v.location AS base_location,
                   o.owner_name,
                   b.customer_name, b.booking_date, b.status AS booking_status,
                   vr.from_location, vr.to_location, vr.rent,
                   loc.latitude, loc.longitude, loc.accuracy, loc.heading, loc.speed,
                   loc.status AS location_status, loc.recorded_at
            FROM vehicle_tracking_sessions s
            JOIN vehicles v ON s.vehicle_id = v.id
            JOIN owners o ON s.owner_id = o.id
            LEFT JOIN bookings b ON s.booking_id = b.id
            LEFT JOIN vehicle_routes vr ON b.route_id = vr.id
            LEFT JOIN vehicle_live_locations loc
                ON loc.vehicle_id = s.vehicle_id AND loc.session_id = s.id
            WHERE s.access_token = %s
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (access_token,),
        )
        row = cur.fetchone()
        if not row:
            return None, "Invalid or expired tracking link."

        return _build_tracking_payload(row), None
    finally:
        cur.close()
        cnx.close()


def get_live_tracking_data_by_booking(booking_id, customer_phone=None, customer_name=None, owner_id=None):
    """
    Fetch live location for an authorized customer or owner accessing by booking_id.
    """
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    try:
        # Check booking existence and authorization
        cur.execute(
            """
            SELECT b.id AS booking_id, b.vehicle_id, b.customer_name, b.phone, b.booking_date, b.status AS booking_status,
                   v.owner_id, v.vehicle_name, v.vehicle_type, v.contact_number, v.location AS base_location,
                   o.owner_name,
                   vr.from_location, vr.to_location, vr.rent
            FROM bookings b
            JOIN vehicles v ON b.vehicle_id = v.id
            JOIN owners o ON v.owner_id = o.id
            LEFT JOIN vehicle_routes vr ON b.route_id = vr.id
            WHERE b.id = %s
            """,
            (booking_id,),
        )
        booking = cur.fetchone()
        if not booking:
            return None, "Booking not found."

        # Authorization: Must be owner or the customer with matching phone
        is_owner = (owner_id is not None and int(owner_id) == booking["owner_id"])
        is_customer = (
            customer_phone is not None and str(customer_phone).strip() == str(booking["phone"]).strip()
        )

        if not (is_owner or is_customer):
            return None, "Unauthorized: You do not have permission to view tracking for this booking."

        # Find latest tracking session for this booking or vehicle
        cur.execute(
            """
            SELECT s.id AS session_id, s.owner_id, s.vehicle_id, s.booking_id, s.registration_number,
                   s.status AS session_status, s.started_at, s.stopped_at,
                   loc.latitude, loc.longitude, loc.accuracy, loc.heading, loc.speed,
                   loc.status AS location_status, loc.recorded_at
            FROM vehicle_tracking_sessions s
            LEFT JOIN vehicle_live_locations loc
                ON loc.vehicle_id = s.vehicle_id AND loc.session_id = s.id
            WHERE s.booking_id = %s AND s.vehicle_id = %s
            ORDER BY (s.status = 'ACTIVE') DESC, s.id DESC
            LIMIT 1
            """,
            (booking_id, booking["vehicle_id"]),
        )
        session_row = cur.fetchone()

        merged = dict(booking)
        if session_row:
            merged.update(session_row)
        else:
            merged.update({
                "session_id": None,
                "access_token": None,
                "session_status": "NONE",
                "latitude": None,
                "longitude": None,
                "accuracy": None,
                "speed": None,
                "heading": None,
                "recorded_at": None,
            })

        return _build_tracking_payload(merged), None
    finally:
        cur.close()
        cnx.close()


def _build_tracking_payload(row):
    """
    Format database row into a structured tracking dictionary with status calculations.
    """
    recorded_at = row.get("recorded_at")
    seconds_ago = None
    is_stale = True
    tracking_state = "UNAVAILABLE"

    if recorded_at:
        # Calculate seconds elapsed
        if isinstance(recorded_at, datetime):
            now = datetime.now()
            # Handle naive vs aware
            diff = (now - recorded_at.replace(tzinfo=None)).total_seconds()
            seconds_ago = max(0, int(diff))
        else:
            seconds_ago = 9999

        # Stale threshold: 60 seconds
        is_stale = (seconds_ago > 60)

    session_status = row.get("session_status") or "INACTIVE"
    has_coords = (row.get("latitude") is not None and row.get("longitude") is not None)

    if session_status == "STOPPED":
        tracking_state = "STOPPED"
    elif session_status == "ACTIVE":
        if not has_coords:
            tracking_state = "WAITING_FOR_GPS"
        elif is_stale:
            tracking_state = "OUTDATED"
        else:
            tracking_state = "LIVE"
    else:
        tracking_state = "NOT_STARTED"

    # Route endpoints must come from the selected booking, never a vehicle's unrelated default route.
    destination = _canonicalize_known_place_spelling(row.get("to_location"))
    pickup = _canonicalize_known_place_spelling(row.get("from_location"))

    dest_coords = None
    if destination:
        dest_coords = geocode_location(destination)

    pickup_coords = None
    if pickup:
        pickup_coords = geocode_location(pickup)

    return {
        "authorized": True,
        "tracking_state": tracking_state,
        "is_stale": is_stale,
        "seconds_ago": seconds_ago,
        "last_updated": recorded_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(recorded_at, datetime) else None,
        "vehicle": {
            "id": row.get("vehicle_id"),
            "name": row.get("vehicle_name"),
            "type": row.get("vehicle_type"),
            "registration_number": row.get("registration_number"),
            "contact_number": row.get("contact_number"),
            "base_location": row.get("base_location"),
            "owner_name": row.get("owner_name"),
        },
        "booking": {
            "id": row.get("booking_id"),
            "customer_name": row.get("customer_name"),
            "customer_phone": row.get("phone"),
            "booking_date": str(row.get("booking_date")) if row.get("booking_date") else None,
            "status": row.get("booking_status"),
            "rent": str(row.get("rent") or "0"),
            "pickup": pickup,
            "destination": destination,
            "pickup_coords": [pickup_coords[0], pickup_coords[1]] if pickup_coords else None,
            "destination_coords": [dest_coords[0], dest_coords[1]] if dest_coords else None,
        },
        "location": {
            "latitude": float(row["latitude"]) if row.get("latitude") is not None else None,
            "longitude": float(row["longitude"]) if row.get("longitude") is not None else None,
            "accuracy": float(row["accuracy"]) if row.get("accuracy") is not None else None,
            "speed": float(row["speed"]) if row.get("speed") is not None else None,
            "heading": float(row["heading"]) if row.get("heading") is not None else None,
        } if has_coords else None,
    }
