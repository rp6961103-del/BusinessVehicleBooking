const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function createTrackingHarness(initialData, { routeFails = false } = {}) {
    const elements = new Map();
    const markers = [];
    const polylines = [];
    const routeRequests = [];
    const tileLayers = [];
    const intervals = [];
    let pollData = initialData;
    let readyCallback;
    const map = {
        setView() { return this; },
        on() {},
        panTo() {},
        fitBounds() {},
        invalidateSize() {},
        removeLayer(layer) { layer.removed = true; },
    };

    function element(id) {
        if (!elements.has(id)) {
            elements.set(id, {
                id,
                dataset: {},
                style: {},
                hidden: false,
                textContent: "",
                className: "",
                attributes: {},
                getBoundingClientRect() { return { width: 800, height: 600 }; },
                classList: {
                    add() {},
                    remove() {},
                },
                addEventListener() {},
                setAttribute(name, value) { this.attributes[name] = value; },
            });
        }
        return elements.get(id);
    }

    element("trackingMeta").dataset.bookingId = String(initialData.booking.id);
    element("initialTrackingData").dataset.json = JSON.stringify(initialData);

    const document = {
        head: { appendChild() {} },
        createElement() {
            return { set src(value) { this.source = value; }, set integrity(value) { this.hash = value; } };
        },
        getElementById: element,
        addEventListener(name, callback) {
            if (name === "DOMContentLoaded") readyCallback = callback;
        },
        documentElement: {},
    };

    const leaflet = {
        map: (container) => {
            assert.equal(container.id, "trackingMap");
            return map;
        },
        tileLayer: (url, options) => {
            const layer = {
                url,
                options,
                handlers: {},
                on(name, callback) { this.handlers[name] = callback; return this; },
                addTo() { return this; },
            };
            tileLayers.push(layer);
            return layer;
        },
        divIcon: (options) => options,
        marker: (coords) => {
            let popup = null;
            let tooltip = null;
            let latLng = { lat: coords[0], lng: coords[1] };
            const marker = {
                addTo() { markers.push(this); return this; },
                bindPopup(content) { popup = content; return this; },
                bindTooltip(content) { tooltip = content; return this; },
                setPopupContent(content) { popup = content; return this; },
                getPopup() { return popup === null ? null : {}; },
                setLatLng(next) { latLng = { lat: next[0], lng: next[1] }; return this; },
                setIcon() {},
                getLatLng() { return latLng; },
                getTooltip() { return tooltip; },
            };
            return marker;
        },
        circle: () => ({
            addTo() { return this; },
            setLatLng() {},
            setRadius() {},
        }),
        polyline: (coordinates, options) => {
            const route = {
                coordinates,
                options,
                addTo() { polylines.push(this); return this; },
                bindPopup() { return this; },
                getBounds() { return {}; },
            };
            return route;
        },
        latLngBounds: () => ({}),
    };

    const window = {
        location: { search: "", hostname: "localhost" },
        setTimeout: () => 1,
        clearTimeout() {},
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        setInterval: (callback) => {
            intervals.push(callback);
            return intervals.length;
        },
        clearInterval() {},
        addEventListener() {},
    };

    const fetch = async (input) => {
        const url = new URL(String(input), "https://booking.example");
        if (url.pathname.endsWith("/api/route")) {
            routeRequests.push(url);
            if (routeFails) return { ok: false, status: 502, json: async () => ({}) };
            const start = [Number(url.searchParams.get("start_lat")), Number(url.searchParams.get("start_lng"))];
            const destination = [Number(url.searchParams.get("dest_lat")), Number(url.searchParams.get("dest_lng"))];
            const isBookedRoute = start[0] === initialData.booking.pickup_coords[0] &&
                start[1] === initialData.booking.pickup_coords[1];
            return {
                ok: true,
                json: async () => ({
                    success: true,
                    route: {
                        coordinates: [start, destination],
                        distance_meters: isBookedRoute ? 423358 : 44000,
                        duration_seconds: isBookedRoute ? 21260 : 3600,
                    },
                }),
            };
        }
        if (url.hostname === "router.project-osrm.org") {
            return { ok: !routeFails, status: 502, json: async () => ({}) };
        }
        return {
            ok: true,
            json: async () => ({ success: true, data: pollData }),
        };
    };

    const sourcePath = path.join(__dirname, "..", "static", "customer_tracking.js");
    vm.runInNewContext(fs.readFileSync(sourcePath, "utf8"), {
        document,
        window,
        fetch,
        L: leaflet,
        URLSearchParams,
        AbortController,
        Date,
        Number,
        Math,
        Promise,
        console,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            callback(1200);
            return 1;
        },
        cancelAnimationFrame() {},
        setInterval: window.setInterval,
        clearInterval: window.clearInterval,
    });

    return {
        elements,
        markers,
        polylines,
        routeRequests,
        tileLayers,
        start() { readyCallback(); },
        setPollingData(data) { pollData = data; },
        async poll() { await intervals[0](); },
    };
}

function trackingPayload(state, secondsAgo, booking = {}) {
    const pickupCoords = Object.hasOwn(booking, "pickup_coords")
        ? booking.pickup_coords
        : [9.9261153, 78.1140983];
    const destinationCoords = Object.hasOwn(booking, "destination_coords")
        ? booking.destination_coords
        : [12.9767936, 77.590082];
    return {
        tracking_state: state,
        seconds_ago: secondsAgo,
        location: state === "NOT_STARTED" ? null : { latitude: 9.9252, longitude: 78.1198, accuracy: 6 },
        vehicle: { name: `Vehicle ${booking.id || 26}`, registration_number: "AP04" },
        booking: {
            id: booking.id || 26,
            status: "Accepted",
            pickup: booking.pickup || "Madurai",
            destination: booking.destination || "Bangalore",
            pickup_coords: pickupCoords,
            destination_coords: destinationCoords,
        },
    };
}

async function settlePromises() {
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
}

test("full-screen map routes the booked pickup to destination and keeps live GPS separate", async () => {
    const payload = trackingPayload("LIVE", 5);
    const harness = createTrackingHarness(payload);
    harness.start();
    await settlePromises();

    assert.equal(harness.elements.get("statusText").textContent, "LIVE • ON TRIP");
    assert.equal(harness.routeRequests[0].searchParams.get("start_lat"), String(payload.booking.pickup_coords[0]));
    assert.equal(harness.routeRequests[0].searchParams.get("start_lng"), String(payload.booking.pickup_coords[1]));
    assert.equal(harness.routeRequests[0].searchParams.get("dest_lat"), String(payload.booking.destination_coords[0]));
    assert.equal(harness.routeRequests[0].searchParams.get("dest_lng"), String(payload.booking.destination_coords[1]));
    assert.equal(JSON.stringify(harness.polylines[0].coordinates[0]), JSON.stringify(payload.booking.pickup_coords));
    assert.equal(JSON.stringify(harness.polylines[0].coordinates.at(-1)), JSON.stringify(payload.booking.destination_coords));
    assert.equal(harness.polylines[0].options.color, "#1a73e8");
    assert.equal(harness.markers.length, 3);
    assert.deepEqual(harness.markers[0].getLatLng(), {
        lat: payload.booking.pickup_coords[0],
        lng: payload.booking.pickup_coords[1],
    });
    assert.deepEqual(harness.markers[1].getLatLng(), {
        lat: payload.booking.destination_coords[0],
        lng: payload.booking.destination_coords[1],
    });
    assert.deepEqual(harness.markers[2].getLatLng(), { lat: 9.9252, lng: 78.1198 });
    assert.equal(harness.elements.get("valRemainingDistance").textContent, "44.0 km");
    assert.equal(harness.elements.get("valEstDuration").textContent, "60 mins");
    assert.equal(harness.elements.get("routeDistanceLabel").textContent, "Remaining road distance");
    assert.equal(harness.tileLayers[0].url, "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png");
    assert.match(harness.tileLayers[0].options.attribution, /OpenStreetMap/);
    assert.equal(harness.tileLayers[0].options.maxZoom, 19);
});

test("two booking IDs request their own exact booked route endpoints", async () => {
    const bookings = [
        trackingPayload("NOT_STARTED", null),
        trackingPayload("NOT_STARTED", null, {
            id: 4,
            pickup: "Kalyandurgam",
            destination: "Ananthapuram",
            pickup_coords: [14.5517958, 77.1119702],
            destination_coords: [13.1673168, 79.2128484],
        }),
    ];

    for (const payload of bookings) {
        const harness = createTrackingHarness(payload);
        harness.start();
        await settlePromises();
        assert.equal(harness.routeRequests.length, 1);
        assert.equal(
            Number(harness.routeRequests[0].searchParams.get("start_lat")),
            payload.booking.pickup_coords[0]
        );
        assert.equal(
            Number(harness.routeRequests[0].searchParams.get("start_lng")),
            payload.booking.pickup_coords[1]
        );
        assert.equal(
            Number(harness.routeRequests[0].searchParams.get("dest_lat")),
            payload.booking.destination_coords[0]
        );
        assert.equal(
            Number(harness.routeRequests[0].searchParams.get("dest_lng")),
            payload.booking.destination_coords[1]
        );
        assert.equal(JSON.stringify(harness.polylines[0].coordinates[0]), JSON.stringify(payload.booking.pickup_coords));
        assert.equal(JSON.stringify(harness.polylines[0].coordinates.at(-1)), JSON.stringify(payload.booking.destination_coords));
    }
});

test("live polling moves the existing vehicle marker and refreshes the remaining road route", async () => {
    const initialData = trackingPayload("LIVE", 5);
    const harness = createTrackingHarness(initialData);
    harness.start();
    await settlePromises();

    const updatedData = trackingPayload("LIVE", 1);
    updatedData.location = { latitude: 9.95, longitude: 78.14, accuracy: 5 };
    harness.setPollingData(updatedData);
    await harness.poll();
    await settlePromises();

    assert.equal(harness.markers.length, 3);
    assert.deepEqual(harness.markers[2].getLatLng(), { lat: 9.95, lng: 78.14 });
    assert.equal(harness.routeRequests.length, 3);
    assert.equal(harness.routeRequests[2].searchParams.get("start_lat"), "9.95");
    assert.equal(harness.routeRequests[2].searchParams.get("start_lng"), "78.14");
    assert.equal(harness.polylines.filter((route) => !route.removed).length, 2);
});

test("stale GPS remains marked as last reported while the booked route remains visible", async () => {
    const harness = createTrackingHarness(trackingPayload("OUTDATED", 720));
    harness.start();
    await settlePromises();

    assert.equal(harness.elements.get("statusText").textContent, "LOCATION OUTDATED");
    assert.equal(harness.elements.get("routeBasis").hidden, false);
    assert.equal(harness.elements.get("telemetryLastUpdated").textContent, "Location last updated 12 mins ago");
    assert.equal(harness.polylines.length, 1);
    assert.equal(harness.polylines[0].options.color, "#1a73e8");
    assert.equal(harness.elements.get("routeDistanceLabel").textContent, "Booked road distance");
});

test("missing endpoint coordinates show an honest error and do not geocode or route guessed points", async () => {
    const payload = trackingPayload("NOT_STARTED", null, {
        pickup_coords: null,
        destination_coords: null,
    });
    const harness = createTrackingHarness(payload);
    harness.start();
    await settlePromises();

    assert.equal(harness.routeRequests.length, 0);
    assert.equal(harness.polylines.length, 0);
    assert.match(
        harness.elements.get("routeMessage").textContent,
        /could not be resolved confidently/i
    );
});

test("routing failures show an unavailable message without drawing a fake line", async () => {
    const harness = createTrackingHarness(trackingPayload("LIVE", 5), { routeFails: true });
    harness.start();
    await settlePromises();

    assert.equal(
        harness.elements.get("valRemainingDistance").textContent,
        "Route distance temporarily unavailable"
    );
    assert.equal(harness.polylines.length, 0);
});
