"""Settings, read only from environment variables (and a local .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_BASE = {
    # TODO(kapso-docs): confirm the current API version in the base URL.
    "kapso": "https://api.kapso.ai/meta/whatsapp/v24.0",
    "meta": "https://graph.facebook.com/v24.0",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://menions:menions@localhost:5432/menions"
    public_base_url: str = "http://localhost:8000"

    whatsapp_provider: Literal["fake", "kapso", "meta"] = "fake"
    whatsapp_api_base: str = ""
    whatsapp_phone_number_id: str = ""
    kapso_api_key: str = ""
    meta_access_token: str = ""

    whatsapp_webhook_signature: Literal["none", "kapso", "meta"] = "none"
    kapso_webhook_secret: str = ""
    meta_app_secret: str = ""
    whatsapp_verify_token: str = ""

    # Phones (comma-separated) whose messages starting with "/" are admin commands, and who
    # get admin alerts on WhatsApp. Example: +256772000001,+256701000002
    admin_phones: str = ""
    # The business WhatsApp number in international digits (2567...), for click-to-chat links.
    whatsapp_display_number: str = ""
    # Where merchants send plan payments.
    momo_pay_number: str = ""
    momo_pay_name: str = ""
    # An approved template with one body parameter, used to reach someone outside the
    # 24-hour window. Empty: such messages are not sent and an admin alert is recorded.
    whatsapp_update_template: str = ""
    whatsapp_template_language: str = "en"
    # Delivery and pickup photos are saved here.
    media_dir: str = "var/media"

    company_name: str = "Men-Ions"
    company_tin: str = ""
    company_address: str = "Kampala, Uganda"
    company_phone: str = ""

    # --- Plan payments ---------------------------------------------------------------
    # manual: the merchant sends Mobile Money by hand and an admin approves with /approve.
    # pawapay: the merchant gets a PIN prompt on their phone; the plan starts the moment
    #          pawaPay confirms the payment. Manual stays available as a fallback.
    payments_provider: Literal["manual", "pawapay"] = "manual"
    pawapay_api_token: str = ""
    # Sandbox by default. Production: https://api.pawapay.io
    pawapay_api_base: str = "https://api.sandbox.pawapay.io"
    # Optional: a long random string added to the callback URL as ?k=... in pawaPay's dashboard.
    # Callbacks are never trusted on their own (we always re-check the status with pawaPay),
    # so this only keeps strangers from making us call pawaPay.
    pawapay_callback_token: str = ""

    # --- EFRIS (URA e-receipts) ------------------------------------------------------
    # manual: admins issue the e-receipt on URA's portal and send /fdn REF FDN CODE.
    # fake:   made-up FDNs, for development only (refused in production).
    efris_provider: Literal["manual", "fake"] = "manual"
    # Admins are alerted when a sale has waited this long for its FDN. URA allows 24 hours.
    efris_alert_hours: int = 12

    # --- Privacy -----------------------------------------------------------------------
    # Delivery photos, customers' pins and directions, and raw WhatsApp messages are deleted
    # this many days after an order closes. Receipts are kept: they are financial records.
    data_retention_days: int = 90
    # Who people contact about their data. Shown in the privacy notice.
    privacy_contact: str = ""

    @property
    def admin_phone_list(self) -> list[str]:
        from app.phones import InvalidPhone, normalize_wa_id

        phones = []
        for raw in self.admin_phones.split(","):
            if raw.strip():
                try:
                    phones.append(normalize_wa_id(raw.strip()))
                except InvalidPhone:
                    pass
        return phones

    @property
    def api_base(self) -> str:
        return (self.whatsapp_api_base or DEFAULT_API_BASE.get(self.whatsapp_provider, "")).rstrip("/")

    @model_validator(mode="after")
    def _refuse_unsafe(self) -> "Settings":
        problems: list[str] = []
        if self.env == "production" and self.whatsapp_webhook_signature == "none":
            problems.append("WHATSAPP_WEBHOOK_SIGNATURE=none is not allowed when ENV=production")
        if self.whatsapp_webhook_signature == "meta" and not self.meta_app_secret:
            problems.append("WHATSAPP_WEBHOOK_SIGNATURE=meta needs META_APP_SECRET")
        if self.whatsapp_webhook_signature == "kapso" and not self.kapso_webhook_secret:
            problems.append("WHATSAPP_WEBHOOK_SIGNATURE=kapso needs KAPSO_WEBHOOK_SECRET")
        if self.whatsapp_provider == "kapso" and not self.kapso_api_key:
            problems.append("WHATSAPP_PROVIDER=kapso needs KAPSO_API_KEY")
        if self.whatsapp_provider == "meta" and not self.meta_access_token:
            problems.append("WHATSAPP_PROVIDER=meta needs META_ACCESS_TOKEN")
        if self.whatsapp_provider in ("kapso", "meta") and not self.whatsapp_phone_number_id:
            problems.append(f"WHATSAPP_PROVIDER={self.whatsapp_provider} needs WHATSAPP_PHONE_NUMBER_ID")
        if self.payments_provider == "pawapay" and not self.pawapay_api_token:
            problems.append("PAYMENTS_PROVIDER=pawapay needs PAWAPAY_API_TOKEN")
        if self.env == "production" and self.efris_provider == "fake":
            problems.append("EFRIS_PROVIDER=fake is not allowed when ENV=production")
        if self.data_retention_days < 7:
            problems.append("DATA_RETENTION_DAYS must be at least 7")
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
