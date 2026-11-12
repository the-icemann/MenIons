"""Inbound event -> who sent it -> handler. The handlers are stubs until milestone 2."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.events import InboundEvent, StatusUpdate, UnknownEvent
from app.models import AdminAlert, Conversation, Merchant, MerchantStatus, Order, OrderStatus, Rider, Role
from app.phones import mask_phone

log = logging.getLogger(__name__)

FREE_WINDOW = timedelta(hours=24)
# A customer is "known" while one of their orders is still in play.
CLOSED_STATUSES = (OrderStatus.DELIVERED, OrderStatus.CANCELLED)


def resolve_role(session: Session, phone: str) -> Role:
    """An ACTIVE merchant, then a PENDING merchant, then a rider, then the customer of an
    open order. Otherwise unknown."""
    status = session.execute(select(Merchant.status).where(Merchant.phone == phone)).scalar()
    if status == MerchantStatus.ACTIVE:
        return Role.MERCHANT
    if status == MerchantStatus.PENDING:
        return Role.MERCHANT_PENDING
    if session.execute(select(Rider.id).where(Rider.phone == phone)).scalar():
        return Role.RIDER
    open_order = select(Order.id).where(Order.customer_phone == phone, Order.status.not_in(CLOSED_STATUSES)).limit(1)
    if session.execute(open_order).scalar():
        return Role.CUSTOMER
    return Role.UNKNOWN


def touch_conversation(session: Session, phone: str, role: Role, at: datetime) -> Conversation:
    """Create or update the conversation row: set the role and when this phone last wrote to us.
    step and context are left alone."""
    stmt = insert(Conversation).values(phone=phone, role=str(role), context={}, last_inbound_at=at, updated_at=at)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Conversation.phone],
        set_={"role": str(role), "last_inbound_at": at, "updated_at": at},
    )
    session.execute(stmt)
    session.flush()
    return session.execute(
        select(Conversation).where(Conversation.phone == phone).execution_options(populate_existing=True)
    ).scalar_one()


def in_free_window(session: Session, phone: str, now: datetime) -> bool:
    """True within 24 hours of this phone's last message to us: we may send a normal message.
    Otherwise only an approved template can be sent."""
    last = session.execute(select(Conversation.last_inbound_at).where(Conversation.phone == phone)).scalar()
    return last is not None and now - last < FREE_WINDOW


def handle_merchant(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    log.info("merchant event %s from %s (stub)", type(event).__name__, mask_phone(conversation.phone))


def handle_merchant_pending(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    log.info("pending merchant event %s from %s (stub)", type(event).__name__, mask_phone(conversation.phone))


def handle_rider(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    log.info("rider event %s from %s (stub)", type(event).__name__, mask_phone(conversation.phone))


def handle_customer(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    log.info("customer event %s from %s (stub)", type(event).__name__, mask_phone(conversation.phone))


def handle_unknown(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    # Milestone 2: this becomes merchant self-onboarding.
    log.info("unknown sender event %s from %s (stub)", type(event).__name__, mask_phone(conversation.phone))


def handle_status(session: Session, event: StatusUpdate) -> None:
    if event.status == "failed":
        session.add(
            AdminAlert(
                kind="message_failed",
                at=event.received_at,
                data={"message_id": event.message_id, "error": event.error},
            )
        )
        session.flush()


def dispatch(session: Session, event: InboundEvent) -> None:
    if isinstance(event, StatusUpdate):
        handle_status(session, event)
        return
    if isinstance(event, UnknownEvent):
        log.warning("unrecognised webhook item stored, not handled (id=%s)", event.provider_message_id)
        return

    phone = event.phone  # type: ignore[attr-defined]  # every other event carries a phone
    role = resolve_role(session, phone)
    conversation = touch_conversation(session, phone, role, event.received_at)
    # Looked up by name at call time so a handler can be replaced (milestone 2, and tests).
    if role == Role.MERCHANT:
        handle_merchant(session, event, conversation)
    elif role == Role.MERCHANT_PENDING:
        handle_merchant_pending(session, event, conversation)
    elif role == Role.RIDER:
        handle_rider(session, event, conversation)
    elif role == Role.CUSTOMER:
        handle_customer(session, event, conversation)
    else:
        handle_unknown(session, event, conversation)
