"""Kapso's own webhook format (kind=kapso, payload v2) -> normalized events.

The Kapso sandbox refuses Meta-format webhooks, so this reader exists alongside
cloud_api_adapter. One request carries one message:

    {"message": {...Meta's message object..., "kapso": {"direction": ..., "status": ...}},
     "conversation": {"phone_number": ...}, "phone_number_id": ...}

- direction "inbound": a message from a person. message itself has Meta's shape (from, type,
  text, interactive, location, image...), so it is read by cloud_api_adapter's message reader.
  Note kapso.status is "delivered" on inbound messages; it is not a delivery status.
- direction "outbound": a status change on a message we sent (whatsapp.message.sent,
  .delivered, .read, .failed). The status is kapso.status; errors are on the last entry of
  kapso.statuses.

Checked against a real inbound text from the sandbox (tests/fixtures/kapso_text.json). The
outbound and location fixtures follow Kapso's documented examples until real ones are captured.
Nothing here raises: anything unrecognised becomes an UnknownEvent so it is still stored.
"""

from datetime import datetime
from typing import Any

from app.clock import utcnow
from app.cloud_api_adapter import STATUSES, _message
from app.events import InboundEvent, StatusUpdate, UnknownEvent


def is_kapso_payload(raw: Any) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("message"), dict) and "entry" not in raw


def _outbound(msg: dict[str, Any], at: datetime) -> InboundEvent:
    kapso = msg.get("kapso") or {}
    status = kapso.get("status")
    if status not in STATUSES:
        return UnknownEvent(raw=msg, provider_message_id=None, received_at=at)
    history = kapso.get("statuses") or []
    errors = (history[-1].get("errors") if history and isinstance(history[-1], dict) else None) or []
    return StatusUpdate(
        provider_message_id=msg["id"],
        received_at=at,
        message_id=msg["id"],
        status=status,
        error=errors[0] if errors else None,
    )


def _inbound(msg: dict[str, Any], conversation: dict[str, Any], at: datetime) -> InboundEvent:
    if not msg.get("from") and conversation.get("phone_number"):
        msg = {**msg, "from": conversation["phone_number"]}
    return _message(msg, at)


def parse(raw: Any, received_at: datetime | None = None) -> list[InboundEvent]:
    """Always returns exactly one event."""
    at = received_at or utcnow()
    msg = raw.get("message") if isinstance(raw, dict) else None
    if not isinstance(msg, dict):
        return [UnknownEvent(raw=raw, provider_message_id=None, received_at=at)]

    direction = (msg.get("kapso") or {}).get("direction")
    try:
        if direction == "inbound":
            return [_inbound(msg, raw.get("conversation") or {}, at)]
        if direction == "outbound":
            return [_outbound(msg, at)]
    except Exception:  # noqa: BLE001 - malformed input is stored, never raised
        pass
    item_id = msg.get("id") if direction == "inbound" else None
    return [UnknownEvent(raw=msg, provider_message_id=item_id, received_at=at)]
