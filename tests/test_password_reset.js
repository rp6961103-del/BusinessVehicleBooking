const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("the reset-link fragment is copied to the form and removed from the address", () => {
    const tokenField = { value: "" };
    let ready;
    let replaced;
    const document = {
        getElementById(id) {
            return id === "reset-token" ? tokenField : null;
        },
        addEventListener(name, callback) {
            if (name === "DOMContentLoaded") ready = callback;
        },
    };
    const window = {
        location: { hash: `#${"A".repeat(43)}`, pathname: "/reset-password/customer" },
        history: {
            replaceState(_state, _title, url) {
                replaced = url;
            },
        },
    };
    const source = fs.readFileSync(
        path.join(__dirname, "..", "static", "password_reset.js"),
        "utf8"
    );

    vm.runInNewContext(source, { document, window });
    ready();

    assert.equal(tokenField.value, "A".repeat(43));
    assert.equal(replaced, "/reset-password/customer");
});
