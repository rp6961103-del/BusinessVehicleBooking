const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function createBrowserHarness(fetchImpl, secureContext = true) {
    const elements = new Map();
    const listeners = new Map();
    let readyCallback;
    let positionSuccess;
    let positionError;

    function makeElement(id) {
        return {
            id,
            textContent: "",
            innerHTML: "",
            value: id === "vehicleSelect" ? "3" : "",
            disabled: false,
            style: {},
            classList: { add() {}, remove() {} },
            addEventListener(name, handler) {
                listeners.set(`${id}:${name}`, handler);
            },
            setAttribute() {},
            select() {},
            setSelectionRange() {},
            options: [],
            selectedIndex: 0,
        };
    }

    const document = {
        getElementById(id) {
            if (id === "serverActiveSession") return null;
            if (!elements.has(id)) elements.set(id, makeElement(id));
            return elements.get(id);
        },
        querySelector() {
            return { getAttribute: () => "test-csrf" };
        },
        addEventListener(name, handler) {
            if (name === "DOMContentLoaded") readyCallback = handler;
        },
        execCommand() {},
    };
    const map = {
        setView() { return this; },
        on() {},
        removeLayer() {},
    };
    const leaflet = {
        map: () => map,
        tileLayer: () => ({ addTo() { return this; } }),
        divIcon: (options) => options,
        marker: () => ({
            addTo() { return this; },
            bindPopup() { return this; },
            setIcon() {},
            getLatLng: () => ({ lat: 14.5, lng: 77.5 }),
        }),
        circle: () => ({
            addTo() { return this; },
            setLatLng() {},
            setRadius() {},
        }),
    };
    const window = {
        isSecureContext: secureContext,
        location: { hostname: secureContext ? "localhost" : "192.168.0.20" },
        addEventListener() {},
    };
    const navigator = {
        geolocation: {
            watchPosition(success, error) {
                positionSuccess = success;
                positionError = error;
                return 1;
            },
            clearWatch() {},
        },
        permissions: {
            query: async () => ({ state: "prompt" }),
        },
        clipboard: { writeText: async () => {} },
    };

    const sourcePath = path.join(__dirname, "..", "static", "owner_tracking.js");
    const source = fs.readFileSync(sourcePath, "utf8");
    vm.runInNewContext(source, {
        document,
        window,
        navigator,
        fetch: fetchImpl,
        L: leaflet,
        sessionStorage: {
            getItem: () => null,
            setItem() {},
            removeItem() {},
        },
        console,
        setTimeout: () => 0,
        clearTimeout,
        Date,
        Number,
        Math,
        Promise,
        requestAnimationFrame: () => 1,
        cancelAnimationFrame() {},
    });

    return {
        elements,
        listeners,
        window,
        get callbacks() {
            return { readyCallback, positionSuccess, positionError };
        },
    };
}

test("first valid GPS fix is sent and broadcast status waits for API success", async () => {
    let resolveUpdate;
    const updateResponse = new Promise((resolve) => {
        resolveUpdate = resolve;
    });
    const requests = [];
    const harness = createBrowserHarness(async (url, options) => {
        requests.push({ url, options });
        if (url.endsWith("/api/session/start")) {
            return {
                ok: true,
                json: async () => ({
                    success: true,
                    session: {
                        session_token: "driver-session",
                        customer_url: "https://example.test/live",
                        vehicle_id: 3,
                        booking_id: 42,
                    },
                }),
            };
        }
        return updateResponse;
    });

    harness.callbacks.readyCallback();
    await harness.listeners.get("btnStartTracking:click")();
    harness.callbacks.positionSuccess({
        coords: {
            latitude: 14.5,
            longitude: 77.5,
            accuracy: 8,
            speed: 2,
            heading: 90,
            altitude: 400,
        },
    });

    const updateRequest = requests.find(({ url }) => url.endsWith("/api/location/update"));
    assert.ok(updateRequest, "initial fix should be sent immediately");
    assert.equal(updateRequest.options.headers["X-CSRFToken"], "test-csrf");
    assert.deepEqual(JSON.parse(updateRequest.options.body), {
        csrf_token: "test-csrf",
        session_token: "driver-session",
        latitude: 14.5,
        longitude: 77.5,
        accuracy: 8,
        speed: 2,
        heading: 90,
        altitude: 400,
    });
    assert.equal(harness.elements.get("statusText").textContent, "GPS FIX RECEIVED • SAVING");

    resolveUpdate({
        ok: true,
        status: 200,
        json: async () => ({
            success: true,
            data: { latitude: 14.5, longitude: 77.5 },
        }),
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(harness.elements.get("statusText").textContent, "LIVE • BROADCASTING");
    assert.match(harness.elements.get("valGpsLock").innerHTML, /Sent to server/);
});

test("invalid GPS coordinates do not appear as a successful fix", async () => {
    const harness = createBrowserHarness(async () => ({
        ok: true,
        json: async () => ({
            success: true,
            session: { session_token: "driver-session", vehicle_id: 3 },
        }),
    }));

    harness.callbacks.readyCallback();
    await harness.listeners.get("btnStartTracking:click")();
    harness.callbacks.positionSuccess({
        coords: {
            latitude: 91,
            longitude: 77.5,
            accuracy: 8,
            speed: null,
            heading: null,
            altitude: null,
        },
    });

    assert.equal(harness.elements.get("statusText").textContent, "INVALID GPS COORDINATES");
    assert.equal(harness.elements.get("valGpsLock").textContent, "Invalid coordinates");
    assert.equal(harness.elements.get("telemetryLat").textContent, "");
});

test("zero GPS accuracy is sent without being treated as unavailable", async () => {
    const requests = [];
    const harness = createBrowserHarness(async (url, options) => {
        requests.push({ url, options });
        if (url.endsWith("/api/session/start")) {
            return {
                ok: true,
                json: async () => ({
                    success: true,
                    session: { session_token: "driver-session", vehicle_id: 3 },
                }),
            };
        }
        return {
            ok: true,
            status: 200,
            json: async () => ({ success: true, data: { latitude: 14.5, longitude: 77.5 } }),
        };
    });

    harness.callbacks.readyCallback();
    await harness.listeners.get("btnStartTracking:click")();
    harness.callbacks.positionSuccess({
        coords: { latitude: 14.5, longitude: 77.5, accuracy: 0 },
    });
    await new Promise((resolve) => setImmediate(resolve));

    const updateRequest = requests.find(({ url }) => url.endsWith("/api/location/update"));
    assert.ok(updateRequest);
    assert.equal(JSON.parse(updateRequest.options.body).accuracy, 0);
    assert.equal(harness.elements.get("telemetryAccuracy").textContent, "± 0 m");
});

test("missing GPS accuracy is rejected before a location can be broadcast", async () => {
    const requests = [];
    const harness = createBrowserHarness(async (url, options) => {
        requests.push({ url, options });
        if (url.endsWith("/api/session/start")) {
            return {
                ok: true,
                json: async () => ({
                    success: true,
                    session: { session_token: "driver-session", vehicle_id: 3 },
                }),
            };
        }
        return {
            ok: true,
            status: 200,
            json: async () => ({ success: true, data: { latitude: 14.5, longitude: 77.5 } }),
        };
    });

    harness.callbacks.readyCallback();
    await harness.listeners.get("btnStartTracking:click")();
    harness.callbacks.positionSuccess({
        coords: { latitude: 14.5, longitude: 77.5 },
    });
    await new Promise((resolve) => setImmediate(resolve));

    const updateRequest = requests.find(({ url }) => url.endsWith("/api/location/update"));
    assert.equal(updateRequest, undefined);
    assert.equal(harness.elements.get("statusText").textContent, "INVALID GPS ACCURACY");
    assert.equal(harness.elements.get("valGpsLock").textContent, "Invalid GPS accuracy");
});

test("negative and non-numeric GPS accuracy are rejected before transmission", async () => {
    for (const accuracy of [-1, "unknown"]) {
        let updateCount = 0;
        const harness = createBrowserHarness(async (url) => {
            if (url.endsWith("/api/location/update")) updateCount += 1;
            return {
                ok: true,
                json: async () => ({
                    success: true,
                    session: { session_token: "driver-session", vehicle_id: 3 },
                }),
            };
        });
        harness.callbacks.readyCallback();
        await harness.listeners.get("btnStartTracking:click")();
        harness.callbacks.positionSuccess({
            coords: { latitude: 14.5, longitude: 77.5, accuracy },
        });

        assert.equal(updateCount, 0);
        assert.equal(harness.elements.get("statusText").textContent, "INVALID GPS ACCURACY");
        assert.equal(harness.elements.get("telemetryLat").textContent, "");
    }
});

test("GPS errors explain permission denial and unavailable fixes", async () => {
    const harness = createBrowserHarness(async () => ({
        ok: true,
        json: async () => ({ success: true, session: { session_token: "driver-session", vehicle_id: 3 } }),
    }));
    harness.callbacks.readyCallback();
    await harness.listeners.get("btnStartTracking:click")();

    harness.callbacks.positionError({
        code: 1,
        PERMISSION_DENIED: 1,
        POSITION_UNAVAILABLE: 2,
        TIMEOUT: 3,
    });
    assert.match(harness.elements.get("alertMessage").textContent, /Permission Denied/);

    harness.callbacks.positionError({
        code: 2,
        PERMISSION_DENIED: 1,
        POSITION_UNAVAILABLE: 2,
        TIMEOUT: 3,
    });
    assert.match(harness.elements.get("alertMessage").textContent, /Position Unavailable/);

    harness.callbacks.positionError({
        code: 3,
        PERMISSION_DENIED: 1,
        POSITION_UNAVAILABLE: 2,
        TIMEOUT: 3,
    });
    assert.match(harness.elements.get("alertMessage").textContent, /Timed Out/);
});

test("GPS broadcasting is blocked with an HTTPS guidance message on insecure HTTP", async () => {
    let requestCount = 0;
    const harness = createBrowserHarness(async () => {
        requestCount += 1;
        return { ok: true, json: async () => ({ success: true }) };
    }, false);
    harness.callbacks.readyCallback();

    await harness.listeners.get("btnStartTracking:click")();

    assert.match(harness.elements.get("alertMessage").textContent, /secure browser context.*HTTPS/i);
    assert.equal(requestCount, 0);
});

test("API rejection is visible and does not produce a LIVE status", async () => {
    const harness = createBrowserHarness(async (url) => {
        if (url.endsWith("/api/session/start")) {
            return {
                ok: true,
                json: async () => ({
                    success: true,
                    session: { session_token: "driver-session", vehicle_id: 3 },
                }),
            };
        }
        return {
            ok: false,
            status: 403,
            json: async () => ({ success: false, error: "Owner login required." }),
        };
    });

    harness.callbacks.readyCallback();
    await harness.listeners.get("btnStartTracking:click")();
    harness.callbacks.positionSuccess({
        coords: {
            latitude: 14.5,
            longitude: 77.5,
            accuracy: 8,
            speed: null,
            heading: null,
            altitude: null,
        },
    });
    await new Promise((resolve) => setImmediate(resolve));

    assert.match(harness.elements.get("alertMessage").textContent, /server did not save.*Owner login required/i);
    assert.equal(harness.elements.get("statusText").textContent, "GPS FIX • UPDATE FAILED");
});
