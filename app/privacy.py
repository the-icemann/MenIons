"""Personal data: the public privacy notice, the scheduled clean-up, and requests to see or
delete what we hold about a phone number (Data Protection and Privacy Act, 2019).

Receipts and payment records are never deleted: they are financial records, sealed by the
receipts hash chain, and tax law requires them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import get_settings
from app.models import (
    AdminAlert,
    Conversation,
    Deposit,
    Media,
    Merchant,
    Order,
    OrderStatus,
    Receipt,
    Rider,
    WebhookEvent,
)

log = logging.getLogger(__name__)
PRIVACY_UPDATED = "22 November 2026"
# FAILED counts as finished here: it only moves on if an admin reschedules it, which may never happen.
CLOSED = (OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.FAILED)
OPEN = tuple(s for s in OrderStatus if s not in CLOSED)
PURGED = {"purged": True}
REMOVED = "(removed)"


def privacy_link() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/privacy"


# --- The notice --------------------------------------------------------------------------

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/privacy", response_class=HTMLResponse)
def privacy_notice(request: Request):
    settings = get_settings()
    return _templates.TemplateResponse(
        request,
        "privacy.html",
        {
            "company": {
                "name": settings.company_name,
                "address": settings.company_address,
                "tin": settings.company_tin,
            },
            "contact": settings.privacy_contact or settings.company_phone,
            "retention_days": settings.data_retention_days,
            "updated": PRIVACY_UPDATED,
        },
    )


# --- Clean-up -----------------------------------------------------------------------------


def _delete_files(rows: list[Media]) -> int:
    folder = Path(get_settings().media_dir).resolve()
    removed = 0
    for media in rows:
        path = Path(media.url)
        if media.url.startswith(("http:", "https:", "whatsapp-media:")):
            continue
        try:
            resolved = path.resolve()
            if resolved.is_relative_to(folder) and resolved.is_file():
                resolved.unlink()
                removed += 1
        except OSError:
            log.warning("could not delete %s", media.url)
    return removed


def _purge_orders(session: Session, orders: list[Order], now: datetime) -> int:
    """Remove a finished order's location, directions, notes and photos."""
    if not orders:
        return 0
    ids = [o.id for o in orders]
    media = list(session.execute(select(Media).where(Media.order_id.in_(ids))).scalars())
    _delete_files(media)
    session.execute(delete(Media).where(Media.order_id.in_(ids)))
    for order in orders:
        order.drop_lat = order.drop_lng = None
        order.drop_text = None
        order.notes = None
        order.purged_at = now
    session.flush()
    return len(media)


def purge_old_data(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Delete what we no longer need, DATA_RETENTION_DAYS after it stopped being needed."""
    now = now or utcnow()
    cutoff = now - timedelta(days=get_settings().data_retention_days)
    orders = list(
        session.execute(
            select(Order)
            .where(Order.status.in_(CLOSED), Order.updated_at < cutoff, Order.purged_at.is_(None))
            .limit(2000)
        ).scalars()
    )
    photos = _purge_orders(session, orders, now)
    messages = session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.received_at < cutoff, ~WebhookEvent.raw.has_key("purged"))
        .values(raw=PURGED)
    ).rowcount
    alerts = session.execute(
        update(AdminAlert).where(AdminAlert.at < cutoff, ~AdminAlert.data.has_key("purged")).values(data=PURGED)
    ).rowcount
    conversations = session.execute(
        update(Conversation)
        .where(Conversation.updated_at < cutoff, or_(Conversation.step.is_not(None), Conversation.context != {}))
        .values(step=None, context={})
    ).rowcount
    session.flush()
    result = {"orders": len(orders), "photos": photos, "messages": messages, "alerts": alerts,
              "conversations": conversations}  # fmt: skip
    log.info("data clean-up: %s", result)
    return result


# --- A person's own data --------------------------------------------------------------------


def _mentions(phone: str):
    """Stored webhook rows that contain this number."""
    return cast(WebhookEvent.raw, String).contains(phone.lstrip("+"))


def describe(session: Session, phone: str) -> str:
    """What we hold about a phone number, for answering an access request."""
    merchant = session.execute(select(Merchant).where(Merchant.phone == phone)).scalar_one_or_none()
    rider = session.execute(select(Rider).where(Rider.phone == phone)).scalar_one_or_none()
    as_customer = session.execute(select(func.count(Order.id)).where(Order.customer_phone == phone)).scalar_one()
    receipts = session.execute(select(func.count(Receipt.id)).where(Receipt.recipient_phone == phone)).scalar_one()
    deposits = session.execute(select(func.count(Deposit.id)).where(Deposit.payer_phone == phone)).scalar_one()
    messages = session.execute(select(func.count(WebhookEvent.id)).where(_mentions(phone))).scalar_one()
    has_chat = session.get(Conversation, phone) is not None

    lines = [f"*Data held for {phone}*"]
    if merchant:
        lines.append(
            f"Shop: {merchant.shop_name}; owner {merchant.owner_name}; sells {merchant.sells or '-'}; "
            f"pickup {merchant.pickup_text or '-'} (pin {'yes' if merchant.pickup_lat else 'no'}); "
            f"TIN {merchant.tin or 'none'}; status {merchant.status}"
        )
        shop_orders = session.execute(select(func.count(Order.id)).where(Order.merchant_id == merchant.id)).scalar_one()
        lines.append(f"Deliveries booked by the shop: {shop_orders}")
    if rider:
        lines.append(
            f"Rider: {rider.name}; permit {rider.permit_number or '-'}; ID {rider.national_id or '-'}; "
            f"plate {rider.plate or '-'}"
        )
    lines.append(f"Deliveries to this number: {as_customer}")
    lines.append(f"Receipts addressed to it: {receipts} (kept: financial records)")
    lines.append(f"Mobile Money payments from it: {deposits} (kept: financial records)")
    lines.append(f"Stored WhatsApp messages mentioning it: {messages}")
    lines.append(f"Conversation state: {'yes' if has_chat else 'none'}")
    return "\n".join(lines)


class ForgetRefused(Exception):
    pass


def forget(session: Session, phone: str, now: datetime | None = None) -> str:
    """Delete what we hold about a phone number, except receipts and payment records.

    Refused while the person has a delivery in progress: finish or cancel it first.
    """
    now = now or utcnow()
    merchant = session.execute(select(Merchant).where(Merchant.phone == phone)).scalar_one_or_none()
    rider = session.execute(select(Rider).where(Rider.phone == phone)).scalar_one_or_none()

    open_q = select(func.count(Order.id)).where(Order.status.in_(OPEN))
    busy = session.execute(open_q.where(Order.customer_phone == phone)).scalar_one()
    if merchant:
        busy += session.execute(open_q.where(Order.merchant_id == merchant.id)).scalar_one()
    if rider:
        busy += session.execute(open_q.where(Order.rider_id == rider.id)).scalar_one()
    if busy:
        raise ForgetRefused(f"{busy} delivery(ies) still in progress for this number; finish or cancel them first")

    done = []
    as_customer = list(session.execute(select(Order).where(Order.customer_phone == phone)).scalars())
    if as_customer:
        _purge_orders(session, as_customer, now)
        for order in as_customer:
            order.customer_name = REMOVED
            order.customer_phone = "removed"
        done.append(f"{len(as_customer)} delivery(ies) to this number: name, number, location and photos removed")
    if merchant:
        merchant.owner_name = REMOVED
        merchant.sells = None
        merchant.pickup_text = None
        merchant.pickup_lat = merchant.pickup_lng = None
        merchant.tin = None
        merchant.status = "SUSPENDED"
        merchant.active = False
        merchant.phone = f"removed-m{merchant.id}"
        done.append(f"shop {merchant.shop_name}: closed, owner and pickup details removed")
    if rider:
        rider.name = REMOVED
        rider.permit_number = rider.national_id = rider.plate = None
        rider.active = rider.on_shift = False
        rider.phone = f"removed-r{rider.id}"
        done.append("rider account: closed, name and documents removed")
    messages = session.execute(update(WebhookEvent).where(_mentions(phone)).values(raw=PURGED)).rowcount
    if messages:
        done.append(f"{messages} stored WhatsApp message(s) deleted")
    if session.get(Conversation, phone) is not None:
        session.execute(delete(Conversation).where(Conversation.phone == phone))
        done.append("conversation state deleted")
    session.flush()
    if not done:
        return f"We hold nothing about {phone} apart from any receipts and payments."
    return (
        "Deleted for " + phone + ":\n• " + "\n• ".join(done) + "\nKept: receipts and payment records (required by law)."
    )
