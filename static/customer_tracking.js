/**
 * Live Vehicle Location Tracking — Professional Customer Tracker
 * Features:
 * - Real GPS tracking with Leaflet.js & OpenStreetMap
 * - Smooth vehicle marker animation (gliding interpolation)
 * - Auto-centering / Follow vehicle mode with stop/resume toggle
 * - Labeled booked pickup and destination markers
 * - Blue OSRM road geometry for the booked journey; live GPS is a separate marker
 * - Throttled remaining-road route and metrics while GPS is current
 * - Graceful fallback when routing is unavailable (never draws fake straight line)
 * - Real-time status indicators (Live, Last Updated, GPS Unavailable, Connection Lost)
 */

(function () {
    "use strict";

    // Application & Map State
    let map = null;
    let vehicleMarker = null;
    let pickupMarker = null;
    let destinationMarker = null;
    let accuracyCircle = null;
    let bookedRoutePolyline = null;
    let remainingRoutePolyline = null;

    let isFollowActive = true;
    let isFirstPosition = true;
    let markerAnimFrame = null;
    let lastKnownPosition = null;
    let lastRoutedPosition = null;
    let lastRecordedTimestamp = null;
    let currentPickupCoords = null;
    let currentDestinationCoords = null;
    let bookedRouteData = null;
    let currentTrackingState = null;
    let lastTrackingData = null;
    let hasInitialFit = false;
    let bookedRouteKey = null;
    let lastBookedRouteAttemptAt = 0;
    let lastRemainingRouteAttemptAt = 0;
    let isPolling = false;

    let pollTimer = null;
    let relativeTimer = null;
    let isFetchingBookedRoute = false;
    let isFetchingRemainingRoute = false;
    let activeTileLayer = null;
    let hasLoadedMapTile = false;
    const POLL_INTERVAL_MS = 5000; // Poll every 5 seconds
    const RECALCULATE_DISTANCE_THRESHOLD_METERS = 1000; // Recalculate remaining route after meaningful movement
    const ROUTE_RETRY_INTERVAL_MS = 30000;
    const STALE_AFTER_SECONDS = 60;

    // Configuration from URL or DOM
    const urlParams = new URLSearchParams(window.location.search);
    const token = urlParams.get("token") || (document.getElementById("trackingMeta")?.dataset.token || "");
    const bookingId = urlParams.get("booking_id") || (document.getElementById("trackingMeta")?.dataset.bookingId || "");

    // DOM Elements
    const elements = {
        statusPill: document.getElementById("statusPill"),
        statusDot: document.getElementById("statusDot"),
        statusText: document.getElementById("statusText"),
        alertBanner: document.getElementById("alertBanner"),
        alertMessage: document.getElementById("alertMessage"),
        alertIcon: document.getElementById("alertIcon"),
        btnToggleFollow: document.getElementById("btnToggleFollow"),
        followIndicator: document.getElementById("followIndicator"),
        followText: document.getElementById("followText"),
        btnRecenter: document.getElementById("btnRecenterMap"),
        btnFitBounds: document.getElementById("btnFitBounds"),

        // Trip Navigation Stats Bar
        valRemainingDistance: document.getElementById("valRemainingDistance"),
        valRouteDistanceLabel: document.getElementById("routeDistanceLabel"),
        valEstDuration: document.getElementById("valEstDuration"),
        valEstArrival: document.getElementById("valEstArrival"),
        valLastUpdated: document.getElementById("telemetryLastUpdated"),

        // Vehicle Details
        valVehicleName: document.getElementById("valVehicleName"),
        valVehicleType: document.getElementById("valVehicleType"),
        valRegNumber: document.getElementById("valRegNumber"),
        valDriverName: document.getElementById("valDriverName"),
        valDriverPhone: document.getElementById("valDriverPhone"),

        // Sidebar Trip & Route Card
        valPickup: document.getElementById("valPickup"),
        valDestination: document.getElementById("valDestination"),
        valSidebarDistance: document.getElementById("valSidebarDistance"),
        valSidebarDuration: document.getElementById("valSidebarDuration"),
        valSidebarEta: document.getElementById("valSidebarEta"),
        valBookingDate: document.getElementById("valBookingDate"),
        valBookingStatus: document.getElementById("valBookingStatus"),

        // Telemetry Bar
        valSpeed: document.getElementById("telemetrySpeed"),
        valAccuracy: document.getElementById("telemetryAccuracy"),
        valHeading: document.getElementById("telemetryHeading"),
        valCoords: document.getElementById("telemetryCoords"),
    };

    // Calculate Haversine distance in meters between two [lat, lng] coordinates
    function calculateDistanceMeters(lat1, lon1, lat2, lon2) {
        const R = 6371000; // Earth radius in meters
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLon = (lon2 - lon1) * Math.PI / 180;
        const a =
            Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
        const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
        return R * c;
    }

    function escapePopupValue(value) {
        return String(value || "").replace(/[&<>"']/g, function (character) {
            return {
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            }[character];
        });
    }

    function updateVehiclePopup() {
        if (!vehicleMarker || !lastTrackingData || !lastTrackingData.location) return;
        const data = lastTrackingData;
        const locationIsLive = currentTrackingState === "LIVE";
        const accuracy = data.location.accuracy;
        const popup = `
            <div style="font-size: 13px;">
                <b style="color: #146b5b; font-size: 14px;">🚚 ${escapePopupValue(data.vehicle?.name || "Vehicle")}</b><br>
                <b>Registration:</b> ${escapePopupValue(data.vehicle?.registration_number || "Unavailable")}<br>
                <b>Position:</b> ${locationIsLive ? "Live GPS" : "Last reported — not current"}<br>
                <b>GPS Accuracy:</b> ${Number.isFinite(accuracy) && accuracy >= 0 ? `±${Math.round(accuracy)}m` : "Unavailable"}<br>
                <b>Updated:</b> ${escapePopupValue(formatRelativeTime(data.seconds_ago))}
            </div>
        `;
        if (vehicleMarker.getPopup()) {
            vehicleMarker.setPopupContent(popup);
        } else {
            vehicleMarker.bindPopup(popup);
        }
    }

    // Calculate bearing in degrees from point A to point B
    function calculateBearing(lat1, lon1, lat2, lon2) {
        const y = Math.sin((lon2 - lon1) * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180);
        const x = Math.cos(lat1 * Math.PI / 180) * Math.sin(lat2 * Math.PI / 180) -
                  Math.sin(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.cos((lon2 - lon1) * Math.PI / 180);
        const deg = Math.atan2(y, x) * 180 / Math.PI;
        return (deg + 360) % 360;
    }

    function showMapLoadStatus(message, isError = false) {
        const status = document.getElementById("mapLoadStatus");
        if (!status) return;
        status.textContent = message;
        status.dataset.state = isError ? "error" : "loading";
        status.hidden = false;
    }

    function hideMapLoadStatus() {
        const status = document.getElementById("mapLoadStatus");
        if (status) status.hidden = true;
    }

    function installTileLayer() {
        const url = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
        const attribution = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors';
        let tileErrors = 0;
        activeTileLayer = L.tileLayer(url, {
            maxZoom: 19,
            attribution,
        });
        activeTileLayer.on("tileload", function () {
            hasLoadedMapTile = true;
            hideMapLoadStatus();
        });
        activeTileLayer.on("tileerror", function (event) {
            tileErrors += 1;
            console.warn("Map tile request failed.", {
                provider: "OpenStreetMap",
                error: event && event.error ? String(event.error) : "tile load error",
            });
        });
        activeTileLayer.addTo(map);

        window.setTimeout(function () {
            if (!hasLoadedMapTile) {
                console.error("OpenStreetMap tiles failed to load.", {
                    failedTiles: tileErrors,
                });
                showMapLoadStatus(
                    "Map tiles could not be loaded. Check your internet connection or content-blocking settings, then reload.",
                    true
                );
            }
        }, 10000);
    }

    function loadLeafletFallback() {
        return new Promise(function (resolve) {
            const fallbackScript = document.createElement("script");
            fallbackScript.src = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js";
            fallbackScript.integrity = "sha256-20nQCchB9co0qIjJZRGuk2/ZVM+kNiyxNV1lvTlZBo=";
            fallbackScript.crossOrigin = "anonymous";
            fallbackScript.onload = function () {
                resolve(typeof L !== "undefined");
            };
            fallbackScript.onerror = function (error) {
                console.error("Leaflet could not load from the fallback CDN.", error);
                resolve(false);
            };
            document.head.appendChild(fallbackScript);
        });
    }

    // Initialize Leaflet Map
    function initMap() {
        if (map) return true;
        if (typeof L === "undefined") {
            console.error("Leaflet is unavailable; map initialization was skipped.");
            showMapLoadStatus("The map library could not be loaded. Check your connection and reload.", true);
            return false;
        }

        const mapContainer = document.getElementById("trackingMap");
        const bounds = mapContainer && mapContainer.getBoundingClientRect();
        if (!bounds || bounds.width <= 0 || bounds.height <= 0) {
            console.error("Map container has no visible size.", bounds);
            showMapLoadStatus("The map container is not visible. Reload the page to try again.", true);
            return false;
        }

        try {
            map = L.map(mapContainer, {
                zoomControl: true,
                scrollWheelZoom: true,
            }).setView([20, 0], 2);
            installTileLayer();
            window.requestAnimationFrame(function () {
                if (map) map.invalidateSize();
            });
            window.addEventListener("resize", function () {
                if (map) map.invalidateSize();
            });
            if (typeof ResizeObserver !== "undefined") {
                const observer = new ResizeObserver(function () {
                    if (map) map.invalidateSize();
                });
                observer.observe(mapContainer);
            }
        } catch (error) {
            console.error("Leaflet map initialization failed.", error);
            map = null;
            showMapLoadStatus("The interactive map could not be initialized. Reload the page to try again.", true);
            return false;
        }

        // When the user manually drags the map, temporarily pause auto-follow
        map.on("dragstart", function () {
            if (isFollowActive) {
                setFollowMode(false, true);
            }
        });
        return true;
    }

    // Custom Marker Icons
    function getVehicleIcon(heading) {
        const rot = (heading !== null && heading !== undefined) ? `transform: rotate(${Math.round(heading)}deg);` : "";
        return L.divIcon({
            className: "custom-vehicle-marker",
            html: `
                <div class="vehicle-marker-pulse-wrapper">
                    <div class="vehicle-marker-ring"></div>
                    <div class="vehicle-marker-core" style="${rot}">🚚</div>
                </div>
            `,
            iconSize: [44, 44],
            iconAnchor: [22, 22],
            popupAnchor: [0, -22],
        });
    }

    function getDestinationIcon() {
        return L.divIcon({
            className: "custom-destination-marker",
            html: `
                <div class="destination-pin-wrapper">
                    <div class="destination-pin-shadow"></div>
                    <div class="destination-pin-body">
                        <span class="destination-pin-icon">🏁</span>
                    </div>
                </div>
            `,
            iconSize: [36, 44],
            iconAnchor: [18, 42],
            popupAnchor: [0, -42],
        });
    }

    function getPickupIcon() {
        return L.divIcon({
            className: "custom-pickup-marker",
            html: `
                <div class="pickup-pin-wrapper">
                    <div class="pickup-pin-shadow"></div>
                    <div class="pickup-pin-body">
                        <span class="pickup-pin-icon">🟢</span>
                    </div>
                </div>
            `,
            iconSize: [32, 40],
            iconAnchor: [16, 38],
            popupAnchor: [0, -38],
        });
    }

    // Smooth Marker Animation (glides smoothly between coordinates)
    function animateMarkerTo(marker, newLatLng, duration = 1200) {
        if (!marker) return;
        const startLatLng = marker.getLatLng();
        if (!startLatLng) {
            marker.setLatLng(newLatLng);
            return;
        }

        const deltaLat = newLatLng[0] - startLatLng.lat;
        const deltaLng = newLatLng[1] - startLatLng.lng;
        const distanceM = calculateDistanceMeters(startLatLng.lat, startLatLng.lng, newLatLng[0], newLatLng[1]);

        // If distance is tiny (< 0.5m) or huge teleport (> 100km, e.g. first GPS fix), jump directly
        if (distanceM < 0.5 || distanceM > 100000) {
            marker.setLatLng(newLatLng);
            if (isFollowActive && map) {
                map.panTo(newLatLng, { animate: true, duration: 0.8 });
            }
            return;
        }

        if (markerAnimFrame) {
            cancelAnimationFrame(markerAnimFrame);
        }

        const startTime = performance.now();

        function step(now) {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1.0);
            // Ease-out quad smoothing curve
            const ease = 1 - (1 - progress) * (1 - progress);

            const curLat = startLatLng.lat + deltaLat * ease;
            const curLng = startLatLng.lng + deltaLng * ease;
            marker.setLatLng([curLat, curLng]);

            if (isFollowActive && map) {
                map.panTo([curLat, curLng], { animate: false });
            }

            if (progress < 1.0) {
                markerAnimFrame = requestAnimationFrame(step);
            }
        }

        markerAnimFrame = requestAnimationFrame(step);
    }

    // Toggle and Manage Follow Vehicle Mode
    function setFollowMode(active, isUserInterruption = false) {
        isFollowActive = active;
        if (!elements.btnToggleFollow) return;

        if (isFollowActive) {
            elements.btnToggleFollow.classList.add("btn-follow-active");
            if (elements.followIndicator) elements.followIndicator.textContent = "🟢";
            if (elements.followText) elements.followText.textContent = "Following (Click to Stop)";
            if (vehicleMarker && map) {
                map.panTo(vehicleMarker.getLatLng(), { animate: true, duration: 0.8 });
            }
        } else {
            elements.btnToggleFollow.classList.remove("btn-follow-active");
            if (elements.followIndicator) elements.followIndicator.textContent = "🎯";
            if (elements.followText) elements.followText.textContent = "Follow Vehicle";
            if (isUserInterruption) {
                showToast("Auto-follow paused. Click 'Follow Vehicle' to resume.");
            }
        }
    }

    // Toast Notification
    function showToast(msg) {
        let toast = document.getElementById("trackingToast");
        if (!toast) {
            toast = document.createElement("div");
            toast.id = "trackingToast";
            toast.className = "tracking-toast";
            document.body.appendChild(toast);
        }
        toast.textContent = msg;
        toast.classList.add("show");
        setTimeout(() => toast.classList.remove("show"), 3200);
    }

    // Update Banner Message
    function showAlert(msg, type = "warning", icon = "ℹ️") {
        if (!elements.alertBanner || !elements.alertMessage) return;
        elements.alertMessage.textContent = msg;
        if (elements.alertIcon) elements.alertIcon.textContent = icon;
        elements.alertBanner.className = "map-alert-banner";
        if (type === "danger") elements.alertBanner.classList.add("banner-danger");
        if (type === "success") elements.alertBanner.classList.add("banner-success");
        elements.alertBanner.style.display = "flex";
    }

    function hideAlert() {
        if (elements.alertBanner) {
            elements.alertBanner.style.display = "none";
        }
    }

    // Format Relative Time ("4s ago", "2 mins ago")
    function formatRelativeTime(seconds) {
        if (seconds === null || seconds === undefined) return "Never";
        if (seconds < 5) return "Just now";
        if (seconds < 60) return `${seconds}s ago`;
        const mins = Math.floor(seconds / 60);
        if (mins === 1) return "1 min ago";
        if (mins < 60) return `${mins} mins ago`;
        const hours = Math.floor(mins / 60);
        return `${hours}h ago`;
    }

    // Update Status Pill
    function setTrackingStatus(state, secondsAgo) {
        if (!elements.statusPill || !elements.statusDot || !elements.statusText) return;
        elements.statusPill.className = "map-status-pill";
        elements.statusDot.className = "pulsing-dot";

        if (state === "LIVE") {
            elements.statusPill.classList.add("pill-live");
            elements.statusDot.classList.add("dot-live");
            elements.statusText.textContent = "LIVE • ON TRIP";
            hideAlert();
        } else if (state === "OUTDATED") {
            elements.statusPill.classList.add("pill-stale");
            elements.statusDot.classList.add("dot-waiting");
            elements.statusText.textContent = "LOCATION OUTDATED";
            showAlert(
                `No recent GPS update. The last reported position was ${formatRelativeTime(secondsAgo)} ago.`,
                "warning",
                "⚠️"
            );
        } else if (state === "WAITING_FOR_GPS") {
            elements.statusPill.classList.add("pill-waiting");
            elements.statusDot.classList.add("dot-waiting");
            elements.statusText.textContent = "WAITING FOR DRIVER LOCATION";
            showAlert("Driver has started broadcasting. Waiting for initial GPS satellite coordinates...", "warning", "🛰️");
        } else if (state === "STOPPED") {
            elements.statusPill.classList.add("pill-stopped");
            elements.statusDot.classList.add("dot-stopped");
            elements.statusText.textContent = "TRIP ENDED / STOPPED";
            showAlert("Live location sharing has been stopped by the driver.", "warning", "⏹️");
        } else if (state === "NOT_STARTED") {
            elements.statusPill.classList.add("pill-waiting");
            elements.statusDot.classList.add("dot-waiting");
            elements.statusText.textContent = "WAITING FOR DRIVER LOCATION";
            showAlert("Waiting for driver location. Live tracking has not started yet.", "warning", "⏳");
        } else if (state === "UNAVAILABLE") {
            elements.statusPill.classList.add("pill-stopped");
            elements.statusDot.classList.add("dot-stopped");
            elements.statusText.textContent = "LOCATION UNAVAILABLE";
            showAlert("Vehicle location is currently unavailable.", "warning", "📍");
        } else if (state === "CONNECTION_LOST") {
            elements.statusPill.classList.add("pill-stopped");
            elements.statusDot.classList.add("dot-stopped");
            elements.statusText.textContent = "CONNECTION LOST";
            showAlert("Could not reach the tracking service. Automatically attempting to reconnect; the marker shows the last reported position.", "danger", "⚡");
        } else if (state === "SERVICE_UNAVAILABLE") {
            elements.statusPill.classList.add("pill-stopped");
            elements.statusDot.classList.add("dot-stopped");
            elements.statusText.textContent = "TRACKING SERVICE UNAVAILABLE";
            showAlert("The tracking server or database is temporarily unavailable. The map shows the last reported position.", "danger", "⚠️");
        } else if (state === "ACCESS_DENIED") {
            elements.statusPill.classList.add("pill-stopped");
            elements.statusDot.classList.add("dot-stopped");
            elements.statusText.textContent = "TRACKING ACCESS UNAVAILABLE";
            showAlert("This booking's tracking data could not be authorized. Please return to My Bookings and sign in again.", "danger", "🔒");
        } else {
            elements.statusPill.classList.add("pill-waiting");
            elements.statusDot.classList.add("dot-waiting");
            elements.statusText.textContent = "LOCATION UNAVAILABLE";
            showAlert("Vehicle location is currently unavailable.", "warning", "📍");
        }
    }

    function asCoordinatePair(coords) {
        if (!Array.isArray(coords) || coords.length < 2) return null;
        if (coords[0] === null || coords[1] === null ||
            (typeof coords[0] === "string" && !coords[0].trim()) ||
            (typeof coords[1] === "string" && !coords[1].trim())) return null;
        const latitude = Number(coords[0]);
        const longitude = Number(coords[1]);
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude) ||
            Math.abs(latitude) > 90 || Math.abs(longitude) > 180) return null;
        return [latitude, longitude];
    }

    function normalizeRoadRoute(route) {
        if (!route || !Array.isArray(route.coordinates) || route.coordinates.length < 2) return null;
        if (route.distance_meters === null || route.distance_meters === undefined ||
            route.duration_seconds === null || route.duration_seconds === undefined) return null;
        const coordinates = route.coordinates.map(asCoordinatePair);
        const distance = Number(route.distance_meters);
        const duration = Number(route.duration_seconds);
        if (coordinates.some((point) => !point) || !Number.isFinite(distance) || distance < 0 ||
            !Number.isFinite(duration) || duration < 0) return null;
        return { coordinates, distance_meters: distance, duration_seconds: duration };
    }

    async function requestRoadRoute(originCoords, destCoords) {
        const origin = asCoordinatePair(originCoords);
        const destination = asCoordinatePair(destCoords);
        if (!origin || !destination) return null;

        let routeData = null;
        try {
            const params = new URLSearchParams({
                start_lat: String(origin[0]),
                start_lng: String(origin[1]),
                dest_lat: String(destination[0]),
                dest_lng: String(destination[1]),
            });
            const controller = new AbortController();
            const timeout = window.setTimeout(() => controller.abort(), 10000);
            let response;
            try {
                response = await fetch(`/live-tracking/api/route?${params.toString()}`, { signal: controller.signal });
            } finally {
                window.clearTimeout(timeout);
            }
            if (response.ok) {
                const result = await response.json();
                if (result.success) {
                    routeData = normalizeRoadRoute(result.route);
                    if (!routeData) console.warn("Tracking API returned invalid road-route geometry or metrics.");
                } else {
                    console.warn("Tracking API could not calculate the road route.", result.error || "No details returned");
                }
            } else {
                console.warn("Tracking API road-route request failed.", { status: response.status });
            }
        } catch (error) {
            console.warn("Backend route API attempt failed; trying OSRM directly:", error);
        }
        if (routeData) return routeData;

        try {
            const [oLat, oLng] = origin;
            const [dLat, dLng] = destination;
            const url = `https://router.project-osrm.org/route/v1/driving/${oLng.toFixed(6)},${oLat.toFixed(6)};${dLng.toFixed(6)},${dLat.toFixed(6)}?overview=full&geometries=geojson`;
            const controller = new AbortController();
            const timeout = window.setTimeout(() => controller.abort(), 10000);
            let response;
            try {
                response = await fetch(url, { signal: controller.signal });
            } finally {
                window.clearTimeout(timeout);
            }
            if (!response.ok) {
                console.warn("Public OSRM route request failed.", { status: response.status });
                return null;
            }
            const result = await response.json();
            if (result.code !== "Ok" || !Array.isArray(result.routes) || !result.routes.length) {
                console.warn("Public OSRM returned no valid route.", result.message || result.code);
                return null;
            }
            const route = result.routes[0];
            const coordinates = route.geometry?.coordinates?.map((point) => [point[1], point[0]]);
            return normalizeRoadRoute({
                coordinates,
                distance_meters: route.distance,
                duration_seconds: route.duration,
            });
        } catch (error) {
            console.warn("Direct OSRM route request failed:", error);
            return null;
        }
    }

    function setRouteMessage(message) {
        const routeMessage = document.getElementById("routeMessage");
        if (routeMessage) routeMessage.textContent = message || "";
    }

    function displayRouteMetrics(route, label) {
        const distance = `${(route.distance_meters / 1000).toFixed(1)} km`;
        const duration = route.duration_seconds < 60
            ? "< 1 min"
            : `${Math.round(route.duration_seconds / 60)} mins`;
        if (elements.valRouteDistanceLabel) elements.valRouteDistanceLabel.textContent = label;
        if (elements.valRemainingDistance) elements.valRemainingDistance.textContent = distance;
        if (elements.valEstDuration) elements.valEstDuration.textContent = duration;
        setRouteMessage("");
    }

    function showRouteUnavailable(message) {
        if (elements.valRouteDistanceLabel) elements.valRouteDistanceLabel.textContent = "Road route";
        if (elements.valRemainingDistance) elements.valRemainingDistance.textContent = "Route distance temporarily unavailable";
        if (elements.valEstDuration) elements.valEstDuration.textContent = "Route distance temporarily unavailable";
        setRouteMessage(message);
    }

    function restoreBookedRouteMetrics() {
        if (bookedRouteData) {
            displayRouteMetrics(bookedRouteData, "Booked road distance");
        }
    }

    function clearRemainingRoute() {
        if (remainingRoutePolyline && map) map.removeLayer(remainingRoutePolyline);
        remainingRoutePolyline = null;
        lastRoutedPosition = null;
    }

    async function updateBookedRoute() {
        if (!currentPickupCoords || !currentDestinationCoords) {
            bookedRouteData = null;
            bookedRouteKey = null;
            if (bookedRoutePolyline && map) map.removeLayer(bookedRoutePolyline);
            bookedRoutePolyline = null;
            clearRemainingRoute();
            showRouteUnavailable("The booked pickup or destination could not be resolved confidently. No route is displayed.");
            return;
        }

        const requestKey = JSON.stringify([currentPickupCoords, currentDestinationCoords]);
        if (bookedRouteKey !== requestKey) {
            if (bookedRoutePolyline && map) map.removeLayer(bookedRoutePolyline);
            bookedRoutePolyline = null;
            bookedRouteData = null;
            clearRemainingRoute();
            bookedRouteKey = requestKey;
            lastBookedRouteAttemptAt = 0;
        }
        if (bookedRoutePolyline || isFetchingBookedRoute ||
            Date.now() - lastBookedRouteAttemptAt < ROUTE_RETRY_INTERVAL_MS) return;

        isFetchingBookedRoute = true;
        lastBookedRouteAttemptAt = Date.now();
        if (elements.valRouteDistanceLabel) elements.valRouteDistanceLabel.textContent = "Booked road distance";
        if (elements.valRemainingDistance) elements.valRemainingDistance.textContent = "Calculating booked road route…";
        if (elements.valEstDuration) elements.valEstDuration.textContent = "Calculating…";

        const route = await requestRoadRoute(currentPickupCoords, currentDestinationCoords);
        isFetchingBookedRoute = false;
        if (bookedRouteKey !== requestKey) return;

        if (!route) {
            showRouteUnavailable("The road route is temporarily unavailable. The booked endpoints remain marked.");
            return;
        }

        bookedRouteData = route;
        bookedRoutePolyline = L.polyline(route.coordinates, {
            color: "#1a73e8",
            weight: 6,
            opacity: 0.9,
            lineJoin: "round",
            lineCap: "round",
        }).addTo(map);
        bookedRoutePolyline.bindPopup("Booked road route from pickup to destination");
        displayRouteMetrics(route, "Booked road distance");
        fitAllBounds();
    }

    async function updateRemainingRoute(originCoords) {
        if (currentTrackingState !== "LIVE" || !originCoords || !currentDestinationCoords) {
            clearRemainingRoute();
            restoreBookedRouteMetrics();
            return;
        }

        const origin = asCoordinatePair(originCoords);
        if (!origin || isFetchingRemainingRoute) return;
        const moved = lastRoutedPosition
            ? calculateDistanceMeters(lastRoutedPosition[0], lastRoutedPosition[1], origin[0], origin[1])
            : Infinity;
        const shouldRetry = !remainingRoutePolyline &&
            Date.now() - lastRemainingRouteAttemptAt >= ROUTE_RETRY_INTERVAL_MS;
        if (remainingRoutePolyline && moved < RECALCULATE_DISTANCE_THRESHOLD_METERS) return;
        if (!remainingRoutePolyline && !shouldRetry) return;

        isFetchingRemainingRoute = true;
        lastRemainingRouteAttemptAt = Date.now();
        if (remainingRoutePolyline && map) map.removeLayer(remainingRoutePolyline);
        remainingRoutePolyline = null;

        const route = await requestRoadRoute(origin, currentDestinationCoords);
        isFetchingRemainingRoute = false;
        if (currentTrackingState !== "LIVE") {
            restoreBookedRouteMetrics();
            return;
        }
        if (!route) {
            restoreBookedRouteMetrics();
            setRouteMessage("The live remaining route is temporarily unavailable; the booked route remains visible.");
            return;
        }

        remainingRoutePolyline = L.polyline(route.coordinates, {
            color: "#0f766e",
            weight: 5,
            opacity: 0.9,
            dashArray: "9 7",
            lineJoin: "round",
            lineCap: "round",
        }).addTo(map);
        remainingRoutePolyline.bindPopup("Remaining road route from the latest live vehicle position");
        lastRoutedPosition = origin;
        displayRouteMetrics(route, "Remaining road distance");
    }

    function renderEndpoints(pickupText, destText, preloadedPickupCoords, preloadedDestCoords) {
        if (!map) return;
        currentPickupCoords = asCoordinatePair(preloadedPickupCoords);
        currentDestinationCoords = asCoordinatePair(preloadedDestCoords);

        if (currentPickupCoords) {
            if (!pickupMarker) {
                pickupMarker = L.marker(currentPickupCoords, {
                    icon: getPickupIcon(),
                    zIndexOffset: 800,
                }).addTo(map);
            } else {
                pickupMarker.setLatLng(currentPickupCoords);
            }
            pickupMarker.bindPopup(`<b>Origin / Pickup:</b><br>${escapePopupValue(pickupText)}`);
            pickupMarker.bindTooltip(`Origin: ${escapePopupValue(pickupText)}`, {
                permanent: true,
                direction: "top",
            });
        } else if (pickupMarker) {
            map.removeLayer(pickupMarker);
            pickupMarker = null;
        }

        if (currentDestinationCoords) {
            if (!destinationMarker) {
                destinationMarker = L.marker(currentDestinationCoords, {
                    icon: getDestinationIcon(),
                    zIndexOffset: 900,
                }).addTo(map);
            } else {
                destinationMarker.setLatLng(currentDestinationCoords);
            }
            destinationMarker.bindPopup(`<b>Destination:</b><br>${escapePopupValue(destText)}`);
            destinationMarker.bindTooltip(`Destination: ${escapePopupValue(destText)}`, {
                permanent: true,
                direction: "top",
            });
        } else if (destinationMarker) {
            map.removeLayer(destinationMarker);
            destinationMarker = null;
        }

        if (!currentPickupCoords || !currentDestinationCoords) {
            bookedRouteKey = null;
            updateBookedRoute();
            fitInitialBounds();
            return;
        }

        updateBookedRoute();
        fitInitialBounds();
    }

    // Apply Live Data to UI and Map
    async function applyTrackingData(data) {
        if (!data) return;

        lastTrackingData = data;
        currentTrackingState = data.tracking_state;
        initMap();

        // 1. Vehicle Info
        if (data.vehicle) {
            if (elements.valVehicleName) elements.valVehicleName.textContent = data.vehicle.name || "Vehicle";
            if (elements.valVehicleType) elements.valVehicleType.textContent = data.vehicle.type || "-";
            if (elements.valRegNumber) elements.valRegNumber.textContent = data.vehicle.registration_number || "-";
            if (elements.valDriverName) elements.valDriverName.textContent = data.vehicle.owner_name || "Owner";
            if (elements.valDriverPhone) {
                const phone = data.vehicle.contact_number;
                elements.valDriverPhone.innerHTML = phone
                    ? `<a href="tel:${phone}" style="color: var(--track-primary); text-decoration: underline;">📞 ${phone}</a>`
                    : "-";
            }
        }

        // 2. Booking Info & Endpoints
        if (data.booking) {
            if (elements.valPickup) elements.valPickup.textContent = data.booking.pickup || "Not Specified";
            if (elements.valDestination) elements.valDestination.textContent = data.booking.destination || "Not Specified";
            if (elements.valBookingDate) elements.valBookingDate.textContent = data.booking.booking_date || "-";
            if (elements.valBookingStatus) elements.valBookingStatus.textContent = data.booking.status || "-";

            const elBookingId = document.getElementById("valBookingId");
            if (elBookingId && data.booking.id) elBookingId.textContent = "#" + data.booking.id;
            const elMetricBookingId = document.getElementById("metricBookingId");
            if (elMetricBookingId && data.booking.id) elMetricBookingId.textContent = "#" + data.booking.id;
            const elRent = document.getElementById("valRent");
            if (elRent && data.booking.rent) elRent.textContent = "₹" + data.booking.rent;
            const elMetricRent = document.getElementById("metricRent");
            if (elMetricRent && data.booking.rent) elMetricRent.textContent = "₹" + data.booking.rent;
            const elCustomerName = document.getElementById("valCustomerName");
            if (elCustomerName && data.booking.customer_name) elCustomerName.textContent = data.booking.customer_name;

            renderEndpoints(
                data.booking.pickup,
                data.booking.destination,
                data.booking.pickup_coords,
                data.booking.destination_coords
            );

            if (!data.booking.destination) {
                showRouteUnavailable("No destination is saved on this booking. No route is displayed.");
            }
        }

        // 3. Status and Clock
        setTrackingStatus(data.tracking_state, data.seconds_ago);
        lastRecordedTimestamp = data.seconds_ago !== null ? Date.now() - (data.seconds_ago * 1000) : null;
        updateRelativeTime();
        const routeBasis = document.getElementById("routeBasis");
        if (routeBasis) {
            routeBasis.hidden = !["OUTDATED", "STOPPED"].includes(currentTrackingState);
        }

        // 4. Vehicle Coordinates & Live Movement
        if (data.location && data.location.latitude !== null && data.location.longitude !== null) {
            const latLng = asCoordinatePair([data.location.latitude, data.location.longitude]);
            if (!latLng) {
                clearRemainingRoute();
                return;
            }
            const [lat, lng] = latLng;
            const accuracy = data.location.accuracy;
            const speed = data.location.speed;
            const locationIsLive = data.tracking_state === "LIVE";
            let heading = data.location.heading;

            // If GPS heading is not supplied, compute bearing from previous coordinate
            if ((heading === null || heading === undefined) && lastKnownPosition) {
                const moved = calculateDistanceMeters(lastKnownPosition[0], lastKnownPosition[1], lat, lng);
                if (moved > 3.0) {
                    heading = calculateBearing(lastKnownPosition[0], lastKnownPosition[1], lat, lng);
                }
            }

            // Update Telemetry Values
            if (elements.valSpeed) {
                elements.valSpeed.textContent = locationIsLive
                    ? ((speed !== null && speed !== undefined) ? `${(speed * 3.6).toFixed(1)} km/h` : "0 km/h")
                    : "Not current";
            }
            if (elements.valAccuracy) {
                elements.valAccuracy.textContent = accuracy ? `± ${Math.round(accuracy)} m` : "Normal";
            }
            if (elements.valHeading) {
                elements.valHeading.textContent = (heading !== null && heading !== undefined) ? `${Math.round(heading)}°` : "--";
            }
            if (elements.valCoords) {
                elements.valCoords.textContent = `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
            }

            // Vehicle Marker with Smooth Animation
            if (!vehicleMarker) {
                vehicleMarker = L.marker(latLng, {
                    icon: getVehicleIcon(heading),
                    zIndexOffset: 1000,
                }).addTo(map);
            } else {
                animateMarkerTo(vehicleMarker, latLng, 1200);
                vehicleMarker.setIcon(getVehicleIcon(heading));
            }

            updateVehiclePopup();

            // Accuracy Circle
            if (accuracy && accuracy > 0) {
                if (!accuracyCircle) {
                    accuracyCircle = L.circle(latLng, {
                        radius: accuracy,
                        color: "#146b5b",
                        fillColor: "#146b5b",
                        fillOpacity: 0.1,
                        weight: 1.5,
                    }).addTo(map);
                } else {
                    accuracyCircle.setLatLng(latLng);
                    accuracyCircle.setRadius(accuracy);
                }
            }

            // On first position fix, frame appropriately
            if (isFirstPosition && !currentPickupCoords) map.setView(latLng, 15);
            isFirstPosition = false;
            fitInitialBounds();

            updateRemainingRoute(latLng);
            lastKnownPosition = latLng;
        } else {
            updateRemainingRoute(null);
        }
    }

    // Continuously update the "Updated X seconds ago" label every second
    function updateRelativeTime() {
        if (!elements.valLastUpdated) return;
        if (!lastRecordedTimestamp) {
            elements.valLastUpdated.textContent = "Waiting for driver location.";
            return;
        }
        const elapsedSecs = Math.max(0, Math.floor((Date.now() - lastRecordedTimestamp) / 1000));
        elements.valLastUpdated.textContent = `Location last updated ${formatRelativeTime(elapsedSecs)}`;
        if (lastTrackingData) lastTrackingData.seconds_ago = elapsedSecs;
        if (elapsedSecs > STALE_AFTER_SECONDS && currentTrackingState === "LIVE") {
            currentTrackingState = "OUTDATED";
            setTrackingStatus("OUTDATED", elapsedSecs);
            updateRemainingRoute(null);
            updateVehiclePopup();
            if (vehicleMarker) {
                const routeBasis = document.getElementById("routeBasis");
                if (routeBasis) routeBasis.hidden = false;
            }
        }
    }

    // Fetch latest location via polling API
    async function pollLiveLocation() {
        if ((!token && !bookingId) || isPolling) return;

        const query = token ? `token=${encodeURIComponent(token)}` : `booking_id=${encodeURIComponent(bookingId)}`;
        isPolling = true;
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 10000);
        try {
            const res = await fetch(`/live-tracking/api/location/poll?${query}`, { signal: controller.signal });
            if (!res.ok) {
                const errData = await res.json().catch(() => ({}));
                const state = res.status === 401 || res.status === 403 || res.status === 404
                    ? "ACCESS_DENIED"
                    : res.status >= 500 ? "SERVICE_UNAVAILABLE" : "CONNECTION_LOST";
                setTrackingStatus(state);
                if (errData.error) {
                    showAlert(
                        state === "ACCESS_DENIED"
                            ? "This booking's tracking data is not available to this account."
                            : errData.error,
                        "danger",
                        state === "ACCESS_DENIED" ? "🔒" : "🚫"
                    );
                }
                return;
            }

            const data = await res.json();
            if (data.success && data.data) {
                if (bookingId && Number(data.data.booking?.id) !== Number(bookingId)) {
                    setTrackingStatus("CONNECTION_LOST");
                    showAlert("Tracking data did not match this booking.", "danger", "🚫");
                    return;
                }
                await applyTrackingData(data.data);
            } else if (data.error) {
                showAlert(data.error, "danger", "🚫");
                setTrackingStatus("SERVICE_UNAVAILABLE");
            }
        } catch (err) {
            console.warn("Polling network error:", err);
            setTrackingStatus("CONNECTION_LOST");
        } finally {
            window.clearTimeout(timeout);
            isPolling = false;
        }
    }

    // Recenter Map on Vehicle
    function recenterMap() {
        if (!map || !vehicleMarker) return;
        setFollowMode(true);
        map.setView(vehicleMarker.getLatLng(), 16);
    }

    // Fit map bounds to show Pickup, Vehicle, Route, and Destination
    function fitAllBounds() {
        if (!map) return;
        const group = [];
        if (vehicleMarker) group.push(vehicleMarker.getLatLng());
        if (pickupMarker) group.push(pickupMarker.getLatLng());
        if (destinationMarker) group.push(destinationMarker.getLatLng());
        if (bookedRoutePolyline) {
            map.fitBounds(bookedRoutePolyline.getBounds(), { padding: [50, 50], maxZoom: 16 });
            return;
        }

        if (group.length > 0) {
            const bounds = L.latLngBounds(group);
            map.fitBounds(bounds, { padding: [50, 50], maxZoom: 16 });
        }
    }

    function fitInitialBounds() {
        if (hasInitialFit || !pickupMarker || !destinationMarker) return;
        hasInitialFit = true;
        fitAllBounds();
    }

    // DOM Ready
    document.addEventListener("DOMContentLoaded", async function () {
        if (typeof L === "undefined") {
            showMapLoadStatus("Loading the interactive map library…");
            const leafletLoaded = await loadLeafletFallback();
            if (!leafletLoaded) {
                showMapLoadStatus("The map library could not be loaded. Check your connection and reload.", true);
                return;
            }
        }
        if (!initMap()) return;

        if (elements.btnToggleFollow) {
            elements.btnToggleFollow.addEventListener("click", function () {
                setFollowMode(!isFollowActive);
            });
        }
        if (elements.btnRecenter) elements.btnRecenter.addEventListener("click", recenterMap);
        if (elements.btnFitBounds) elements.btnFitBounds.addEventListener("click", fitAllBounds);
        const fullscreenButton = document.getElementById("btnFullscreen");
        if (fullscreenButton) {
            const updateFullscreenButton = function () {
                const isFullscreen = Boolean(document.fullscreenElement);
                fullscreenButton.textContent = isFullscreen ? "⛶ Exit Full Screen" : "⛶ Full Screen";
                fullscreenButton.setAttribute("aria-pressed", String(isFullscreen));
                if (map) window.setTimeout(function () { map.invalidateSize(); }, 100);
            };
            fullscreenButton.addEventListener("click", async function () {
                try {
                    if (document.fullscreenElement) {
                        await document.exitFullscreen();
                    } else {
                        await document.documentElement.requestFullscreen();
                    }
                } catch (error) {
                    showAlert("Full-screen mode is not available in this browser.", "warning", "ℹ️");
                }
            });
            document.addEventListener("fullscreenchange", updateFullscreenButton);
        }

        // Check if initial data was pre-rendered by server
        const initialDataEl = document.getElementById("initialTrackingData");
        if (initialDataEl && initialDataEl.dataset.json) {
            try {
                const initial = JSON.parse(initialDataEl.dataset.json);
                applyTrackingData(initial);
            } catch (e) {
                console.error("Failed to parse initial tracking data", e);
            }
        }

        // Start Polling Loop
        pollLiveLocation();
        pollTimer = setInterval(pollLiveLocation, POLL_INTERVAL_MS);

        // Start 1-second Clock Ticker
        relativeTimer = setInterval(updateRelativeTime, 1000);

        window.addEventListener("beforeunload", () => {
            if (pollTimer) clearInterval(pollTimer);
            if (relativeTimer) clearInterval(relativeTimer);
            if (markerAnimFrame) cancelAnimationFrame(markerAnimFrame);
        });
    });

})();
