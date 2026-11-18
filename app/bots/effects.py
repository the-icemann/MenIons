"""Turn the order state machine's side effects into queued messages.

orders.transition() and create_order() return SideEffects instead of sending anything. The
bots pass them here; the messages go out after the transaction commits (app.messaging).
`actor_phone` is the person who made the change: they get the bot's own reply, so
notifications addressed to them are skipped (receipts are still sent).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import eta
from app.bots.common import box, chat_link, local_phone, maps_link, receipt_link
from app.config import get_settings
from app.models import AdminAlert, Merchant, Order, Receipt, Rider
from app.orders import SideEffect
from app.plans import priority_riders
from app.receipts import TITLES


def notify_admins(session: Session, text: str) -> None:
    for phone in get_settings().admin_phone_list:
        box(session).text(phone, text)


def job_card(order: Order, merchant: Merchant) -> str:
    lines = [
        f"🛵 Job {order.code}",
        f"Pick up: {merchant.shop_name}, {local_phone(merchant.phone)}",
        f"   {merchant.pickup_text or ''}".rstrip(),
        f"   {maps_link(merchant.pickup_lat, merchant.pickup_lng)}",
        f"Package: {order.package}",
        f"Deliver to: {order.customer_name}, {local_phone(order.customer_phone)}",
        f"   {order.drop_text or ''}".rstrip(),
        f"   {maps_link(order.drop_lat, order.drop_lng)}",
    ]
    if order.notes:
        lines.append(f"Notes: {order.notes}")
    return "\n".join(line for line in lines if line.strip())


def arrival(order: Order, merchant: Merchant, *, picked_up: bool) -> str:
    """'about 20–40 min', or '' if a pin is missing."""
    minutes = eta.estimate_minutes(
        (merchant.pickup_lat, merchant.pickup_lng), (order.drop_lat, order.drop_lng), picked_up=picked_up
    )
    return eta.describe(minutes)


def customer_location_request(order: Order, merchant: Merchant) -> str:
    return (
        f"Hi {order.customer_name} 👋 {merchant.shop_name} is sending you {order.package} "
        f"with Men-Ions (order {order.code}).\n\n"
        "Where should our rider bring it? Tap *Send location* and choose where you are, "
        "or 📎 → Location."
    )


def booking_link_message(order: Order) -> str:
    link = chat_link(order.code)
    if link:
        how = f"Send your customer this link. It opens WhatsApp with a message to us:\n{link}"
    else:
        how = f"Ask your customer to send *{order.code}* to this WhatsApp number."
    return (
        f"{how}\n\nWhen they share their location we'll get a rider. "
        "If we can already reach them on WhatsApp, we've asked them too."
    )


def _riders_to_offer(session: Session, merchant: Merchant) -> list[Rider]:
    riders = list(
        session.execute(
            select(Rider).where(Rider.active.is_(True), Rider.on_shift.is_(True)).order_by(Rider.id)
        ).scalars()
    )
    first = set(priority_riders(merchant))
    return sorted(riders, key=lambda r: (r.id not in first, r.id))


def run(session: Session, effects: list[SideEffect], actor_phone: str | None = None) -> None:
    for effect in effects:
        order = session.get(Order, effect.order_id)
        merchant = session.get(Merchant, order.merchant_id)
        rider = session.get(Rider, order.rider_id) if order.rider_id else None

        def tell(phone: str | None, text: str, *, optional: bool = False) -> None:
            if phone and phone != actor_phone:
                box(session).text(phone, text, optional=optional)

        kind = effect.kind
        if kind == "send_receipt":
            receipt = session.get(Receipt, effect.receipt_id)
            if receipt.recipient_phone:
                title = TITLES.get(receipt.type, "Receipt")
                box(session).text(
                    receipt.recipient_phone,
                    f"🧾 {title} {receipt.number} for {order.code}:\n{receipt_link(receipt)}",
                    receipt_id=receipt.id,
                )

        elif kind == "notify_customer_booked":
            # Usually outside the 24-hour window: the merchant also gets a link to pass on.
            if order.customer_phone != actor_phone:
                box(session).location_request(
                    order.customer_phone, customer_location_request(order, merchant), optional=True
                )

        elif kind == "notify_riders_job_available":
            riders = _riders_to_offer(session, merchant)
            text = (
                f"🆕 Job {order.code}\nPick up: {merchant.shop_name}, {merchant.pickup_text or ''}\n"
                f"Deliver: {order.drop_text or maps_link(order.drop_lat, order.drop_lng)}\n"
                f"Package: {order.package}\n\nFirst to accept gets it."
            )
            for r in riders:
                box(session).buttons(r.phone, text, [(f"acc:{order.id}", "Accept job")], optional=True)
            tell(
                merchant.phone,
                f"📍 {order.customer_name} shared their location for {order.code}. Finding a rider…",
                optional=True,
            )
            if not riders:
                session.add(AdminAlert(kind="no_rider_on_shift", order_id=order.id, data={"order_code": order.code}))
                notify_admins(
                    session,
                    f"⚠️ {order.code} is ready but no rider is on shift. Assign: /assign {order.code} <rider phone>",
                )

        elif kind == "notify_rider_assigned":
            if rider:
                box(session).buttons(rider.phone, job_card(order, merchant), [(f"pu:{order.id}", "Picked up")])

        elif kind == "notify_merchant_rider_assigned":
            if rider:
                tell(
                    merchant.phone,
                    f"🛵 {rider.name} ({local_phone(rider.phone)}) is coming to pick up {order.code} "
                    f"for {order.customer_name}.",
                    optional=True,
                )

        elif kind == "notify_customer_picked_up":
            if rider:
                eta_text = arrival(order, merchant, picked_up=True)
                expect = f"\nExpected in {eta_text}." if eta_text else ""
                tell(
                    order.customer_phone,
                    f"📦 Your parcel from {merchant.shop_name} ({order.code}) is on the way with "
                    f"{rider.name}, {local_phone(rider.phone)}.{expect}\n\n"
                    f"Your delivery code is *{order.drop_code}*. Give it to the rider only once you have the parcel.",
                )
                tell(
                    merchant.phone,
                    f"📦 {order.code} picked up by {rider.name}, on the way to {order.customer_name}.{expect}",
                    optional=True,
                )

        elif kind == "notify_admin_delivery_failed":
            notify_admins(
                session,
                f"⚠️ Delivery failed: {order.code} ({merchant.shop_name} → {order.customer_name}). "
                f"Rider: {rider.name if rider else '-'}.\nReschedule: /reschedule {order.code}  Cancel: /cancel {order.code}",
            )

        elif kind == "notify_order_cancelled":
            tell(order.customer_phone, f"Order {order.code} from {merchant.shop_name} was cancelled.", optional=True)
            tell(merchant.phone, f"Order {order.code} for {order.customer_name} was cancelled.", optional=True)
            if rider:
                tell(
                    rider.phone,
                    f"❌ Job {order.code} was cancelled. Don't pick it up; if you have it, return it to {merchant.shop_name}.",
                )


def send_receipts(session: Session, receipts: list[Receipt], about: str) -> None:
    """For receipts not tied to an order (a plan purchase)."""
    for receipt in receipts:
        if receipt.recipient_phone:
            title = TITLES.get(receipt.type, "Receipt")
            box(session).text(
                receipt.recipient_phone,
                f"🧾 {title} {receipt.number} for {about}:\n{receipt_link(receipt)}",
                receipt_id=receipt.id,
            )
