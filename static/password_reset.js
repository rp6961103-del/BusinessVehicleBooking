"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const tokenField = document.getElementById("reset-token");
    const tokenFromLink = window.location.hash.slice(1);
    if (tokenField && tokenFromLink) {
        tokenField.value = tokenFromLink;
        window.history.replaceState(null, "", window.location.pathname);
    }
});
