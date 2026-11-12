"""Pick the WhatsApp provider from settings. Switching provider never touches business logic."""

from app.config import Settings
from app.whatsapp.base import WhatsAppProvider
from app.whatsapp.cloud_api import CloudApiProvider
from app.whatsapp.fake import FakeProvider


def build_provider(settings: Settings) -> WhatsAppProvider:
    if settings.whatsapp_provider == "kapso":
        auth = {"X-API-Key": settings.kapso_api_key}
    elif settings.whatsapp_provider == "meta":
        auth = {"Authorization": f"Bearer {settings.meta_access_token}"}
    else:
        return FakeProvider()
    return CloudApiProvider(settings.api_base, settings.whatsapp_phone_number_id, auth)
