"""
sms_service.py — SMS delivery for Autofiera
----------------------------------------------
Sends customer-facing SMS messages via Twilio.

All Twilio credentials are read from environment variables — never hardcoded.
Set these in your .env file:
    TWILIO_ACCOUNT_SID   = ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    TWILIO_AUTH_TOKEN    = your_auth_token
    TWILIO_FROM_NUMBER   = +1XXXXXXXXXX   (your Twilio phone number)
    APP_BASE_URL         = https://yourdomain.com   (no trailing slash)

If TWILIO_ACCOUNT_SID is not set, SMS calls are silently skipped with a
console log — safe for local development without credentials.
"""

import os
import logging

logger = logging.getLogger(__name__)


def _twilio_client():
    """Return a Twilio REST client, or None if credentials are not configured."""
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        return None
    try:
        from twilio.rest import Client
        return Client(sid, token)
    except ImportError:
        logger.warning("twilio package is not installed. Run: pip install twilio")
        return None


def _base_url() -> str:
    url = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not url:
        logger.warning("APP_BASE_URL is not set — SMS links will be incomplete.")
    return url


def send_tracking_sms(phone: str, job_token: str, customer_name: str = "") -> bool:
    """
    Send the live tracking link to a customer.

    Returns True if the message was dispatched (or skipped in dev mode),
    False if delivery failed.
    """
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    client      = _twilio_client()
    base_url    = _base_url()
    link        = f"{base_url}/customer/tracking/{job_token}"

    greeting = f"Hi {customer_name}, " if customer_name else ""
    body = (
        f"{greeting}your vehicle is now in our care.\n"
        f"Track your job live here:\n{link}\n\n"
        f"Tap the link to see real-time status and DVI details."
    )

    if not client:
        # Dev mode — just log
        logger.info(f"[SMS-DEV] Would send to {phone}: {body}")
        print(f"[SMS-DEV] Would send to {phone}:\n{body}")
        return True

    try:
        message = client.messages.create(body=body, from_=from_number, to=phone)
        logger.info(f"[SMS] Sent tracking link to {phone} — SID: {message.sid}")
        return True
    except Exception as exc:
        logger.error(f"[SMS] Failed to send to {phone}: {exc}")
        return False


def send_status_update_sms(phone: str, job_token: str, new_status: str,
                            customer_name: str = "") -> bool:
    """
    Notify the customer when their job status changes.
    Called from controller.py when a staff member updates job status.
    """
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    client      = _twilio_client()
    base_url    = _base_url()
    link        = f"{base_url}/customer/tracking/{job_token}"

    status_messages = {
        "Pending":     "Your vehicle has been received and is queued for service.",
        "In Progress": "Work has started on your vehicle.",
        "Completed":   "Your vehicle is ready for pickup!",
    }
    status_line = status_messages.get(new_status, f"Status updated to: {new_status}")

    greeting = f"Hi {customer_name}, " if customer_name else ""
    body = (
        f"{greeting}{status_line}\n"
        f"View full details: {link}"
    )

    if not client:
        logger.info(f"[SMS-DEV] Would send status update to {phone}: {body}")
        print(f"[SMS-DEV] Would send status update to {phone}:\n{body}")
        return True

    try:
        message = client.messages.create(body=body, from_=from_number, to=phone)
        logger.info(f"[SMS] Sent status update to {phone} — SID: {message.sid}")
        return True
    except Exception as exc:
        logger.error(f"[SMS] Failed to send status update to {phone}: {exc}")
        return False
