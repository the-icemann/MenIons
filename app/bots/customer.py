"""Customers: share the delivery location for their order, add directions, ask where it is."""

from __future__ import annotations

import re

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app import orders
from app.bots import effects
from app.bots.common import STATUS_LABELS, box, clear, ctx, go, local_phone, step_of, text_of
from app.events import InboundEvent, LocationShared
from app.models import Conversation, Merchant, Order, OrderStatus, Rider
from app.orders import InvalidTransition, TransitionConditionFailed
from app.router import CLOSED_STATUSES

S = OrderStatus
_CODE = re.compile(r"\bMNI-\d{4,}\b", re.IGNORECASE)


def _open_orders(session: Session, phone: str) -> list[Order]:
    # Orders still waiting for a location first, then the newest.
    return list(
        session.execute(
            select(Order)
            .where(Order.customer_phone == phone, Order.status.not_in(CLOSED_STATUSES))
            .order_by(case((Order.status == S.BOOKED, 0), else_=1), Order.id.desc())
        ).scalars()
    )


def _pick(session: Session, event: InboundEvent, conversation: Conversation) -> Order | None:
    found = _open_orders(session, conversation.phone)
    if not found:
        return None
    mentioned = _CODE.search(text_of(event))
    if mentioned:
        for order in found:
            if order.code == mentioned.group(0).upper():
                return order
    remembered = ctx(conversation).get("order_id")
    if remembered and not isinstance(event, LocationShared):
        for order in found:
            if order.id == remembered:
                return order
    return found[0]


def _status_text(session: Session, order: Order) -> str:
    merchant = session.get(Merchant, order.merchant_id)
    lines = [f"*{order.code}* from {merchant.shop_name}: {STATUS_LABELS[S(order.status)]}."]
    if order.rider_id:
        rider = session.get(Rider, order.rider_id)
        lines.append(f"Rider: {rider.name}, {local_phone(rider.phone)}.")
    eta_text = effects.arrival(order, merchant, picked_up=order.status == S.PICKED_UP)
    if order.status in (S.READY, S.ASSIGNED) and eta_text:
        lines.append(f"Estimated arrival: {eta_text}.")
    elif order.status == S.PICKED_UP and eta_text:
        lines.append(f"From the shop it usually takes {eta_text}.")
    if order.status in (S.READY, S.ASSIGNED, S.PICKED_UP):
        lines.append(f"Your delivery code is *{order.drop_code}*. Give it to the rider only once you have the parcel.")
    return "\n".join(lines)


def handle_customer(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    phone, out = conversation.phone, box(session)
    order = _pick(session, event, conversation)
    if order is None:
        clear(conversation)
        out.text(phone, "We don't have an open delivery for this number.")
        return
    merchant = session.get(Merchant, order.merchant_id)

    if order.status == S.BOOKED:
        if not isinstance(event, LocationShared):
            go(conversation, "c:pin", order_id=order.id)
            out.location_request(phone, effects.customer_location_request(order, merchant))
            return
        order.drop_lat, order.drop_lng = event.lat, event.lng
        named = ", ".join(p for p in (event.name, event.address) if p)
        if named:
            order.drop_text = named[:200]
        try:
            side_effects = orders.transition(session, order, S.READY, "customer", at=event.received_at)
        except (InvalidTransition, TransitionConditionFailed):
            out.text(phone, _status_text(session, order))
            return
        go(conversation, "c:directions", order_id=order.id)
        eta_text = effects.arrival(order, merchant, picked_up=False)
        expect = f" Estimated arrival: {eta_text}." if eta_text else ""
        out.text(
            phone,
            f"📍 Thank you! We're finding a rider for {order.code}.{expect}\n\n"
            f"Your delivery code is *{order.drop_code}*. Give it to the rider only once you have the parcel.\n\n"
            "To help the rider, you can type directions now (landmark, gate colour, floor).",
        )
        effects.run(session, side_effects, actor_phone=phone)
        return

    text = text_of(event)
    if step_of(conversation, "c:") == "c:directions" and text and ctx(conversation).get("order_id") == order.id:
        if order.status in (S.READY, S.ASSIGNED):
            order.drop_text = (f"{order.drop_text} · {text}" if order.drop_text else text)[:400]
            clear(conversation)
            out.text(phone, "👍 Thanks, the rider will see your directions.")
            return
    clear(conversation)
    out.text(phone, _status_text(session, order))
