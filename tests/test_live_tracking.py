"""
Automated Test Suite for Live Vehicle Location Tracking.
Tests coordinate validation, session management, authorization,
OSRM road routing, destination resolution, freshness detection,
and API endpoints without modifying any existing test or code files.
"""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import pytest
import requests

from tracking_service import (
    validate_coordinates,
    format_distance,
    format_duration,
    geocode_location,
    get_road_route,
    start_tracking_session,
    stop_tracking_session,
    record_live_location,
    get_live_tracking_data_by_token,
    get_live_tracking_data_by_booking,
    _GEOCODE_CACHE,
    _build_tracking_payload,
)
from run_live_tracking import app


# --------------------------------------------------------------------------
# 1. Coordinate Validation & Formatting Tests
# --------------------------------------------------------------------------

def test_validate_coordinates_valid():
    """Valid latitude and longitude should succeed."""
    valid, res = validate_coordinates(14.6819, 77.6006, accuracy=12.5, speed=10.2, heading=180.0)
    assert valid is True
    assert res["latitude"] == 14.6819
    assert res["longitude"] == 77.6006
    assert res["accuracy"] == 12.5
    assert res["speed"] == 10.2
    assert res["heading"] == 180.0


def test_validate_coordinates_out_of_bounds():
    """Latitude > 90 or < -90, Longitude > 180 or < -180 should fail."""
    valid, err = validate_coordinates(95.0, 77.0)
    assert valid is False
    assert "Latitude must be a valid number between -90 and 90" in err

    valid, err = validate_coordinates(-91.0, 77.0)
    assert valid is False

    valid, err = validate_coordinates(14.0, 185.0)
    assert valid is False
    assert "Longitude must be a valid number between -180 and 180" in err

    valid, err = validate_coordinates(14.0, -181.0)
    assert valid is False


def test_validate_coordinates_non_numeric():
    """Non-numeric or NaN coordinates must be rejected."""
    valid, err = validate_coordinates("invalid", "coordinates")
    assert valid is False
    assert "Invalid numeric format" in err

    valid, err = validate_coordinates(float("nan"), 77.0)
    assert valid is False

    valid, err = validate_coordinates(14.0, float("inf"))
    assert valid is False


def test_validate_coordinates_negative_accuracy():
    """Negative accuracy must be rejected."""
    valid, err = validate_coordinates(14.0, 77.0, accuracy=-5.0)
    assert valid is False
    assert "Accuracy must be a finite, non-negative number" in err


def test_validate_coordinates_accuracy_zero_and_missing():
    """Zero accuracy is valid, and omitted accuracy remains optional."""
    valid, values = validate_coordinates(14.0, 77.0, accuracy=0)
    assert valid is True
    assert values["accuracy"] == 0.0

    valid, values = validate_coordinates(14.0, 77.0)
    assert valid is True
    assert values["accuracy"] is None

    valid, values = validate_coordinates(14.0, 77.0, accuracy="")
    assert valid is True
    assert values["accuracy"] is None


def test_validate_coordinates_accepts_numeric_form_accuracy():
    """Numeric strings remain supported for form-encoded API clients."""
    valid, values = validate_coordinates(14.0, 77.0, accuracy="12.5")
    assert valid is True
    assert values["accuracy"] == 12.5


def test_validate_coordinates_accuracy_above_old_limit_fits_schema():
    """Finite accuracy is accepted through the DECIMAL(8,2) schema limit."""
    valid, values = validate_coordinates(14.0, 77.0, accuracy=100001)
    assert valid is True
    assert values["accuracy"] == 100001.0


def test_validate_coordinates_accepts_schema_accuracy_limit():
    valid, values = validate_coordinates(14.0, 77.0, accuracy=999999.99)
    assert valid is True
    assert values["accuracy"] == 999999.99


@pytest.mark.parametrize("accuracy", [-1, float("nan"), float("inf"), float("-inf"), True, "not-a-number"])
def test_validate_coordinates_rejects_invalid_accuracy(accuracy):
    """Invalid accuracy values must never be persisted as GPS measurements."""
    valid, error = validate_coordinates(14.0, 77.0, accuracy=accuracy)
    assert valid is False
    assert "Accuracy must be a finite, non-negative number" in error


def test_validate_coordinates_rejects_accuracy_above_schema_capacity():
    valid, error = validate_coordinates(14.0, 77.0, accuracy=1000000)
    assert valid is False
    assert "maximum storable value of 999999.99" in error


def test_validate_coordinates_rejects_values_that_overflow_float_conversion():
    too_large = 10 ** 10000

    valid, error = validate_coordinates(14.0, 77.0, accuracy=too_large)
    assert valid is False
    assert "Accuracy must be a finite, non-negative number" in error

    valid, error = validate_coordinates(too_large, 77.0)
    assert valid is False
    assert "Invalid numeric format" in error


def test_format_distance_and_duration():
    """Distance and duration formatters should return clean human-readable text."""
    assert format_distance(500) == "500 m"
    assert format_distance(12400) == "12.4 km"
    assert format_distance(None) == "--"

    assert format_duration(30) == "< 1 min"
    assert format_duration(1200) == "20 mins"
    assert format_duration(3600) == "1 hr"
    assert format_duration(5400) == "1 hr 30 mins"
    assert format_duration(None) == "--"


def test_geocode_location_uses_real_geocoding_and_direct_coordinates():
    """Place names resolve through Nominatim; explicit coordinate strings remain usable."""
    _GEOCODE_CACHE.clear()
    response = Mock(status_code=200)
    response.json.return_value = [{
        "lat": "14.68",
        "lon": "77.60",
        "type": "administrative",
        "display_name": "Test destination for geocoding, India",
        "address": {"country_code": "in"},
    }]
    with patch("tracking_service.requests.get", return_value=response) as geocode_request:
        ananthapur = geocode_location("test destination for geocoding")

    assert ananthapur == (14.68, 77.6)
    assert geocode_request.call_args.kwargs["params"]["q"] == "test destination for geocoding, India"
    assert geocode_request.call_args.kwargs["params"]["countrycodes"] == "in"
    assert geocode_request.call_args.kwargs["params"]["addressdetails"] == 1


def test_bangalore_geocoding_uses_india_context_without_fixed_coordinates():
    _GEOCODE_CACHE.clear()
    response = Mock(status_code=200)
    response.json.return_value = [{
        "lat": "12.9768",
        "lon": "77.5901",
        "type": "administrative",
        "name": "Bengaluru",
        "display_name": "Bengaluru, Bangalore, Karnataka, India",
        "address": {"country_code": "in"},
    }]
    with patch("tracking_service.requests.get", return_value=response) as geocode_request:
        coordinates = geocode_location("Bangalore")

    assert coordinates == (12.9768, 77.5901)
    assert geocode_request.call_args.kwargs["params"]["q"] == "Bangalore, India"

    # Direct "lat,lng" string format
    direct = geocode_location("14.5534, 77.1084")
    assert direct is not None
    assert direct[0] == 14.5534
    assert direct[1] == 77.1084

    assert geocode_location("") is None
    assert geocode_location(None) is None


def test_geocoder_ignores_unrelated_highway_result_for_simple_place_name():
    _GEOCODE_CACHE.clear()
    response = Mock(status_code=200)
    response.json.return_value = [
        {
            "lat": "13.0575814",
            "lon": "79.2670718",
            "type": "motorway",
            "display_name": "Chennai to Banglore Expressway, India",
            "address": {"country_code": "in"},
        },
        {
            "lat": "9.9261153",
            "lon": "78.1140983",
            "type": "administrative",
            "name": "Madurai",
            "display_name": "Madurai, Tamil Nadu, India",
            "address": {"country_code": "in", "city": "Madurai"},
        },
    ]
    with patch("tracking_service.requests.get", return_value=response):
        assert geocode_location("Madurai") == (9.9261153, 78.1140983)


def test_banglore_alias_queries_bangalore_and_ignores_unrelated_match():
    _GEOCODE_CACHE.clear()
    response = Mock(status_code=200)
    response.json.return_value = [{
        "lat": "12.9768",
        "lon": "77.5901",
        "type": "administrative",
        "name": "Bengaluru",
        "display_name": "Bengaluru, Bangalore, Karnataka, India",
        "address": {"country_code": "in", "city": "Bengaluru"},
    }]
    with patch("tracking_service.requests.get", return_value=response) as geocode_request:
        coordinates = geocode_location("banglore")

    assert coordinates == (12.9768, 77.5901)
    assert geocode_request.call_args.kwargs["params"]["q"] == "Bangalore, India"


def test_anatapur_alias_uses_the_matching_anantapur_place():
    _GEOCODE_CACHE.clear()
    response = Mock(status_code=200)
    response.json.return_value = [{
        "lat": "14.6783221",
        "lon": "77.6065039",
        "type": "city",
        "name": "Anantapur",
        "display_name": "Anantapur, Anantapuramu District, Andhra Pradesh, India",
        "address": {"country_code": "in"},
    }]
    with patch("tracking_service.requests.get", return_value=response) as geocode_request:
        coordinates = geocode_location("anatapur")

    assert coordinates == (14.6783221, 77.6065039)
    assert geocode_request.call_args.kwargs["params"]["q"] == "Anantapur, India"


def test_geocode_location_does_not_invent_coordinates_for_unknown_place():
    _GEOCODE_CACHE.clear()
    response = Mock(status_code=200)
    response.json.return_value = []
    with patch("tracking_service.requests.get", return_value=response):
        assert geocode_location("unknown destination location") is None


def test_payload_does_not_use_vehicle_default_route_for_booking_endpoints():
    with patch("tracking_service.geocode_location") as geocoder:
        payload = _build_tracking_payload({
            "booking_id": 26,
            "session_status": "NONE",
            "from_location": None,
            "to_location": None,
            "def_from_location": "Unrelated default pickup",
            "def_to_location": "Unrelated default destination",
            "base_location": "Vehicle base location",
        })

    assert payload["booking"]["pickup"] is None
    assert payload["booking"]["destination"] is None
    assert payload["booking"]["pickup_coords"] is None
    assert payload["booking"]["destination_coords"] is None
    geocoder.assert_not_called()


def test_payload_displays_known_city_spelling_corrections_without_changing_database_data():
    with patch(
        "tracking_service.geocode_location",
        side_effect=[(9.9261, 78.1141), (12.9768, 77.5901)],
    ):
        payload = _build_tracking_payload({
            "booking_id": 26,
            "session_status": "NONE",
            "from_location": "madurai",
            "to_location": "banglore",
        })

    assert payload["booking"]["pickup"] == "madurai"
    assert payload["booking"]["destination"] == "Bangalore"


# --------------------------------------------------------------------------
# 2. Road Route via OSRM & Routing Tests
# --------------------------------------------------------------------------

def test_get_road_route_valid():
    """Test calculating real road route from Kalyandurgam to Anantapur via OSRM."""
    success, res = get_road_route(14.5534, 77.1084, 14.6819, 77.6006)
    assert success is True
    assert res["code"] == "Ok"
    assert res["distance_meters"] > 0
    assert res["duration_seconds"] > 0
    assert "km" in res["distance_text"]
    assert len(res["coordinates"]) > 10
    # Coordinates must be [lat, lng] pairs
    assert len(res["coordinates"][0]) == 2
    assert -90 <= res["coordinates"][0][0] <= 90


def test_get_road_route_invalid_coordinates():
    """Invalid start or destination coordinates must fail gracefully."""
    success, err = get_road_route(999.0, 77.0, 14.0, 77.0)
    assert success is False
    assert "Invalid start coordinates" in err

    success, err = get_road_route(14.0, 77.0, 14.0, 999.0)
    assert success is False
    assert "Invalid destination coordinates" in err


def test_get_road_route_service_unavailable():
    """Graceful degradation when OSRM routing service is unreachable."""
    with patch("requests.get", side_effect=requests.RequestException("OSRM Connection refused")):
        success, err = get_road_route(14.5534, 77.1084, 14.6819, 77.6006)
        assert success is False
        assert "network error" in err.lower() or "connection refused" in err.lower()


# --------------------------------------------------------------------------
# 3. Tracking Service & Database Logic Tests
# --------------------------------------------------------------------------

def test_start_session_unauthorized_owner():
    """Attempting to track a vehicle not owned by the owner must fail."""
    session_data, err = start_tracking_session(owner_id=999, vehicle_id=1)
    assert session_data is None
    assert "Unauthorized" in err


def test_tracking_session_lifecycle():
    """Test start session, update location, get location, and stop session."""
    session_data, err = start_tracking_session(owner_id=1, vehicle_id=1, registration_number="AP02 AB 9999")
    assert err is None
    assert session_data is not None
    assert "session_token" in session_data
    assert "access_token" in session_data

    session_token = session_data["session_token"]
    access_token = session_data["access_token"]

    unauthorized_ok, unauthorized_result = record_live_location(
        owner_id=999,
        session_token=session_token,
        lat=14.6819,
        lng=77.6006,
    )
    assert unauthorized_ok is False
    assert "Unauthorized" in unauthorized_result

    # 1. Update live GPS position
    ok, loc_res = record_live_location(
        owner_id=1,
        session_token=session_token,
        lat=14.6819,
        lng=77.6006,
        accuracy=8.5,
        speed=12.0,
        heading=90.0,
    )
    assert ok is True
    assert loc_res["latitude"] == 14.6819
    assert loc_res["longitude"] == 77.6006

    # 2. Fetch live data via customer access_token
    cust_data, cust_err = get_live_tracking_data_by_token(access_token)
    assert cust_err is None
    assert cust_data["authorized"] is True
    assert cust_data["tracking_state"] == "LIVE"
    assert cust_data["location"]["latitude"] == 14.6819
    assert cust_data["location"]["longitude"] == 77.6006
    assert cust_data["vehicle"]["registration_number"] == "AP02 AB 9999"

    # 3. Stop session
    stop_ok, stop_msg = stop_tracking_session(owner_id=1, session_token=session_token)
    assert stop_ok is True

    # 4. Fetching after stop should show STOPPED status
    post_stop_data, _ = get_live_tracking_data_by_token(access_token)
    assert post_stop_data["tracking_state"] == "STOPPED"


def test_customer_marker_payload_update():
    """Customer tracking payload reflects coordinate changes when driver position moves."""
    session_data, err = start_tracking_session(owner_id=1, vehicle_id=1, registration_number="TEST-POS-UPDATE")
    assert err is None
    session_token = session_data["session_token"]
    access_token = session_data["access_token"]

    try:
        # First coordinate update (Pos 1)
        record_live_location(owner_id=1, session_token=session_token, lat=14.6500, lng=77.5500, accuracy=5.0)
        data1, _ = get_live_tracking_data_by_token(access_token)
        assert data1["location"]["latitude"] == 14.6500
        assert data1["location"]["longitude"] == 77.5500

        # Second coordinate update (Pos 2 - vehicle moved)
        record_live_location(owner_id=1, session_token=session_token, lat=14.6800, lng=77.6000, accuracy=4.0)
        data2, _ = get_live_tracking_data_by_token(access_token)
        assert data2["location"]["latitude"] == 14.6800
        assert data2["location"]["longitude"] == 77.6000
        assert data2["location"]["latitude"] != data1["location"]["latitude"]
    finally:
        stop_tracking_session(owner_id=1, session_token=session_token)


def test_booking_without_route_id_does_not_use_vehicle_default_route():
    """A missing booking route must not be replaced by an unrelated vehicle route."""
    row = {
        "recorded_at": datetime.now(),
        "session_status": "ACTIVE",
        "latitude": 14.68,
        "longitude": 77.60,
        "accuracy": 10.0,
        "speed": 0.0,
        "heading": 0.0,
        "vehicle_id": 1,
        "vehicle_name": "Test Van",
        "vehicle_type": "Van",
        "registration_number": "VH-001",
        "contact_number": "9999999999",
        "base_location": "ananthapuram",
        "owner_name": "Test Owner",
        "booking_id": 100,
        "customer_name": "John",
        "booking_date": "2026-10-20",
        "booking_status": "Accepted",
        "from_location": None,  # No explicit booking route
        "to_location": None,
        "def_from_location": "kalyandurgam",  # Vehicle default route
        "def_to_location": "ananthapuram",
        "access_token": "token123",
    }
    with patch("tracking_service.geocode_location") as geocoder:
        payload = _build_tracking_payload(row)
    assert payload["booking"]["destination"] is None
    assert payload["booking"]["pickup"] is None
    assert payload["booking"]["destination_coords"] is None
    assert payload["booking"]["pickup_coords"] is None
    geocoder.assert_not_called()
    assert "access_token" not in payload


def test_missing_destination_handling():
    """When no destination is specified on booking or vehicle, never invent a fake destination."""
    row = {
        "recorded_at": datetime.now(),
        "session_status": "ACTIVE",
        "latitude": 14.68,
        "longitude": 77.60,
        "vehicle_id": 1,
        "to_location": None,
        "def_to_location": None,
        "from_location": None,
        "def_from_location": None,
        "base_location": "Anantapur",
        "access_token": "token123",
    }
    payload = _build_tracking_payload(row)
    assert payload["booking"]["destination"] is None
    assert payload["booking"]["destination_coords"] is None


def test_tracking_payload_stale_detection():
    """Coordinates older than 60s must be marked OUTDATED and never claimed as LIVE."""
    stale_time = datetime.now() - timedelta(seconds=120)
    row = {
        "recorded_at": stale_time,
        "session_status": "ACTIVE",
        "latitude": 14.68,
        "longitude": 77.60,
        "vehicle_id": 1,
        "access_token": "token123",
    }
    payload = _build_tracking_payload(row)
    assert payload["is_stale"] is True
    assert payload["tracking_state"] == "OUTDATED"


def test_get_live_tracking_invalid_token():
    """Invalid access token must return error and not disclose coordinates."""
    data, err = get_live_tracking_data_by_token("non_existent_fake_token_12345")
    assert data is None
    assert "Invalid or expired tracking link" in err


def test_booking_authorization_check():
    """Unauthorized user should not view tracking by booking ID."""
    data, err = get_live_tracking_data_by_booking(
        booking_id=1,
        customer_phone="0000000000",
        owner_id=999,
    )
    assert data is None
    assert "Unauthorized" in err


# --------------------------------------------------------------------------
# 4. Flask Blueprint HTTP Route Tests
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_tracking_hub_route(client):
    """Tracking hub page loads successfully with 200 OK."""
    res = client.get("/live-tracking/")
    assert res.status_code == 200
    assert b"Live Vehicle Location Tracking" in res.data


def test_owner_tracking_page_unauthenticated(client):
    """Unauthenticated access to owner broadcaster shows login prompt."""
    res = client.get("/live-tracking/owner")
    assert res.status_code == 200
    assert b"Owner Login Required" in res.data


def test_customer_tracking_page_route(client):
    """Customer tracking page loads with 200 OK."""
    res = client.get("/live-tracking/customer")
    assert res.status_code == 200
    assert b"Live Vehicle Tracking" in res.data


def test_api_status_endpoint(client):
    """Diagnostic health check returns JSON status healthy."""
    res = client.get("/live-tracking/api/status")
    assert res.status_code == 200
    json_data = res.get_json()
    assert json_data["status"] == "healthy"
    assert "sessions_count" in json_data


def test_api_route_endpoint_success(client):
    """API endpoint /api/route returns 200 with road geometry, distance, and duration."""
    res = client.get(
        "/live-tracking/api/route?start_lat=14.5534&start_lng=77.1084&dest_lat=14.6819&dest_lng=77.6006"
    )
    assert res.status_code == 200
    json_data = res.get_json()
    assert json_data["success"] is True
    assert "route" in json_data
    assert "coordinates" in json_data["route"]
    assert "distance_text" in json_data["route"]
    assert "duration_text" in json_data["route"]


def test_api_route_endpoint_missing_params(client):
    """API endpoint /api/route rejects missing start or destination parameters."""
    res1 = client.get("/live-tracking/api/route")
    assert res1.status_code == 400
    assert res1.get_json()["success"] is False

    res2 = client.get("/live-tracking/api/route?start_lat=14.55&start_lng=77.10")
    assert res2.status_code == 400
    assert res2.get_json()["success"] is False


def test_api_route_endpoint_invalid_coordinates(client):
    """API endpoint /api/route rejects invalid non-numeric coordinates."""
    res = client.get("/live-tracking/api/route?start_lat=abc&start_lng=xyz&dest_lat=14.0&dest_lng=77.0")
    assert res.status_code == 502 or res.status_code == 400
    assert res.get_json()["success"] is False


def test_poll_endpoint_unauthorized_booking(client):
    """Polling a booking without matching phone or owner session returns 403."""
    res = client.get("/live-tracking/api/location/poll?booking_id=1")
    assert res.status_code == 403
    assert res.get_json()["success"] is False
