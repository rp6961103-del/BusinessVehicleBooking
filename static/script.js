/**
 * Business Vehicle Booking — Shared Client Script
 * Provides date-picker restrictions, alert auto-dismissal, and interactive rating behavior.
 */

document.addEventListener("DOMContentLoaded", () => {
    const eyeIcon = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"></path><circle cx="12" cy="12" r="3"></circle></svg>';
    const eyeOffIcon = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8"></path><path d="M9.9 5.2A10.7 10.7 0 0 1 12 5c6.4 0 10 7 10 7a16 16 0 0 1-3.1 3.8M6.2 6.2C3.5 8 2 12 2 12s3.6 7 10 7c1.4 0 2.7-.3 3.8-.8"></path></svg>';

    document.querySelectorAll('input[type="password"]').forEach(input => {
        if (input.dataset.passwordToggle === "true") return;

        const wrapper = document.createElement("span");
        wrapper.className = "password-input-wrapper";
        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "password-visibility-toggle";
        toggle.setAttribute("aria-label", "Show password");
        toggle.setAttribute("aria-pressed", "false");
        toggle.title = "Show password";
        toggle.innerHTML = `${eyeIcon}<span class="password-visibility-label">Show</span>`;
        toggle.addEventListener("click", () => {
            const showPassword = input.type === "password";
            input.type = showPassword ? "text" : "password";
            const label = showPassword ? "Hide password" : "Show password";
            toggle.setAttribute("aria-label", label);
            toggle.setAttribute("aria-pressed", String(showPassword));
            toggle.title = label;
            const visibleLabel = showPassword ? "Hide" : "Show";
            toggle.innerHTML = `${showPassword ? eyeOffIcon : eyeIcon}<span class="password-visibility-label">${visibleLabel}</span>`;
        });
        wrapper.appendChild(toggle);
        input.dataset.passwordToggle = "true";
    });

    // 1. Ensure booking date inputs cannot select past dates
    const dateInputs = document.querySelectorAll('input[type="date"]');
    const today = new Date().toISOString().split("T")[0];
    dateInputs.forEach(input => {
        if (!input.getAttribute("min")) {
            input.setAttribute("min", today);
        }
    });

    // 2. Auto-dismiss flash alerts after 6 seconds
    const messages = document.querySelectorAll(".message, .admin-message, .alert");
    messages.forEach(msg => {
        if (!msg.classList.contains("permanent")) {
            setTimeout(() => {
                msg.style.transition = "opacity 0.5s ease, transform 0.5s ease";
                msg.style.opacity = "0";
                msg.style.transform = "translateY(-8px)";
                setTimeout(() => msg.remove(), 500);
            }, 6000);
        }
    });

    // 3. Dynamic star rating hover effect for rate_booking.html
    const starWidget = document.querySelector(".star-rating-widget");
    if (starWidget) {
        const labels = starWidget.querySelectorAll("label");
        const inputs = starWidget.querySelectorAll("input");

        inputs.forEach(input => {
            input.addEventListener("change", () => {
                const val = parseInt(input.value, 10);
                labels.forEach(lbl => {
                    const starVal = parseInt(lbl.querySelector("input")?.value || "0", 10);
                    lbl.style.color = starVal <= val ? "#f59e0b" : "#cbd5e1";
                });
            });
        });
    }
});
