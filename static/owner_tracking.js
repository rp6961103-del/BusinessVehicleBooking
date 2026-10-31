/**
 * Live Vehicle Location Tracking — Vehicle Owner Broadcaster Controller
 * Captures real GPS coordinates via navigator.geolocation.watchPosition
 * and broadcasts them to the backend server.
 * NO simulated or fake coordinates.
 * Features:
 * - GPS Permission Status monitor (Granted, Prompt, Denied)
 * - Browser Secure-Context verification (HTTPS vs localhost)
 * - Interactive Leaflet map with smooth vehicle marker animation
 * - Auto-Follow mode with toggle button
 * - Start / Stop broadcasting controls
 * - Real-time telemetry readout
 */

(function () {
    "use strict";

    // Application State
    let watchId = null;
    let activeSession = null;
    let map = null;
    let vehicleMarker = null;
    let accuracyCircle = null;
    let markerAnimFrame = null;

    let isFollowActive = true;
    let lastTransmissionTime = 0;
    let transmissionInProgress = false;
    let updateCount = 0;
    const TRANSMIT_THROTTLE_MS = 5000;

    // DOM Elements
    const elements = {
        vehicleSelect: document.getElementById("vehicleSelect"),
        bookingSelect: document.getElementById("bookingSelect"),
        regNumberInput: document.getElementById("regNumberInput"),
        btnStart: document.getElementById("btnStartTracking"),
        btnStop: document.getElementById("btnStopTracking"),
        btnToggleFollow: document.getElementById("btnToggleFollow"),
        followIndicator: document.getElementById("followIndicator"),
        followText: document.getElementById("followText"),
        btnRecenter: document.getElementById("btnRecenterMap"),
        statusPill: document.getElementById("statusPill"),
        statusText: document.getElementById("statusText"),
        statusDot: document.getElementById("statusDot"),
        alertBanner: document.getElementById("alertBanner"),
        alertMessage: document.getElementById("alertMessage"),
        shareBox: document.getElementById("shareLinkBox"),
        shareInput: document.getElementById("shareLinkInput"),
        btnCopy: document.getElementById("btnCopyLink"),

        // Device & GPS Diagnostic
        valGpsPermission: document.getElementById("valGpsPermission"),
        valSecureContext: document.getElementById("valSecureContext"),
        valGpsLock: document.getElementById("valGpsLock"),
        insecureContextAlert: document.getElementById("insecureContextAlert"),

        // Telemetry
        valLat: document.getElementById("telemetryLat"),
        valLng: document.getElementById("telemetryLng"),
        valAccuracy: document.getElementById("telemetryAccuracy"),
        valSpeed: document.getElementById("telemetrySpeed"),
        valUpdates: document.getElementById("telemetryUpdates"),
        valLastSent: document.getElementById("telemetryLastSent"),
        toast: document.getElementById("trackingToast"),
    };

    function getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") : "";
    }

    function showToast(msg) {
        if (!elements.toast) return;
        elements.toast.textContent = msg;
        elements.toast.classList.add("show");
        setTimeout(() => elements.toast.classList.remove("show"), 3500);
    }

    function showAlert(msg, type = "warning") {
        if (!elements.alertBanner || !elements.alertMessage) return;
        elements.alertMessage.textContent = msg;
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

    function setStatus(state, message) {
        if (!elements.statusPill || !elements.statusText || !elements.statusDot) return;
        elements.statusPill.className = "map-status-pill";
        elements.statusDot.className = "pulsing-dot";

        if (state === "LIVE") {
            elements.statusPill.classList.add("pill-live");
            elements.statusDot.classList.add("dot-live");
            elements.statusText.textContent = message || "LIVE • BROADCASTING";
        } else if (state === "WAITING") {
            elements.statusPill.classList.add("pill-waiting");
            elements.statusDot.classList.add("dot-waiting");
            elements.statusText.textContent = message || "ACQUIRING GPS LOCK...";
        } else {
            elements.statusPill.classList.add("pill-stopped");
            elements.statusDot.classList.add("dot-stopped");
            elements.statusText.textContent = message || "TRACKING OFFLINE";
        }
    }

    // Check GPS Permissions and Secure Context
    async function checkDeviceCapabilities() {
        // 1. Secure Context Check
        const isSecure = isSecureLocationContext();
        if (elements.valSecureContext) {
            if (isSecure) {
                elements.valSecureContext.innerHTML = '<span style="color: #10b981; font-weight: 700;">🟢 Secure Context</span>';
                if (elements.insecureContextAlert) elements.insecureContextAlert.style.display = "none";
            } else {
                elements.valSecureContext.innerHTML = '<span style="color: #ef4444; font-weight: 700;">🔴 Insecure (HTTP Remote)</span>';
                if (elements.insecureContextAlert) elements.insecureContextAlert.style.display = "block";
                showAlert("GPS requires a secure browser context. Open this page over HTTPS (or use localhost) before starting live tracking.", "danger");
            }
        }

        // 2. Geolocation Permission Query
        if (navigator.permissions && navigator.permissions.query) {
            try {
                const status = await navigator.permissions.query({ name: "geolocation" });
                updatePermissionDisplay(status.state);
                status.onchange = function () {
                    updatePermissionDisplay(this.state);
                };
            } catch (e) {
                if (elements.valGpsPermission) elements.valGpsPermission.textContent = "Supported";
            }
        } else if (navigator.geolocation) {
            if (elements.valGpsPermission) elements.valGpsPermission.textContent = "Supported";
        } else {
            if (elements.valGpsPermission) {
                elements.valGpsPermission.innerHTML = '<span style="color: #ef4444;">Not Supported</span>';
            }
        }
    }

    function updatePermissionDisplay(state) {
        if (!elements.valGpsPermission) return;
        if (state === "granted") {
            elements.valGpsPermission.innerHTML = '<span style="color: #10b981; font-weight: 700;">🟢 Granted</span>';
        } else if (state === "prompt") {
            elements.valGpsPermission.innerHTML = '<span style="color: #f59e0b; font-weight: 700;">🟡 Prompt (Ask on start)</span>';
        } else if (state === "denied") {
            elements.valGpsPermission.innerHTML = '<span style="color: #ef4444; font-weight: 700;">🔴 Denied (Blocked)</span>';
        }
    }

    function isSecureLocationContext() {
        return window.isSecureContext ||
            window.location.hostname === "localhost" ||
            window.location.hostname === "127.0.0.1";
    }

    function isValidCoordinates(latitude, longitude) {
        return Number.isFinite(latitude) && Number.isFinite(longitude) &&
            latitude >= -90 && latitude <= 90 &&
            longitude >= -180 && longitude <= 180;
    }

    // Initialize Leaflet Map
    function initMap() {
        if (map) return;
        const defaultLat = 15.9129;
        const defaultLng = 79.7400;

        map = L.map("trackingMap", {
            zoomControl: true,
            scrollWheelZoom: true,
        }).setView([defaultLat, defaultLng], 6);

        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors',
        }).addTo(map);

        // When user manually drags map, pause follow mode
        map.on("dragstart", function () {
            if (isFollowActive) {
                setFollowMode(false, true);
            }
        });
    }

    // Custom Vehicle Icon
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

    // Smooth Marker Animation
    function animateMarkerTo(marker, newLatLng, duration = 1200) {
        if (!marker) return;
        const startLatLng = marker.getLatLng();
        if (!startLatLng) {
            marker.setLatLng(newLatLng);
            return;
        }

        const deltaLat = newLatLng[0] - startLatLng.lat;
        const deltaLng = newLatLng[1] - startLatLng.lng;

        if (markerAnimFrame) {
            cancelAnimationFrame(markerAnimFrame);
        }

        const startTime = performance.now();

        function step(now) {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1.0);
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

    // Follow Mode Toggle
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

    // Start Live Location Broadcasting
    async function startLiveLocation() {
        if (!navigator.geolocation) {
            showAlert("Location is unavailable because this browser does not support GPS geolocation.", "danger");
            return;
        }

        if (!isSecureLocationContext()) {
            showAlert("GPS requires a secure browser context. Open this page over HTTPS (or use localhost) and try again.", "danger");
            setStatus("STOPPED", "HTTPS REQUIRED FOR GPS");
            return;
        }

        const vehicleId = elements.vehicleSelect ? elements.vehicleSelect.value : null;
        if (!vehicleId) {
            showAlert("Please select a vehicle to track.", "danger");
            return;
        }

        const bookingId = elements.bookingSelect ? elements.bookingSelect.value || null : null;
        const regNumber = elements.regNumberInput ? elements.regNumberInput.value.trim() : "";

        elements.btnStart.disabled = true;
        elements.btnStart.textContent = "Starting GPS...";
        showAlert("Requesting GPS permission from browser...", "warning");
        setStatus("WAITING", "REQUESTING PERMISSION");

        try {
            const csrf = getCsrfToken();
            const res = await fetch("/live-tracking/api/session/start", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrf,
                },
                body: JSON.stringify({
                    csrf_token: csrf,
                    vehicle_id: vehicleId,
                    booking_id: bookingId,
                    registration_number: regNumber,
                }),
            });

            const data = await res.json();
            if (!res.ok || !data.success) {
                showAlert(data.error || "Failed to start tracking session.", "danger");
                elements.btnStart.disabled = false;
                elements.btnStart.textContent = "🛰️ Start Live Tracking";
                setStatus("STOPPED", "ERROR STARTING");
                return;
            }

            activeSession = data.session;
            // Update UI for active session
            elements.btnStart.style.display = "none";
            elements.btnStop.style.display = "flex";
            elements.btnStop.disabled = false;

            if (elements.vehicleSelect) elements.vehicleSelect.disabled = true;
            if (elements.bookingSelect) elements.bookingSelect.disabled = true;
            if (elements.regNumberInput) elements.regNumberInput.disabled = true;

            // Display shareable link
            if (elements.shareBox && elements.shareInput && activeSession.customer_url) {
                elements.shareInput.value = activeSession.customer_url;
                elements.shareBox.style.display = "block";
            }

            showAlert("Tracking session started. Allow location access and wait for a GPS fix; coordinates will appear here once received.", "warning");
            setStatus("WAITING", "ACQUIRING SATELLITE FIX...");
            if (elements.valGpsLock) elements.valGpsLock.textContent = "Waiting for GPS coordinates...";

            // Start Geolocation Watcher
            const geoOptions = {
                enableHighAccuracy: true,
                timeout: 15000,
                maximumAge: 0,
            };

            watchId = navigator.geolocation.watchPosition(onPositionSuccess, onPositionError, geoOptions);
            showToast("🛰️ Live GPS Broadcasting Started!");

        } catch (err) {
            showAlert("Network error communicating with server: " + err.message, "danger");
            elements.btnStart.disabled = false;
            elements.btnStart.textContent = "🛰️ Start Live Tracking";
            setStatus("STOPPED", "NETWORK ERROR");
        }
    }

    // Geolocation Success Callback
    function onPositionSuccess(position) {
        const coords = position.coords;
        const lat = coords.latitude;
        const lng = coords.longitude;
        if (!isValidCoordinates(lat, lng)) {
            showAlert("The device returned invalid GPS coordinates. Waiting for a valid location fix.", "danger");
            setStatus("WAITING", "INVALID GPS COORDINATES");
            if (elements.valGpsLock) elements.valGpsLock.textContent = "Invalid coordinates";
            return;
        }

        const accuracy = coords.accuracy;
        if (typeof accuracy !== "number" || !Number.isFinite(accuracy) || accuracy < 0) {
            showAlert("The device returned invalid GPS accuracy. Waiting for a valid location fix.", "danger");
            setStatus("WAITING", "INVALID GPS ACCURACY");
            if (elements.valGpsLock) elements.valGpsLock.textContent = "Invalid GPS accuracy";
            return;
        }
        const speed = Number.isFinite(coords.speed) ? (coords.speed * 3.6).toFixed(1) : null; // km/h
        const heading = coords.heading;
        const altitude = coords.altitude;

        if (elements.valGpsLock) elements.valGpsLock.textContent = "GPS fix received; sending to server...";
        if (elements.valGpsPermission) elements.valGpsPermission.innerHTML = '<span style="color: #10b981; font-weight: 700;">🟢 Granted</span>';
        setStatus("WAITING", "GPS FIX RECEIVED • SAVING");

        // Update Telemetry Display
        if (elements.valLat) elements.valLat.textContent = lat.toFixed(5);
        if (elements.valLng) elements.valLng.textContent = lng.toFixed(5);
        if (elements.valAccuracy) {
        elements.valAccuracy.textContent = `± ${Math.round(accuracy)} m`;
        }
        if (elements.valSpeed) elements.valSpeed.textContent = speed !== null ? `${speed} km/h` : "0 km/h";

        updateCount++;
        if (elements.valUpdates) elements.valUpdates.textContent = updateCount;

        // Update Map Display
        initMap();
        const currentLatLng = [lat, lng];

        if (!vehicleMarker) {
            vehicleMarker = L.marker(currentLatLng, {
                icon: getVehicleIcon(heading),
                zIndexOffset: 1000,
            }).addTo(map);
        } else {
            animateMarkerTo(vehicleMarker, currentLatLng, 1200);
            vehicleMarker.setIcon(getVehicleIcon(heading));
        }

        vehicleMarker.bindPopup(`
            <div style="font-size: 13px;">
                <b style="color: #146b5b;">🚚 Live Vehicle Position</b><br>
                <b>Lat:</b> ${lat.toFixed(5)}<br>
                <b>Lng:</b> ${lng.toFixed(5)}<br>
                <b>Accuracy:</b> ±${Math.round(accuracy)}m<br>
                <b>Speed:</b> ${speed ? speed + ' km/h' : 'Stationary'}
            </div>
        `);

        // Draw accuracy circle
        if (!accuracyCircle) {
            accuracyCircle = L.circle(currentLatLng, {
                radius: accuracy,
                color: "#10b981",
                fillColor: "#10b981",
                fillOpacity: 0.15,
                weight: 1.5,
            }).addTo(map);
        } else {
            accuracyCircle.setLatLng(currentLatLng);
            accuracyCircle.setRadius(accuracy);
        }

        // Center on first update
        if (updateCount === 1) {
            map.setView(currentLatLng, 16);
        }

        // Transmit to Backend (Throttled)
        const now = Date.now();
        if (
            activeSession &&
            !transmissionInProgress &&
            now - lastTransmissionTime >= TRANSMIT_THROTTLE_MS
        ) {
            void sendLocationUpdate(lat, lng, accuracy, coords.speed, heading, altitude);
        }
    }

    // Transmit coordinates to Backend
    async function sendLocationUpdate(lat, lng, accuracy, speed, heading, altitude) {
        if (!activeSession || !activeSession.session_token || transmissionInProgress) return;

        transmissionInProgress = true;
        try {
            const csrf = getCsrfToken();
            if (!csrf) {
                throw new Error("CSRF token is unavailable. Reload this page and try again.");
            }
            const res = await fetch("/live-tracking/api/location/update", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrf,
                },
                body: JSON.stringify({
                    csrf_token: csrf,
                    session_token: activeSession.session_token,
                    latitude: lat,
                    longitude: lng,
                    accuracy: accuracy,
                    speed: speed,
                    heading: heading,
                    altitude: altitude,
                }),
            });

            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.success) {
                const message = data.error || `The server rejected the GPS update (HTTP ${res.status}).`;
                showAlert(`GPS coordinates were received, but the server did not save them: ${message}`, "danger");
                console.error("GPS update failed:", { status: res.status, error: message });
                setStatus("WAITING", "GPS FIX • UPDATE FAILED");
                if (elements.valGpsLock) elements.valGpsLock.textContent = "Server did not save location";
                return;
            }

            lastTransmissionTime = Date.now();
            const nowTime = new Date().toLocaleTimeString();
            if (elements.valLastSent) elements.valLastSent.textContent = nowTime;
            if (elements.valGpsLock) elements.valGpsLock.innerHTML = '<span style="color: #10b981; font-weight: 700;">🟢 Sent to server</span>';
            hideAlert();
            setStatus("LIVE", "LIVE • BROADCASTING");
        } catch (err) {
            const message = err instanceof TypeError
                ? "Network error while sending GPS coordinates. Check your connection and try again."
                : err.message;
            console.error("GPS update request failed:", message);
            showAlert(`GPS coordinates were received, but the update failed: ${message}`, "danger");
            setStatus("WAITING", "GPS FIX • UPDATE FAILED");
            if (elements.valGpsLock) elements.valGpsLock.textContent = "Update failed";
        } finally {
            transmissionInProgress = false;
        }
    }

    // Geolocation Error Callback
    function onPositionError(error) {
        let errorMsg = "Unable to retrieve GPS location.";
        if (!isSecureLocationContext()) {
            errorMsg = "GPS is blocked because this page is not in a secure context. Open it over HTTPS and allow location access.";
        } else switch (error.code) {
            case error.PERMISSION_DENIED:
                errorMsg = "GPS Permission Denied. Please allow location access in your browser or mobile settings to broadcast live position.";
                if (elements.valGpsPermission) elements.valGpsPermission.innerHTML = '<span style="color: #ef4444; font-weight: 700;">🔴 Denied</span>';
                break;
            case error.POSITION_UNAVAILABLE:
                errorMsg = "GPS Position Unavailable. Please ensure location services are enabled on your device.";
                break;
            case error.TIMEOUT:
                errorMsg = "GPS Request Timed Out. Retrying to acquire satellite lock...";
                break;
        }

        showAlert(errorMsg, error.code === error.PERMISSION_DENIED ? "danger" : "warning");
        setStatus(error.code === error.PERMISSION_DENIED ? "STOPPED" : "WAITING", "GPS ERROR");
        if (elements.valGpsLock) elements.valGpsLock.textContent = "No GPS coordinates received";
    }

    // Stop Live Location
    async function stopLiveLocation() {
        if (watchId !== null) {
            navigator.geolocation.clearWatch(watchId);
            watchId = null;
        }

        if (activeSession) {
            try {
                const csrf = getCsrfToken();
                const response = await fetch("/live-tracking/api/session/stop", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": csrf,
                    },
                    body: JSON.stringify({
                        csrf_token: csrf,
                        session_token: activeSession.session_token,
                    }),
                });
                const result = await response.json();
                if (!response.ok || !result.success) {
                    showAlert(result.error || "The server could not stop this tracking session.", "danger");
                    setStatus("WAITING", "STOP FAILED • RETRY");
                    return;
                }
            } catch (err) {
                console.error("Error stopping tracking session:", err);
                showAlert(`Could not stop tracking on the server: ${err.message}`, "danger");
                setStatus("WAITING", "STOP FAILED • RETRY");
                return;
            }
        }

        activeSession = null;

        // Reset UI
        elements.btnStart.style.display = "flex";
        elements.btnStart.disabled = false;
        elements.btnStart.textContent = "🛰️ Start Live Tracking";
        elements.btnStop.style.display = "none";

        if (elements.vehicleSelect) elements.vehicleSelect.disabled = false;
        if (elements.bookingSelect) elements.bookingSelect.disabled = false;
        if (elements.regNumberInput) elements.regNumberInput.disabled = false;

        setStatus("STOPPED", "TRACKING STOPPED");
        showAlert("Live location sharing has been stopped.", "success");
        showToast("⏹️ Live Location Sharing Stopped");
        if (elements.valGpsLock) elements.valGpsLock.textContent = "Offline";

        if (accuracyCircle && map) {
            map.removeLayer(accuracyCircle);
            accuracyCircle = null;
        }
    }

    // Recenter Map on Current Position
    function recenterMap() {
        if (!map || !vehicleMarker) return;
        setFollowMode(true);
        const pos = vehicleMarker.getLatLng();
        map.setView(pos, 16);
    }

    // Copy Customer Tracking Link
    function copyTrackingLink() {
        if (!elements.shareInput) return;
        elements.shareInput.select();
        elements.shareInput.setSelectionRange(0, 99999);
        navigator.clipboard.writeText(elements.shareInput.value).then(() => {
            showToast("📋 Customer tracking link copied to clipboard!");
            if (elements.btnCopy) elements.btnCopy.textContent = "Copied! ✓";
            setTimeout(() => {
                if (elements.btnCopy) elements.btnCopy.textContent = "Copy Link";
            }, 2500);
        }).catch(() => {
            document.execCommand("copy");
            showToast("📋 Copied to clipboard!");
        });
    }

    // Event Listeners
    function setupEvents() {
        if (elements.btnStart) elements.btnStart.addEventListener("click", startLiveLocation);
        if (elements.btnStop) elements.btnStop.addEventListener("click", stopLiveLocation);
        if (elements.btnToggleFollow) {
            elements.btnToggleFollow.addEventListener("click", function () {
                setFollowMode(!isFollowActive);
            });
        }
        if (elements.btnRecenter) elements.btnRecenter.addEventListener("click", recenterMap);
        if (elements.btnCopy) elements.btnCopy.addEventListener("click", copyTrackingLink);

        // When booking is selected, auto-select associated vehicle
        if (elements.bookingSelect && elements.vehicleSelect) {
            elements.bookingSelect.addEventListener("change", function () {
                const selectedOption = this.options[this.selectedIndex];
                const vehicleId = selectedOption.getAttribute("data-vehicle-id");
                if (vehicleId) {
                    elements.vehicleSelect.value = vehicleId;
                }
            });
        }

        window.addEventListener("beforeunload", () => {
            if (watchId !== null) {
                navigator.geolocation.clearWatch(watchId);
            }
            if (markerAnimFrame) {
                cancelAnimationFrame(markerAnimFrame);
            }
        });
    }

    // Initialize on DOM Ready
    document.addEventListener("DOMContentLoaded", function () {
        initMap();
        setupEvents();
        checkDeviceCapabilities();

        // Check if there was an existing active session in session storage or template
        const serverActiveSessionEl = document.getElementById("serverActiveSession");
        let existing = null;

        if (serverActiveSessionEl && serverActiveSessionEl.dataset.session) {
            try {
                existing = JSON.parse(serverActiveSessionEl.dataset.session);
            } catch (e) {}
        }

        if (existing && existing.session_token) {
            activeSession = existing;
            activeSession.customer_url = activeSession.customer_url || "";
            if (elements.btnStart) elements.btnStart.style.display = "none";
            if (elements.btnStop) elements.btnStop.style.display = "flex";
            if (elements.vehicleSelect) {
                elements.vehicleSelect.value = activeSession.vehicle_id;
                elements.vehicleSelect.disabled = true;
            }
            if (elements.bookingSelect && activeSession.booking_id) {
                elements.bookingSelect.value = activeSession.booking_id;
                elements.bookingSelect.disabled = true;
            }
            if (elements.regNumberInput && activeSession.registration_number) {
                elements.regNumberInput.value = activeSession.registration_number;
                elements.regNumberInput.disabled = true;
            }
            if (elements.shareBox && elements.shareInput && activeSession.customer_url) {
                elements.shareInput.value = activeSession.customer_url;
                elements.shareBox.style.display = "block";
            }
            showAlert("Resuming existing active tracking session...", "warning");
            setStatus("WAITING", "ACQUIRING GPS LOCK...");
            if (elements.valGpsLock) elements.valGpsLock.textContent = "Resuming...";

            const geoOptions = {
                enableHighAccuracy: true,
                timeout: 15000,
                maximumAge: 0,
            };
            if (!isSecureLocationContext()) {
                showAlert("The existing tracking session cannot receive GPS in this insecure context. Open this page over HTTPS to resume location sharing.", "danger");
                setStatus("WAITING", "HTTPS REQUIRED FOR GPS");
                if (elements.valGpsLock) elements.valGpsLock.textContent = "Secure context required";
            } else if (!navigator.geolocation) {
                showAlert("The browser does not support GPS geolocation. Open this page on a GPS-capable device.", "danger");
                setStatus("WAITING", "GPS NOT SUPPORTED");
            } else {
                watchId = navigator.geolocation.watchPosition(onPositionSuccess, onPositionError, geoOptions);
            }
        }
    });

})();
