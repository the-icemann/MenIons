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

    company_name: str = "Men-Ions"
    company_tin: str = ""
    company_address: str = "Kampala, Uganda"
    company_phone: str = ""

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
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
