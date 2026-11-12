"""Webhook signature verifiers. Each takes the raw request body and the headers and says
whether the request really came from the provider. Small and isolated on purpose."""

import hashlib
import hmac
from collections.abc import Callable, Mapping

from app.config import Settings

Verifier = Callable[[bytes, Mapping[str, str]], bool]


def _hmac_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_meta(secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    """Meta: X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body, keyed by the app secret>."""
    given = headers.get("x-hub-signature-256", "")
    if not secret or not given.startswith("sha256="):
        return False
    return hmac.compare_digest(given.removeprefix("sha256=").strip().lower(), _hmac_hex(secret, body))


def verify_kapso(secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    """Kapso: X-Webhook-Signature: <hex HMAC-SHA256 of the raw body, keyed by the webhook secret>.

    Kapso's webhook security page documents this for its own webhook format.
    TODO(kapso-docs): confirm which signature header and algorithm Kapso uses on
    Meta-format forwarded webhooks. If it relays Meta's X-Hub-Signature-256 instead, set
    WHATSAPP_WEBHOOK_SIGNATURE=meta.
    """
    given = headers.get("x-webhook-signature", "").strip().lower().removeprefix("sha256=")
    if not secret or not given:
        return False
    return hmac.compare_digest(given, _hmac_hex(secret, body))


def get_verifier(settings: Settings) -> Verifier:
    mode = settings.whatsapp_webhook_signature
    if mode == "meta":
        return lambda body, headers: verify_meta(settings.meta_app_secret, body, headers)
    if mode == "kapso":
        return lambda body, headers: verify_kapso(settings.kapso_webhook_secret, body, headers)
    # "none": development only. Settings refuses it when ENV=production.
    return lambda body, headers: True
