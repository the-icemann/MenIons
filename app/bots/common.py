"""Small helpers every bot uses: reading what the person sent, conversation steps, wording."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.clock import format_kampala
from app.config import get_settings
from app.events import ButtonReply, InboundEvent, ListReply, TextMessage
from app.messaging import Outbox, outbox
from app.models import Conversation, OrderStatus, Receipt

# Words that drop whatever the person was doing and show their menu.
RESET_WORDS = frozenset({"menu", "hi", "hello", "hey", "start", "cancel", "stop", "back", "home", "help"})

STATUS_LABELS = {
    OrderStatus.BOOKED: "Waiting for the customer's location",
    OrderStatus.READY: "Finding a rider",
    OrderStatus.ASSIGNED: "Rider on the way to pick up",
    OrderStatus.PICKED_UP: "Out for delivery",
    OrderStatus.DELIVERED: "Delivered",
    OrderStatus.CANCELLED: "Cancelled",
    OrderStatus.FAILED: "Delivery failed",
}


def box(session: Session) -> Outbox:
    return outbox(session)


def text_of(event: InboundEvent) -> str:
    if isinstance(event, TextMessage):
        return event.text.strip()
    return ""


def choice_of(event: InboundEvent) -> str:
    """The id of the button or list row tapped, or ''."""
    if isinstance(event, ButtonReply):
        return event.button_id
    if isinstance(event, ListReply):
        return event.row_id
    return ""


def is_reset(event: InboundEvent) -> bool:
    return text_of(event).lower().strip(" !.") in RESET_WORDS


def step_of(conversation: Conversation, *prefixes: str) -> str:
    """The current step if it belongs to one of these flows, else ''. Steps are named
    '<flow>:<step>', so a step left over from another role is ignored."""
    step = conversation.step or ""
    return step if any(step.startswith(p) for p in prefixes) else ""


def ctx(conversation: Conversation) -> dict:
    return dict(conversation.context or {})


def go(conversation: Conversation, step: str | None, **values) -> None:
    """Move to a step, keeping the context and adding `values`."""
    conversation.step = step
    conversation.context = {**ctx(conversation), **values}  # a new dict, so the change is saved


def clear(conversation: Conversation) -> None:
    """End the current flow. Keeps only the test-mode role override."""
    keep = {k: v for k, v in ctx(conversation).items() if k == "as_role"}
    conversation.step = None
    conversation.context = keep


def parse_ugx(raw: str) -> int | None:
    """'85,000' -> 85000, '85k' -> 85000, '0' / 'none' / 'paid' -> 0. None if not an amount."""
    value = raw.strip().lower().replace("ugx", "").replace("shs", "").replace("/=", "").strip()
    if value in ("0", "none", "no", "nothing", "paid", "already paid", "nil"):
        return 0
    match = re.fullmatch(r"(\d[\d,. ]*)\s*(k)?", value)
    if not match:
        return None
    digits = re.sub(r"[,. ]", "", match.group(1))
    if not digits.isdigit():
        return None
    amount = int(digits) * (1000 if match.group(2) else 1)
    return amount if amount <= 50_000_000 else None


def ugx(amount: int) -> str:
    return f"UGX {amount:,}"


def local_phone(phone: str) -> str:
    """+256772123456 -> 0772 123456, easier to read and to dial."""
    if phone.startswith("+256") and len(phone) == 13:
        return f"0{phone[4:7]} {phone[7:]}"
    return phone


def maps_link(lat: float | None, lng: float | None) -> str:
    if lat is None or lng is None:
        return "(no pin yet)"
    return f"https://maps.google.com/?q={lat:.6f},{lng:.6f}"


def chat_link(prefilled: str) -> str | None:
    """A click-to-chat link that opens WhatsApp with our number and a message ready to send."""
    number = re.sub(r"\D", "", get_settings().whatsapp_display_number)
    return f"https://wa.me/{number}?text={quote(prefilled)}" if number else None


def receipt_link(receipt: Receipt) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/r/{receipt.number}?k={receipt.view_token}"


def when(at: datetime | None) -> str:
    return format_kampala(at) if at else "-"


def short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
