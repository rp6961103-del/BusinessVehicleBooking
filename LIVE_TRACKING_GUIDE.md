# 🛰️ Live Vehicle Location Tracking Guide

## Overview
A real-time GPS vehicle location tracking & Google Maps-style navigation experience built for **Business Vehicle Booking** using **Flask**, **MySQL**, **JavaScript (Geolocation API with `watchPosition`)**, **Leaflet.js**, **OpenStreetMap**, and **OSRM Road Routing Service**.

---

## 🌟 Key Capabilities (Google Maps-Style Experience)

1. **Interactive Live Map**:
   - Leaflet.js with standard OpenStreetMap tiles, English-oriented labels, and required OpenStreetMap attribution.
   - Real-time GPS coordinates captured from vehicle driver's mobile or desktop browser.
   - Smooth vehicle marker animation: marker glides smoothly between coordinate updates using `requestAnimationFrame` with quadratic easing curves.
   - Auto-centering & Auto-Follow mode: smoothly tracks vehicle position, with a dedicated toggle button (`Following (Click to Stop)` / `Follow Vehicle`) that automatically pauses when the user drags the map and resumes on click.
   - Distinct, labeled origin, destination, and live vehicle markers.

2. **Actual Road Route via OSRM**:
   - Uses only the selected booking's saved `vehicle_routes` endpoints; it does not substitute a vehicle's unrelated default route.
   - Draws the booked pickup-to-destination driving route in blue (`#1a73e8`) and displays the vehicle GPS marker separately.
   - When GPS is live, draws a dashed remaining road route and refreshes it after meaningful movement (>= 1 km).
   - Displays road distance and estimated travel time from valid OSRM responses; errors never fall back to straight lines or invented values.
   - Graceful degradation: if routing service is temporarily unavailable, preserves live vehicle position without drawing fake lines.
   - Never invents fake destinations when no destination is specified.

3. **Real-time Accuracy & Integrity**:
   - Reuses existing tracking tables and binds each location to its exact tracking session and booking.
   - Clear status indicators: `LIVE • ON TRIP` (green pulsing badge), `LOCATION OUTDATED` (amber stale indicator), `WAITING FOR GPS`, `CONNECTION LOST` (auto-reconnect), and `TRIP ENDED / STOPPED`.
   - Driver status changes to broadcasting only after a GPS fix with valid latitude, longitude, and finite non-negative accuracy is accepted by the location API; updates are sent at most every five seconds.
   - GPS denial, unavailable fixes, timeout, insecure HTTP, and API/network failures are reported on the broadcaster page.
   - GPS accuracy is forwarded only when finite and non-negative; an unavailable reading is stored as `NULL`, and values are bounded by the existing `DECIMAL(8,2)` columns.
   - Customers see only their own booking when signed in; an explicit tracking access token remains available for the driver's share link.
   - Only the authenticated owner assigned to a vehicle can start, update, or stop that vehicle's tracking session. Customer pages are view-only.
   - Stale/stopped positions are labeled as last reported. The booked route remains visible, but the live remaining route is removed; live status is not claimed.
   - Only confirmed/accepted bookings appear in the owner tracking selector and can be associated with an active GPS session. The backend verifies the owner's vehicle and booking authorization for starting and updating.
   - Confirmed bookings on **My Bookings** have a **View Live Tracking** link to the dedicated full-screen map. Pending and rejected bookings do not show the tracking link, and no map is embedded in the bookings table.
   - The booking-specific full-screen page polls the existing authorized tracking endpoint every five seconds, verifies the response booking ID, and draws the booked origin-to-destination OSRM road geometry from the selected booking's coordinates. The page does not geocode endpoints in the browser. Server-side Nominatim lookups are India-scoped, reject unrelated feature types for simple place names, and report unresolved routes rather than guessing. The known legacy spellings `banglore` and `anatapur` are presented as Bangalore and Anantapur for lookup/display only; database records remain unchanged. Owner start/stop and GPS controls remain on the authenticated owner dashboard.
   - Database poll failures return a sanitized temporary-unavailability response and are logged server-side. A failed poll does not mark the last GPS fix as live.

4. **Security & Device Diagnostics**:
   - Real-time GPS permission detection (`Granted`, `Prompt`, `Denied`).
   - Browser Secure Context checking (`window.isSecureContext`) with clear guidance for mobile devices over HTTP.

---

## 📁 Files Modified & Upgraded

| File | Purpose |
| :--- | :--- |
| [`tracking_service.py`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/tracking_service.py) | OSRM routing, India-scoped geocoding, and booking-specific route endpoints; no vehicle-default route substitution. |
| [`tracking_routes.py`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/tracking_routes.py) | Added dedicated `/live-tracking/api/route` endpoint providing road geometry and distance/time metrics. |
| [`templates/owner_dashboard.html`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/templates/owner_dashboard.html) | Added an owner-dashboard shortcut to the GPS broadcaster. |
| [`templates/customer_live_tracking.html`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/templates/customer_live_tracking.html) | Existing non-shortcut tracking view with the shared read-only Leaflet customer controller. |
| [`templates/customer_tracking_fullscreen.html`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/templates/customer_tracking_fullscreen.html) | Booking-specific full-screen customer map with booking route endpoints, GPS status, last-update time, route metrics, and view-only controls. |
| [`templates/my_bookings.html`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/templates/my_bookings.html) | Provides a dedicated expandable, read-only tracking panel for every booking while retaining the existing booking actions and columns. |
| [`static/booking_tracking.js`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/static/booking_tracking.js) | Polls each expanded booking by its own ID and shows its latest authorized GPS position, update time, status, and Leaflet map. |
| [`static/booking_tracking.css`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/static/booking_tracking.css) | Styles the in-row tracking panel and responsive map. |
| [`templates/owner_live_tracking.html`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/templates/owner_live_tracking.html) | Added camera follow toggle button and GPS & Device Security Status card with permission monitoring. |
| [`static/customer_tracking.js`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/static/customer_tracking.js) | Shared customer map controller: booked-origin-to-destination OSRM route, separate GPS marker, throttled remaining route, and stale tracking status. |
| [`static/owner_tracking.js`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/static/owner_tracking.js) | Driver broadcaster: secure-context/GPS validation, visible geolocation/API errors, and API-confirmed location broadcasting. |
| [`tests/test_owner_tracking.js`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/tests/test_owner_tracking.js) | Browser callback tests for the initial GPS fix, valid-coordinate checks, secure-context requirement, and API confirmation. |
| [`static/live_tracking.css`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/static/live_tracking.css) | Styles for the Trip Navigation Bar, active follow button, Google Maps teardrop destination pin with drop shadow, and responsive design. |
| [`run_live_tracking.py`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/run_live_tracking.py) | Enhanced application runner with `--ssl` option, safe ASCII terminal logging, and two-device testing instructions. |
| [`tests/test_live_tracking.py`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/tests/test_live_tracking.py) | Tracking service tests covering routing, geocoding, edge cases, stale data, and session authorization. |
| [`tests/test_customer_tracking.js`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/tests/test_customer_tracking.js) | Verifies booking-specific route endpoints, separate vehicle marker, stale status, missing geocodes, and routing failures. |
| [`tests/test_tracking_permissions.py`](file:///c:/Users/ADMIN/Desktop/BusinessVehicleBooking/tests/test_tracking_permissions.py) | Tests owner-only tracking controls, CSRF, customer read-only behavior, and polling UI wiring. |

---

## 🚀 How to Run and Test

### Option 1: Using the Windows Batch File
Double-click:
```bat
start_live_tracking_windows.bat
```

### Option 2: Using the Command Line
```powershell
call .venv\Scripts\activate
python run_live_tracking.py
```

---

## 🌐 URLs & Endpoints

| URL | Description |
| :--- | :--- |
| `http://localhost:5000/live-tracking/` | **Live Tracking Hub** — Overview and only the signed-in user's own bookings/sessions. |
| `http://localhost:5000/live-tracking/owner` | **Vehicle Owner Broadcaster** — Vehicle and booking selector, "Start Live Tracking" / "Stop Live Tracking", browser GPS access, and interactive map. |
| `http://localhost:5000/live-tracking/customer` | **Customer Live Tracker** — Leaflet map with latest vehicle position, road route, remaining distance, ETA, and auto-follow. |
| `http://localhost:5000/mybookings` | **Customer My Bookings** — Sign in as a customer and choose **View Live Tracking** for a confirmed booking to open its dedicated full-screen map. |
| `http://localhost:5000/live-tracking/booking/<id>` | **Full-screen Booking Live Map** — Authenticated, booking-specific location view with floating return, recenter, and full-screen controls (e.g. `http://localhost:5000/live-tracking/booking/8`). |
| `http://localhost:5000/live-tracking/api/route` | **OSRM Road Route API** — Accepts `start_lat`, `start_lng`, and `destination` or `dest_lat`, `dest_lng`. |
| `http://localhost:5000/` | **Original Website Home Page** — 100% untouched and fully operational. |

---

## 📱 How to Test Using Two Devices

### Method 1: Desktop Two-Window Test (Easiest & Recommended)
1. Open **Browser Window 1** (e.g. Chrome):
   - Go to `http://localhost:5000/ownerlogin` and log in (or use an active owner account).
   - From the Owner Dashboard, click **Start Live Tracking**, or navigate to `http://localhost:5000/live-tracking/owner`.
   - Select vehicle (e.g. Eicher) and booking (e.g. Booking #8).
   - Click **Start Live Tracking** and grant GPS permission when prompted. Updates are sent no more often than every five seconds.
   - Copy the shareable customer tracking link.
2. Open **Browser Window 2** (e.g. Firefox, Edge, or Chrome Incognito):
   - Paste the customer tracking link (or go to `http://localhost:5000/live-tracking/customer?token=<token>`).
   - This view is read-only. For booking-ID access, sign in as the customer who made that booking.
   - To follow the customer workflow, log in as the customer, open **My Bookings**, and choose **View Live Tracking** on the confirmed booking being broadcast. The dedicated full-screen map polls automatically every five seconds. Repeat on a second booking to confirm it uses that booking's ID and location, not the first one's. Before a GPS position arrives, it says “Waiting for driver location”; if sharing stops or becomes stale, it labels the marker as the last reported position.
   - Stop sharing with **Stop Live Tracking** on the driver's device.
   - When the driver's tab is closed or GPS access is unavailable, the map continues to show the timestamped last report with a stale/stopped status; only a newly accepted GPS update returns it to LIVE.

### Method 2: Testing with a Physical Mobile Device (Phone as Driver GPS)
Modern mobile browsers (Chrome, Safari) require a **Secure Context** to access hardware GPS (`navigator.geolocation`):
- **Option A (Chrome LAN flag on phone)**:
  1. On your Android phone in Chrome, open: `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
  2. Add your computer's LAN IP: `http://192.168.x.x:5000`
  3. Enable the flag and tap **Relaunch**.
  4. Log in as the owner, open `/live-tracking/owner`, select the assigned vehicle/booking, and press **Start Live Tracking**.
- **Option B (Instant HTTPS Tunneling)**:
  1. In a terminal, run: `npx localtunnel --port 5000` (or `ngrok http 5000`).
  2. Open the provided `https://...` URL on your phone.
  3. Because it uses valid HTTPS, mobile browser GPS works out of the box!
  4. Keep using that exact HTTPS hostname for the whole session. Flask sessions are
     host-scoped; switching between `localhost`, another tunnel hostname, or a
     newly generated tunnel URL means the browser will not send the previous
     session cookie. Open a fresh login page after changing the tunnel URL.
  5. For HTTPS-only tunnel use, set `SESSION_COOKIE_SECURE=1` in `.env` before
     starting the app. If Flask must recognize forwarded HTTPS/host information,
     set `TRUSTED_PROXY_HOPS=1` only when the tunnel is the one trusted proxy
     directly in front of Flask. Use `0` for direct local access.

---

## 🧪 Testing
Run the focused tracking and permission tests:
```powershell
.venv\Scripts\pytest.exe tests/test_live_tracking.py tests/test_tracking_permissions.py -v
```
The database-backed tracking lifecycle tests require the project's configured MySQL tracking tables. The owner/customer API permission and CSRF tests run without a database.

The in-row customer tracking UI uses existing tracking tables and APIs; it requires no database migration. Deploy the changed template and static assets, restart the Flask application, then test with at least two bookings (one pending/no GPS and one actively broadcasting).
