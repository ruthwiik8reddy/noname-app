"""
sms_service.py — SMS delivery for BayQ
-----------------------------------------
Sends customer-facing SMS messages via Twilio.
If TWILIO_ACCOUNT_SID is not set, SMS calls are silently logged (dev-safe).
"""

import os
import logging

logger = logging.getLogger(__name__)


def _credentials(studio_id: int | None) -> tuple[str, str, str]:
    """
    (account_sid, auth_token, from_number) for a studio.

    A studio's own Twilio account wins; otherwise the platform account from
    .env is used. Studios can never change the platform account.
    """
    if studio_id is not None:
        try:
            from .services.repositories import SettingsRepository
            cfg = SettingsRepository().all(studio_id)
            if cfg.get("twilio_account_sid") and cfg.get("twilio_auth_token"):
                return (cfg["twilio_account_sid"].strip(), cfg["twilio_auth_token"].strip(),
                        cfg.get("twilio_from_number", "").strip())
        except Exception as exc:  # noqa: BLE001 - fall back to platform creds
            logger.warning(f"Could not load studio SMS settings for {studio_id}: {exc}")
    return (os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            os.getenv("TWILIO_FROM_NUMBER", "").strip())


def _twilio_client(sid: str, token: str):
    """Return a Twilio REST client, or None if credentials are not configured."""
    if not sid or not token:
        return None
    try:
        from twilio.rest import Client
        return Client(sid, token)
    except ImportError:
        logger.warning("twilio package not installed. Run: pip install twilio")
        return None


def _base_url() -> str:
    url = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not url:
        logger.warning("APP_BASE_URL not set — SMS links will be incomplete.")
    return url


def send_tracking_sms(phone: str, job_token: str, customer_name: str = "",
                      studio_id: int | None = None) -> bool:
    """Send the live tracking link to a customer."""
    logger.info(f"Entering send_tracking_sms(phone={phone}, token={job_token[:8]}...)")
    try:
        sid, token, from_number = _credentials(studio_id)
        client      = _twilio_client(sid, token)
        base_url    = _base_url()
        link        = f"{base_url}/customer/tracking/{job_token}"

        greeting = f"Hi {customer_name}, " if customer_name else ""
        body = (
            f"{greeting}your vehicle is now in our care.\n"
            f"Track your job live here:\n{link}\n\n"
            f"Tap the link to see real-time status and DVI details."
        )

        if not client:
            logger.info(f"[SMS-DEV] Would send to {phone}: {body}")
            return True

        message = client.messages.create(body=body, from_=from_number, to=phone)
        logger.info(f"Exiting send_tracking_sms — SID: {message.sid}")
        return True
    except Exception as exc:
        logger.error(f"Error in send_tracking_sms(phone={phone}): {exc}", exc_info=True)
        return False


def send_status_update_sms(phone: str, job_token: str, new_status: str,
                           customer_name: str = "", studio_id: int | None = None) -> bool:
    """Notify the customer when their job status changes."""
    logger.info(f"Entering send_status_update_sms(phone={phone}, status={new_status})")
    try:
        sid, token, from_number = _credentials(studio_id)
        client      = _twilio_client(sid, token)
        base_url    = _base_url()
        link        = f"{base_url}/customer/tracking/{job_token}"

        status_messages = {
            "Pending":     "Your vehicle has been received and is queued for service.",
            "In Progress": "Work has started on your vehicle.",
            "Completed":   "Your vehicle is ready for pickup!",
        }
        status_line = status_messages.get(new_status, f"Status updated to: {new_status}")

        greeting = f"Hi {customer_name}, " if customer_name else ""
        body = f"{greeting}{status_line}\nView full details: {link}"

        if not client:
            logger.info(f"[SMS-DEV] Would send status update to {phone}: {body}")
            return True

        message = client.messages.create(body=body, from_=from_number, to=phone)
        logger.info(f"Exiting send_status_update_sms — SID: {message.sid}")
        return True
    except Exception as exc:
        logger.error(f"Error in send_status_update_sms(phone={phone}): {exc}", exc_info=True)
        return False
