import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr
from html import escape
from urllib.parse import urlparse

import requests
from flask import render_template


logger = logging.getLogger(__name__)


class EmailConfigurationError(Exception):
    pass


def _enabled():
    return os.getenv("MAIL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _email_provider():
    return os.getenv("EMAIL_PROVIDER", "smtp").strip().lower()


def _setting_enabled(name, default):
    value = os.getenv(name, default).strip().lower()
    if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise EmailConfigurationError(f"{name} must be a boolean value")
    return value in {"1", "true", "yes", "on"}


def email_configuration_status():
    provider = _email_provider()
    try:
        enabled = _setting_enabled("MAIL_ENABLED", "false")
    except EmailConfigurationError as error:
        return {"enabled": False, "configured": False, "message": str(error)}

    if not enabled:
        if provider == "google_script":
            required = ("GOOGLE_EMAIL_WEBHOOK_URL", "GOOGLE_EMAIL_WEBHOOK_TOKEN")
        else:
            required = ("MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER")
        missing = [
            name for name in required if not os.getenv(name, "").strip()
        ]
        required_settings = ", ".join(missing)
        setup_message = (
            f" Set {required_settings}." if required_settings else ""
        )
        return {
            "enabled": False,
            "configured": False,
            "message": "Email notifications are disabled. Set MAIL_ENABLED=true to enable them."
            + setup_message,
        }

    if provider == "google_script":
        webhook_url = os.getenv("GOOGLE_EMAIL_WEBHOOK_URL", "").strip()
        token = os.getenv("GOOGLE_EMAIL_WEBHOOK_TOKEN", "").strip()
        missing = [
            name
            for name, value in (
                ("GOOGLE_EMAIL_WEBHOOK_URL", webhook_url),
                ("GOOGLE_EMAIL_WEBHOOK_TOKEN", token),
            )
            if not value
        ]
        if missing:
            return {
                "enabled": True,
                "configured": False,
                "message": "Set the following email configuration: " + ", ".join(missing) + ".",
            }
        parsed_url = urlparse(webhook_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            return {
                "enabled": True,
                "configured": False,
                "message": "GOOGLE_EMAIL_WEBHOOK_URL must be an HTTPS URL.",
            }
        return {"enabled": True, "configured": True, "message": ""}

    if provider != "smtp":
        return {
            "enabled": True,
            "configured": False,
            "message": "EMAIL_PROVIDER must be either smtp or google_script.",
        }

    required = ("MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        return {
            "enabled": True,
            "configured": False,
            "message": "Set the following email configuration: " + ", ".join(missing) + ".",
        }

    try:
        _settings()
    except EmailConfigurationError as error:
        return {"enabled": True, "configured": False, "message": str(error)}

    return {"enabled": True, "configured": True, "message": ""}


def is_mail_configured():
    """Returns True if email notifications are enabled and settings are valid."""
    return email_configuration_status()["configured"]


def _settings():
    required = ("MAIL_SERVER", "MAIL_PORT", "MAIL_DEFAULT_SENDER")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise EmailConfigurationError("Missing email configuration: " + ", ".join(missing))

    try:
        port = int(os.environ["MAIL_PORT"])
    except ValueError as error:
        raise EmailConfigurationError("MAIL_PORT must be an integer between 1 and 65535.") from error
    if not 1 <= port <= 65535:
        raise EmailConfigurationError("MAIL_PORT must be an integer between 1 and 65535.")

    use_tls = _setting_enabled("MAIL_USE_TLS", "true")
    use_ssl = _setting_enabled("MAIL_USE_SSL", "false")
    if use_tls and use_ssl:
        raise EmailConfigurationError("Use either MAIL_USE_TLS or MAIL_USE_SSL, not both.")

    sender = os.environ["MAIL_DEFAULT_SENDER"].strip()
    sender_address = parseaddr(sender)[1]
    if not sender_address or "@" not in sender_address or any(character.isspace() for character in sender_address):
        raise EmailConfigurationError("MAIL_DEFAULT_SENDER must contain a valid sender email address.")

    username = os.getenv("MAIL_USERNAME", "").strip()
    password = os.getenv("MAIL_PASSWORD", "")
    if bool(username) != bool(password):
        raise EmailConfigurationError("Set both MAIL_USERNAME and MAIL_PASSWORD for SMTP authentication.")

    return {
        "server": os.environ["MAIL_SERVER"].strip(),
        "port": port,
        "use_tls": use_tls,
        "use_ssl": use_ssl,
        "username": username or None,
        "password": password or None,
        "sender": sender,
    }


def _send(recipient, subject, template_name, context, html_template_name=None):
    if not recipient:
        return False

    plain_content = render_template(template_name, **context)
    html_content = None
    if html_template_name:
        try:
            html_content = render_template(html_template_name, **context)
        except Exception:
            logger.warning("Could not render HTML email template %s; falling back to text", html_template_name)

    if _email_provider() == "google_script":
        configuration = email_configuration_status()
        if not configuration["configured"]:
            raise EmailConfigurationError(configuration["message"])
        if html_content is None:
            html_content = (
                "<div style='font-family: Arial, sans-serif; white-space: pre-wrap; "
                f"line-height: 1.6;'>{escape(plain_content)}</div>"
            )
        return _send_google_script(recipient, subject, html_content)

    settings = _settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = recipient
    message.set_content(plain_content)
    if html_content is not None:
        message.add_alternative(html_content, subtype="html")

    smtp_class = smtplib.SMTP_SSL if settings["use_ssl"] else smtplib.SMTP
    with smtp_class(
        settings["server"],
        settings["port"],
        timeout=10,
        **({"context": ssl.create_default_context()} if settings["use_ssl"] else {}),
    ) as smtp:
        if settings["use_tls"]:
            smtp.starttls(context=ssl.create_default_context())
        if settings["username"]:
            smtp.login(settings["username"], settings["password"] or "")
        refused_recipients = smtp.send_message(message)
    return not refused_recipients


def _send_google_script(recipient, subject, html_body):
    payload = {
        "token": os.environ["GOOGLE_EMAIL_WEBHOOK_TOKEN"],
        "to": recipient,
        "subject": subject,
        "html": html_body,
    }
    try:
        response = requests.post(
            os.environ["GOOGLE_EMAIL_WEBHOOK_URL"].strip(),
            json=payload,
            timeout=10,
        )
    except requests.RequestException as error:
        logger.error(
            "Google Script email submission failed (%s)",
            type(error).__name__,
        )
        return False

    if not response.ok:
        logger.error(
            "Google Script email submission failed (HTTP %s)",
            response.status_code,
        )
        return False

    try:
        result = response.json()
    except ValueError:
        logger.error("Google Script email submission returned an invalid response")
        return False

    if not isinstance(result, dict) or str(result.get("success", "")).strip().lower() != "true":
        logger.error("Google Script email submission was not accepted")
        return False
    return True


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


def send_booking_decision_to_customer(booking):
    configuration = email_configuration_status()
    if not configuration["enabled"]:
        return False
    if not configuration["configured"]:
        logger.error("Booking decision email unavailable: %s", configuration["message"])
        return False

    recipient = booking.get("customer_email")
    if not recipient:
        logger.warning("Booking decision email skipped: customer email is missing")
        return False

    booking_id = booking.get("booking_id")
    status = booking.get("status", "updated")
    subject = (
        f"BusinessVehicleBooking - Booking #{booking_id} {status}"
        if booking_id
        else f"BusinessVehicleBooking - Booking {status}"
    )
    try:
        accepted = _send(
            recipient,
            subject,
            "emails/booking_decision.txt",
            {"booking": booking},
            html_template_name="emails/booking_decision.html",
        )
    except Exception as error:
        smtp_code = getattr(error, "smtp_code", None)
        error_number = getattr(error, "errno", None)
        logger.error(
            "Booking decision email submission failed (%s, code=%s)",
            type(error).__name__,
            smtp_code or error_number or "unavailable",
        )
        return False

    if not accepted:
        logger.warning("Booking decision email was not accepted by the configured email provider")
    return accepted


def send_password_reset_email(recipient, reset_url, account_type):
    if not _enabled():
        return False
    title = "customer" if account_type == "customer" else "owner"
    return _send(
        recipient,
        "Business Vehicle Booking - Password Reset",
        "emails/password_reset.txt",
        {"reset_url": reset_url, "account_type": title},
        html_template_name="emails/password_reset.html",
    )


def send_booking_notifications(booking):
    results = {"customer": False, "owner": False}
    configuration = email_configuration_status()
    if not configuration["enabled"]:
        return results
    if not configuration["configured"]:
        logger.error("Booking email notifications unavailable: %s", configuration["message"])
        return results

    for key, sender in (
        ("customer", send_booking_confirmation_to_customer),
        ("owner", send_new_booking_notification_to_owner),
    ):
        if not booking.get("customer_email" if key == "customer" else "owner_email"):
            logger.warning("Booking %s email notification skipped: recipient email is missing", key)
            continue
        try:
            results[key] = sender(booking)
            if not results[key]:
                logger.warning("Booking %s email was not accepted by the configured email provider", key)
        except Exception as error:
            smtp_code = getattr(error, "smtp_code", None)
            error_number = getattr(error, "errno", None)
            logger.error(
                "Booking %s email submission failed (%s, code=%s)",
                key,
                type(error).__name__,
                smtp_code or error_number or "unavailable",
            )
    return results