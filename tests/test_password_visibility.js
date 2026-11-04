const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const projectRoot = path.join(__dirname, "..");
const authTemplates = [
    "templates/login.html",
    "templates/register.html",
    "templates/owner_login.html",
    "templates/owner_register.html",
    "templates/admin_login.html",
    "templates/reset_password.html",
];

function createInput() {
    return {
        type: "password",
        dataset: {},
        parentNode: null,
    };
}

function createElement(tagName) {
    return {
        tagName,
        attributes: {},
        children: [],
        parentNode: null,
        setAttribute(name, value) {
            this.attributes[name] = value;
        },
        addEventListener(name, callback) {
            this.listeners = this.listeners || {};
            this.listeners[name] = callback;
        },
        insertBefore(child, reference) {
            const index = this.children.indexOf(reference);
            this.children.splice(index < 0 ? this.children.length : index, 0, child);
            child.parentNode = this;
        },
        appendChild(child) {
            if (child.parentNode) {
                const oldIndex = child.parentNode.children.indexOf(child);
                if (oldIndex >= 0) child.parentNode.children.splice(oldIndex, 1);
            }
            this.children.push(child);
            child.parentNode = this;
        },
    };
}

test("all login, registration, and password reset pages load the shared toggle", () => {
    for (const relativePath of authTemplates) {
        const html = fs.readFileSync(path.join(projectRoot, relativePath), "utf8");
        assert.match(html, /type="password"/, `${relativePath} has a password field`);
        assert.match(html, /static', filename='script\.js'/, `${relativePath} loads shared JS`);
    }
});

test("each password input has an independent eye button with a visible Show/Hide label", () => {
    const inputs = authTemplates.map(() => createInput());
    inputs.push(createInput());
    const formGroup = createElement("div");
    for (const input of inputs) {
        input.parentNode = formGroup;
        formGroup.children.push(input);
    }

    let ready;
    const document = {
        querySelectorAll(selector) {
            return selector === 'input[type="password"]' ? inputs : [];
        },
        querySelector() {
            return null;
        },
        addEventListener(name, callback) {
            if (name === "DOMContentLoaded") ready = callback;
        },
        createElement,
    };

    const script = fs.readFileSync(path.join(projectRoot, "static", "script.js"), "utf8");
    vm.runInNewContext(script, {
        document,
        Date,
        setTimeout() {},
        clearTimeout() {},
        parseInt,
    });
    ready();

    for (const input of inputs) {
        const wrapper = input.parentNode;
        const button = wrapper.children.find(child => child.tagName === "button");
        assert.ok(button);
        const initialIcon = button.innerHTML;
        assert.equal(button.type, "button");
        assert.equal(button.attributes["aria-label"], "Show password");
        assert.equal(input.type, "password");
        assert.match(initialIcon, /<svg\b/);
        assert.match(initialIcon, /M2 12/);
        assert.match(button.innerHTML, /password-visibility-label">Show<\/span>/);

        button.listeners.click();
        assert.equal(input.type, "text");
        assert.equal(button.attributes["aria-label"], "Hide password");
        assert.equal(button.attributes["aria-pressed"], "true");
        assert.match(button.innerHTML, /password-visibility-label">Hide<\/span>/);
        assert.match(button.innerHTML, /<svg\b/);
        assert.match(button.innerHTML, /m3 3/);
        assert.notEqual(button.innerHTML, initialIcon);
        assert.ok(inputs.every(otherInput => otherInput === input || otherInput.type === "password"));

        button.listeners.click();
        assert.equal(input.type, "password");
        assert.equal(button.attributes["aria-label"], "Show password");
        assert.equal(button.attributes["aria-pressed"], "false");
        assert.match(button.innerHTML, /password-visibility-label">Show<\/span>/);
    }
});
