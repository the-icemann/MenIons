"""Mobile money collection (pawaPay) for plan payments."""

from __future__ import annotations

from app.config import get_settings
from app.payments.base import Gateway

_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        from app.payments.pawapay import PawaPay

        settings = get_settings()
        _gateway = PawaPay(settings.pawapay_api_base, settings.pawapay_api_token)
    return _gateway


def set_gateway(gateway: Gateway | None) -> None:
    """Tests swap in a FakeGateway."""
    global _gateway
    _gateway = gateway


def automatic() -> bool:
    return get_settings().payments_provider == "pawapay"
