"""
repositories/settings_repository.py — per-studio key/value settings.

Anything a studio configures for itself (SMS sender, default tax rate, invoice
terms) lives here, scoped by studio_id. Nothing a studio sets may change
another studio's behaviour, which is why this replaced writing to .env.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from .base import BaseRepository

# Keys a studio is allowed to set, with their defaults. Unknown keys are
# rejected so a typo (or a creative agent) can't invent settings nothing reads.
DEFAULTS: Dict[str, str] = {
    "twilio_account_sid": "",
    "twilio_auth_token": "",
    "twilio_from_number": "",
    "default_tax_percent": "8.5",
    "invoice_payment_terms_days": "14",
    "invoice_terms": "",
    "invoice_payment_instructions": "",
    "invoice_footer": "",
    "invoice_number_prefix": "INV-",
    "business_address": "",
    "business_phone": "",
    "business_email": "",
    "business_website": "",
    "business_tax_id": "",
    "business_hours": "",
    # Dialling code added to 10-digit numbers for WhatsApp links ("91", "1"…).
    "whatsapp_country_code": "",
}


class SettingsRepository(BaseRepository):

    def get(self, studio_id: int, key: str, default: Optional[str] = None) -> str:
        value = self.fetch_scalar(
            "SELECT value FROM studio_settings WHERE studio_id=? AND key=?", (studio_id, key)
        )
        if value is None:
            return DEFAULTS.get(key, "") if default is None else default
        return value

    def all(self, studio_id: int) -> Dict[str, str]:
        values = dict(DEFAULTS)
        for row in self.fetch_all(
            "SELECT key, value FROM studio_settings WHERE studio_id=?", (studio_id,)
        ):
            values[row["key"]] = row["value"]
        return values

    def set(self, studio_id: int, key: str, value: str) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"Unknown studio setting: {key}")
        self.execute(
            "INSERT INTO studio_settings (studio_id, key, value, updated_at) "
            "VALUES (?,?,?, datetime('now')) "
            "ON CONFLICT(studio_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now')",
            (studio_id, key, str(value)),
        )

    def set_many(self, studio_id: int, values: Dict[str, str]) -> None:
        for key, value in values.items():
            self.set(studio_id, key, value)

    @staticmethod
    def known_keys() -> Iterable[str]:
        return DEFAULTS.keys()
