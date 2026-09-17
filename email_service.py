import logging
import os
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv
from flask import render_template


load_dotenv()

logger = logging.getLogger(__name__)


class EmailConfigurationError(Exception):
    pass


def _enabled():
    return os.getenv("MAIL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def is_mail_configured():
    """Returns True if email notifications are enabled and required settings are present."""
    if not _enabled():
        return False
    required = ("MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_DEFAULT_SENDER")
    configured = all(bool(os.getenv(name)) for name in required)
    logger.warning(
        "Email configuration loaded: server=%s, port=%s, username_configured=%s, sender_configured=%s",
        os.getenv("MAIL_SERVER", "<missing>"),
        os.getenv("MAIL_PORT", "<missing>"),
        bool(os.getenv("MAIL_USERNAME")),
        bool(os.getenv("MAIL_DEFAULT_SENDER")),
    )
    return configured


def _settings():
    required = ("MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_DEFAULT_SENDER")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise EmailConfigurationError("Missing email configuration: " + ", ".join(missing))
    return {
        "server": os.environ["MAIL_SERVER"],
        "port": int(os.environ.get("MAIL_PORT", "587")),
        "use_tls": os.getenv("MAIL_USE_TLS", "true").lower() in {"1", "true", "yes", "on"},
        "username": os.environ["MAIL_USERNAME"],
        "password": os.environ["MAIL_PASSWORD"],
        "sender": os.environ["MAIL_DEFAULT_SENDER"],
    }


def _send(recipient, subject, template_name, context, html_template_name=None):
    if not recipient:
        return False
    settings = _settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = recipient

    plain_content = render_template(template_name, **context)
    message.set_content(plain_content)

    if html_template_name:
        try:
            html_content = render_template(html_template_name, **context)
            message.add_alternative(html_content, subtype="html")
        except Exception:
            logger.warning("Could not render HTML email template %s; falling back to text", html_template_name)

    try:
        with smtplib.SMTP(settings["server"], settings["port"], timeout=10) as smtp:
            if settings["use_tls"]:
                smtp.starttls()
            smtp.login(settings["username"], settings["password"])
            smtp.send_message(message)
        logger.warning("Email sent successfully: recipient=%s subject=%s", recipient, subject)
        return True
    except (smtplib.SMTPException, OSError):
        logger.exception("SMTP email delivery failed: recipient=%s subject=%s", recipient, subject)
        return False


def send_booking_confirmation_to_customer(booking):
    if not _enabled():
        return False
    booking_id = booking.get("booking_id")
    subject = f"Vehicle Booking Confirmation - Booking #{booking_id}" if booking_id else "BusinessVehicleBooking - Booking Confirmation"
    return _send(
        booking.get("customer_email"),
        subject,
        "emails/booking_confirmation.txt",
        {"booking": booking},
        html_template_name="emails/booking_confirmation.html",
    )


def send_new_booking_notification_to_owner(booking):
    if not _enabled():
        return False
    booking_id = booking.get("booking_id")
    subject = f"BusinessVehicleBooking - New Vehicle Booking - Booking #{booking_id}" if booking_id else "BusinessVehicleBooking - New Vehicle Booking"
    return _send(
        booking.get("owner_email"),
        subject,
        "emails/owner_booking_notification.txt",
        {"booking": booking},
        html_template_name="emails/owner_booking_notification.html",
    )


def send_booking_notifications(booking, include_customer=True):
    """Send booking notifications, optionally excluding the pending customer email.

    New bookings should pass ``include_customer=False`` because the customer
    receives a final message only after the owner accepts or rejects.
    """
    results = {"customer": False, "owner": False}
    if not _enabled():
        return results
    senders = [("owner", send_new_booking_notification_to_owner)]
    if include_customer:
        senders.insert(0, ("customer", send_booking_confirmation_to_customer))
    for key, sender in senders:
        try:
            results[key] = sender(booking)
        except Exception:
            logger.exception("Booking %s email notification failed", key)
    return results


def send_booking_decision_email(booking):
    """Send the customer's final accepted/rejected decision notification."""
    if not _enabled():
        return False
    status = str(booking.get("status") or "").strip().title()
    if status not in {"Accepted", "Rejected"}:
        return False
    customer_email = booking.get("customer_email")
    if not customer_email:
        logger.warning("Customer email is missing for booking %s", booking.get("booking_id"))
        return False
    subject = f"Vehicle Booking {status} - Booking #{booking.get('booking_id')}"
    return _send(
        customer_email,
        subject,
        "emails/booking_decision.txt",
        {"booking": booking},
        html_template_name="emails/booking_decision.html",
    )


def send_demo_payment_confirmation_email(payment):
    """Send a demo payment receipt email without claiming a real financial transfer."""
    if not _enabled():
        return False
    recipient = payment.get("customer_email")
    if not recipient:
        logger.warning("Customer email is missing for demo payment on booking %s", payment.get("booking_id"))
        return False
    subject = f"Demo Payment Confirmation - Booking #{payment.get('booking_id')}"
    return _send(
        recipient,
        subject,
        "emails/payment_confirmation.txt",
        {"payment": payment},
        html_template_name="emails/payment_confirmation.html",
    )


def send_password_reset_email(recipient, reset_url):
    """Send a password-reset link without exposing the reset token to logs."""
    if not _enabled() or not recipient:
        return False
    return _send(
        recipient,
        "BusinessVehicleBooking - Password Reset",
        "emails/password_reset.txt",
        {"reset_url": reset_url},
    )