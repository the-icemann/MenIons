"""Outgoing messages. Handlers queue them on an Outbox while they work; the webhook sends them
only after the database transaction commits, so nobody is told about something that then
rolls back.

WhatsApp allows a normal message only within 24 hours of the person's last message to us.
Outside that window an approved template is needed: if WHATSAPP_UPDATE_TEMPLATE is set, the
message goes out through it; otherwise it is skipped and an admin alert records what was not
sent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import get_settings
from app.models import AdminAlert, Receipt
from app.phones import mask_phone
from app.whatsapp import build_provider
from app.whatsapp.base import Section, WhatsAppProvider

log = logging.getLogger(__name__)

MAX_BODY = 1024  # WhatsApp's limit for interactive message bodies
MAX_TEXT = 4096

_provider: WhatsAppProvider | None = None


def get_provider() -> WhatsAppProvider:
    global _provider
    if _provider is None:
        _provider = build_provider(get_settings())
    return _provider


def set_provider(provider: WhatsAppProvider | None) -> None:
    """Tests swap in a FakeProvider."""
    global _provider
    _provider = provider


@dataclass
class Outgoing:
    phone: str
    kind: str  # text | buttons | list | location_request
    body: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    button_label: str = ""
    sections: list[Section] = field(default_factory=list)
    receipt_id: int | None = None
    # A courtesy message: when it can't be sent (outside the window, no template), skip it
    # quietly instead of recording an admin alert.
    optional: bool = False


@dataclass
class Outbox:
    items: list[Outgoing] = field(default_factory=list)

    def text(self, phone: str, body: str, *, receipt_id: int | None = None, optional: bool = False) -> None:
        self.items.append(Outgoing(phone, "text", body[:MAX_TEXT], receipt_id=receipt_id, optional=optional))

    def buttons(self, phone: str, body: str, buttons: list[tuple[str, str]], *, optional: bool = False) -> None:
        self.items.append(Outgoing(phone, "buttons", body[:MAX_BODY], buttons=buttons, optional=optional))

    def list(self, phone: str, body: str, button_label: str, sections: list[Section]) -> None:
        self.items.append(Outgoing(phone, "list", body[:MAX_BODY], button_label=button_label, sections=sections))

    def location_request(self, phone: str, body: str, *, optional: bool = False) -> None:
        self.items.append(Outgoing(phone, "location_request", body[:MAX_BODY], optional=optional))


def outbox(session: Session) -> Outbox:
    """The outbox for the work going on in this session."""
    return session.info.setdefault("outbox", Outbox())


def take_outbox(session: Session) -> list[Outgoing]:
    box = session.info.pop("outbox", None)
    return box.items if box else []


def discard_outbox(session: Session) -> None:
    session.info.pop("outbox", None)


def _template_text(body: str) -> str:
    # Template parameters may not contain newlines, tabs or more than four spaces in a row.
    flat = re.sub(r"\s*\n+\s*", " · ", body.strip())
    return re.sub(r"[\t ]{2,}", " ", flat)[:1000]


def _send(provider: WhatsAppProvider, item: Outgoing) -> str:
    if item.kind == "buttons":
        return provider.send_buttons(item.phone, item.body, item.buttons)
    if item.kind == "list":
        return provider.send_list(item.phone, item.body, item.button_label, item.sections)
    if item.kind == "location_request":
        return provider.send_location_request(item.phone, item.body)
    return provider.send_text(item.phone, item.body)


def deliver(session: Session, items: list[Outgoing], now: datetime | None = None) -> None:
    """Send queued messages, then commit what sending records (receipts marked sent, alerts).
    A failure on one message never stops the others."""
    from app import receipts
    from app.router import in_free_window

    if not items:
        return
    now = now or utcnow()
    settings = get_settings()
    provider = get_provider()
    for item in items:
        try:
            if in_free_window(session, item.phone, now):
                _send(provider, item)
            elif settings.whatsapp_update_template:
                component = {"type": "body", "parameters": [{"type": "text", "text": _template_text(item.body)}]}
                provider.send_template(
                    item.phone, settings.whatsapp_update_template, settings.whatsapp_template_language, [component]
                )
            else:
                if not item.optional:
                    session.add(
                        AdminAlert(
                            kind="message_not_sent",
                            at=now,
                            data={"phone": item.phone, "reason": "outside_24h_window", "text": item.body[:500]},
                        )
                    )
                continue
            if item.receipt_id is not None:
                receipt = session.get(Receipt, item.receipt_id)
                if receipt is not None and receipt.sent_at is None:
                    receipts.mark_sent(session, receipt, now)
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the rest
            log.exception("sending to %s failed", mask_phone(item.phone))
            session.add(
                AdminAlert(kind="message_send_error", at=now, data={"phone": item.phone, "error": repr(exc)[:500]})
            )
    session.commit()


def save_media(media_id: str, name: str) -> str:
    """Download a WhatsApp photo and keep it. Returns where it is: a file path, or, if the
    download fails, a reference to the media id so it can be fetched later (ids last 30 days)."""
    try:
        data = get_provider().download_media(media_id)
        folder = Path(get_settings().media_dir)
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "photo"  # part of it comes from the webhook
        path = folder / f"{safe}.jpg"
        path.write_bytes(data)
        return str(path)
    except Exception:  # noqa: BLE001
        log.warning("media %s not downloaded; keeping the reference", media_id)
        return f"whatsapp-media:{media_id}"
