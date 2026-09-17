/**
 * Business Vehicle Booking — Shared Client Script
 * Provides date-picker restrictions, alert auto-dismissal, and interactive rating behavior.
 */

document.addEventListener("DOMContentLoaded", () => {
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
