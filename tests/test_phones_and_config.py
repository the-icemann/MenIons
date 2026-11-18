import pytest
from pydantic import ValidationError

from app.config import Settings
from app.phones import InvalidPhone, mask_phone, normalize_phone, normalize_wa_id


@pytest.mark.parametrize(
    "raw",
    ["0772123456", "256772123456", "+256772123456", " 0772 123 456 ", "+256-772-123-456"],
)
def test_normalize_phone_accepts_the_three_formats(raw):
    assert normalize_phone(raw) == "+256772123456"


@pytest.mark.parametrize(
    "raw",
    ["", "hello", "772123456", "07721234", "07721234567", "+254712345678", "0312123456", "+2567721234567", None, 772123456],
)
def test_normalize_phone_rejects_junk(raw):
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)


def test_wa_id_keeps_foreign_numbers_but_rejects_junk():
    assert normalize_wa_id("256772123456") == "+256772123456"
    assert normalize_wa_id("254712345678") == "+254712345678"
    with pytest.raises(InvalidPhone):
        normalize_wa_id("abc")


def test_mask_phone_hides_the_middle():
    assert mask_phone("+256772123456") == "+25677****456"


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    """Docker Compose loads .env into the environment; these tests must see only their own values."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_production_refuses_unsigned_webhooks():
    with pytest.raises(ValidationError, match="not allowed when ENV=production"):
        _settings(env="production", whatsapp_webhook_signature="none")
    assert _settings(env="production", whatsapp_webhook_signature="meta", meta_app_secret="s3cret")


def test_a_chosen_provider_or_verifier_needs_its_secrets():
    with pytest.raises(ValidationError, match="META_APP_SECRET"):
        _settings(whatsapp_webhook_signature="meta")
    with pytest.raises(ValidationError, match="KAPSO_WEBHOOK_SECRET"):
        _settings(whatsapp_webhook_signature="kapso")
    with pytest.raises(ValidationError, match="KAPSO_API_KEY"):
        _settings(whatsapp_provider="kapso", whatsapp_phone_number_id="1")
    with pytest.raises(ValidationError, match="WHATSAPP_PHONE_NUMBER_ID"):
        _settings(whatsapp_provider="meta", meta_access_token="t")


def test_api_base_defaults_per_provider_and_can_be_overridden():
    kapso = _settings(whatsapp_provider="kapso", kapso_api_key="k", whatsapp_phone_number_id="1")
    meta = _settings(whatsapp_provider="meta", meta_access_token="t", whatsapp_phone_number_id="1")
    assert kapso.api_base == "https://api.kapso.ai/meta/whatsapp/v24.0"
    assert meta.api_base == "https://graph.facebook.com/v24.0"
    custom = _settings(whatsapp_provider="meta", meta_access_token="t", whatsapp_phone_number_id="1",
                       whatsapp_api_base="https://waba.example.test/v1/")
    assert custom.api_base == "https://waba.example.test/v1"
