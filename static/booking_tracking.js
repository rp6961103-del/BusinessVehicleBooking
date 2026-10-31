(function () {
    "use strict";

    const POLL_INTERVAL_MS = 5000;
    const trackers = new Map();

    function statusMessage(data, bookingStatus) {
        const state = data.tracking_state;
        const pending = !["confirmed", "accepted", "rejected"].includes(
            String(data.booking.status || bookingStatus || "").toLowerCase().trim()
        );
        let message;

        if (state === "NOT_STARTED") {
            message = "Live tracking has not started yet.";
        } else if (state === "WAITING_FOR_GPS") {
            message = "The driver has started sharing; waiting for a GPS location.";
        } else if (state === "STOPPED") {
            message = "Live location sharing has been stopped by the driver.";
        } else if (state === "OUTDATED") {
            message = "The last GPS location is outdated.";
        } else if (state === "LIVE") {
            message = "Live location is updating.";
        } else {
            message = "Vehicle location is currently unavailable.";
        }

        return pending ? `Awaiting owner confirmation. ${message}` : message;
    }

    function updateTracker(tracker, data) {
        if (!data.booking || String(data.booking.id) !== tracker.bookingId) {
            tracker.status.textContent = "Unable to verify tracking data for this booking.";
            tracker.updated.textContent = "Not available";
            tracker.position.textContent = "Latest GPS position: not available";
            return;
        }

        tracker.status.textContent = statusMessage(data, tracker.bookingStatus);
        tracker.updated.textContent = data.last_updated || "Not updated";

        const location = data.location;
        if (
            !location ||
            location.latitude === null ||
            location.latitude === undefined ||
            location.longitude === null ||
            location.longitude === undefined ||
            !Number.isFinite(Number(location.latitude)) ||
            !Number.isFinite(Number(location.longitude))
        ) {
            tracker.position.textContent = "Latest GPS position: not available";
            tracker.mapElement.hidden = true;
            tracker.placeholder.hidden = false;
            return;
        }

        const position = [Number(location.latitude), Number(location.longitude)];
        tracker.position.textContent =
            `Latest GPS position: ${position[0].toFixed(6)}, ${position[1].toFixed(6)}` +
            (location.accuracy !== null &&
            location.accuracy !== undefined &&
            Number.isFinite(Number(location.accuracy))
                ? ` (accuracy ±${Number(location.accuracy).toFixed(0)} m)`
                : "");
        tracker.placeholder.hidden = true;
        tracker.mapElement.hidden = false;

        if (!tracker.map) {
            tracker.map = L.map(tracker.mapElement).setView(position, 15);
            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            }).addTo(tracker.map);
            tracker.marker = L.marker(position).addTo(tracker.map);
        } else {
            tracker.marker.setLatLng(position);
            if (data.tracking_state === "LIVE") {
                tracker.map.panTo(position);
            }
        }

        tracker.map.invalidateSize();
    }

    async function pollTracker(tracker) {
        if (tracker.inFlight) return;
        tracker.inFlight = true;
        try {
            const response = await fetch(
                `/live-tracking/api/location/poll?booking_id=${encodeURIComponent(tracker.bookingId)}`,
                { credentials: "same-origin" }
            );
            const result = await response.json();
            if (!response.ok || !result.success || !result.data) {
                tracker.status.textContent = response.status === 403
                    ? "You are not authorized to view this booking’s tracking."
                    : "Tracking information is temporarily unavailable.";
                return;
            }
            updateTracker(tracker, result.data);
        } catch (error) {
            tracker.status.textContent = "Unable to refresh tracking information. Retrying shortly.";
        } finally {
            tracker.inFlight = false;
        }
    }

    function closeTracker(tracker) {
        tracker.row.hidden = true;
        tracker.button.setAttribute("aria-expanded", "false");
        if (tracker.timer) {
            window.clearInterval(tracker.timer);
            tracker.timer = null;
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll("[data-booking-tracking]").forEach(function (panel) {
            const bookingId = panel.dataset.bookingId;
            trackers.set(bookingId, {
                bookingId: bookingId,
                bookingStatus: panel.dataset.bookingStatus,
                button: Array.from(document.querySelectorAll(".booking-tracking-toggle"))
                    .find(function (toggle) { return toggle.dataset.bookingId === bookingId; }),
                row: panel.closest(".booking-tracking-row"),
                status: panel.querySelector("[data-tracking-status]"),
                updated: panel.querySelector("[data-tracking-updated]"),
                position: panel.querySelector("[data-tracking-position]"),
                placeholder: panel.querySelector("[data-tracking-placeholder]"),
                mapElement: panel.querySelector("[data-tracking-map]"),
                map: null,
                marker: null,
                timer: null,
                inFlight: false,
            });
        });

        document.querySelectorAll(".booking-tracking-toggle").forEach(function (button) {
            button.addEventListener("click", function () {
                const tracker = trackers.get(button.dataset.bookingId);
                if (!tracker) return;

                if (!tracker.row.hidden) {
                    closeTracker(tracker);
                    return;
                }

                tracker.row.hidden = false;
                button.setAttribute("aria-expanded", "true");
                pollTracker(tracker);
                tracker.timer = window.setInterval(
                    function () { pollTracker(tracker); },
                    POLL_INTERVAL_MS
                );
            });
        });
    });
})();
