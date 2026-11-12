"""WhatsApp Cloud API webhook payload -> normalized events.

Written against Meta's documented payload: entry[].changes[].value.messages[] and
.statuses[]. Nothing here raises on odd input: anything unrecognised becomes an
UnknownEvent so it is still stored.

TODO(kapso-docs): confirm that Kapso's Meta-format forwarding delivers this payload
unwrapped, then add real payloads captured from the Kapso sandbox as fixtures.
"""

import json
from datetime import datetime
from typing import Any

from app.clock import utcnow
from app.events import (
    ButtonReply,
    FlowSubmit,
    InboundEvent,
    ListReply,
    LocationShared,
    MediaReceived,
    StatusUpdate,
    TextMessage,
    UnknownEvent,
)
from app.phones import normalize_wa_id

MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")
STATUSES = ("sent", "delivered", "read", "failed")


def _message(msg: dict[str, Any], at: datetime) -> InboundEvent:
    base = {"provider_message_id": msg["id"], "received_at": at}
    phone = normalize_wa_id(msg["from"])
    kind = msg["type"]

    if kind == "text":
        return TextMessage(phone=phone, text=msg["text"]["body"], **base)

    if kind == "interactive":
        interactive = msg["interactive"]
        sub = interactive["type"]
        if sub == "button_reply":
            reply = interactive["button_reply"]
            return ButtonReply(phone=phone, button_id=reply["id"], title=reply.get("title", ""), **base)
        if sub == "list_reply":
            reply = interactive["list_reply"]
            return ListReply(phone=phone, row_id=reply["id"], title=reply.get("title", ""), **base)
        if sub == "nfm_reply":
            fields = json.loads(interactive["nfm_reply"].get("response_json") or "{}")
            if not isinstance(fields, dict):
                raise ValueError("flow response_json is not an object")
            return FlowSubmit(phone=phone, flow_token=fields.pop("flow_token", None), fields=fields, **base)

    if kind == "button":
        # A tap on a quick-reply button of a template message.
        button = msg["button"]
        return ButtonReply(phone=phone, button_id=button.get("payload", ""), title=button.get("text", ""), **base)

    if kind == "location":
        loc = msg["location"]
        return LocationShared(
            phone=phone,
            lat=float(loc["latitude"]),
            lng=float(loc["longitude"]),
            name=loc.get("name"),
            address=loc.get("address"),
            **base,
        )

    if kind in MEDIA_TYPES:
        media = msg[kind]
        return MediaReceived(phone=phone, kind=kind, media_id=media["id"], caption=media.get("caption"), **base)

    return UnknownEvent(raw=msg, **base)


def _status(status: dict[str, Any], at: datetime) -> InboundEvent:
    if status["status"] not in STATUSES:
        return UnknownEvent(raw=status, provider_message_id=None, received_at=at)
    errors = status.get("errors") or []
    return StatusUpdate(
        provider_message_id=status["id"],
        received_at=at,
        message_id=status["id"],
        status=status["status"],
        error=errors[0] if errors else None,
    )


def parse(raw: Any, received_at: datetime | None = None) -> list[InboundEvent]:
    """Always returns at least one event."""
    at = received_at or utcnow()
    events: list[InboundEvent] = []
    try:
        values = [change.get("value") or {} for entry in raw["entry"] for change in entry.get("changes", [])]
    except (KeyError, TypeError, AttributeError):
        values = []

    for value in values:
        for item, build in [(m, _message) for m in value.get("messages") or []] + [
            (s, _status) for s in value.get("statuses") or []
        ]:
            try:
                events.append(build(item, at))
            except Exception:  # noqa: BLE001 - a malformed item must not lose the others
                item_id = item.get("id") if isinstance(item, dict) and build is _message else None
                events.append(UnknownEvent(raw=item, provider_message_id=item_id, received_at=at))

    return events or [UnknownEvent(raw=raw, provider_message_id=None, received_at=at)]
