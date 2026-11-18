"""Riders: shifts, accepting jobs, pickup and delivery with photos and the drop code.
Riders never handle the customer's money: payment is between merchant and customer."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import orders
from app.bots import effects
from app.bots.common import box, choice_of, clear, ctx, go, is_reset, local_phone, short, step_of, text_of
from app.events import InboundEvent, MediaReceived
from app.messaging import save_media
from app.models import Conversation, Media, Merchant, Order, OrderStatus, Rider
from app.orders import DropCodeRejected, InvalidTransition, TransitionConditionFailed

S = OrderStatus
FAIL_REASONS = [
    ("unreachable", "Customer unreachable"),
    ("refused", "Customer refused"),
    ("wrong_address", "Wrong address"),
    ("other", "Other problem"),
]


def _rider(session: Session, phone: str) -> Rider:
    return session.execute(select(Rider).where(Rider.phone == phone)).scalar_one()


def _my_order(session: Session, rider: Rider, raw_id: str) -> Order | None:
    order = session.get(Order, int(raw_id)) if raw_id.isdigit() else None
    return order if order is not None and order.rider_id == rider.id else None


def show_menu(session: Session, conversation: Conversation, rider: Rider, intro: str = "") -> None:
    clear(conversation)
    shift = "🟢 You're on shift." if rider.on_shift else "⚪ You're off shift."
    toggle = ("r:off", "Go off shift") if rider.on_shift else ("r:on", "Go on shift")
    body = (intro + "\n\n" if intro else "") + f"Hi {rider.name}. {shift}"
    box(session).buttons(conversation.phone, body, [toggle, ("r:jobs", "My jobs"), ("r:open", "Available jobs")])


def _available(session: Session, conversation: Conversation, rider: Rider) -> None:
    ready = session.execute(select(Order).where(Order.status == S.READY).order_by(Order.id).limit(10)).scalars().all()
    if not ready:
        show_menu(session, conversation, rider, "No jobs waiting right now. We'll message you when one comes in.")
        return
    rows = []
    for order in ready:
        merchant = session.get(Merchant, order.merchant_id)
        rows.append({"id": f"acc:{order.id}", "title": order.code,
                     "description": short(f"{merchant.shop_name} → {order.customer_name} · {order.package}", 72)})  # fmt: skip
    box(session).list(
        conversation.phone, "Jobs waiting. Pick one to accept it:", "See jobs", [{"title": "Jobs", "rows": rows}]
    )


def _next_buttons(order: Order) -> list[tuple[str, str]]:
    if order.status == S.ASSIGNED:
        return [(f"pu:{order.id}", "Picked up")]
    return [(f"dl:{order.id}", "Delivered"), (f"fl:{order.id}", "Couldn't deliver")]


def _my_jobs(session: Session, conversation: Conversation, rider: Rider) -> None:
    mine = (
        session.execute(
            select(Order)
            .where(Order.rider_id == rider.id, Order.status.in_((S.ASSIGNED, S.PICKED_UP)))
            .order_by(Order.id)
        )
        .scalars()
        .all()
    )
    if not mine:
        show_menu(session, conversation, rider, "You have no jobs in progress.")
        return
    clear(conversation)
    for order in mine[:5]:
        merchant = session.get(Merchant, order.merchant_id)
        box(session).buttons(conversation.phone, effects.job_card(order, merchant), _next_buttons(order))


def _accept(session: Session, event: InboundEvent, conversation: Conversation, rider: Rider, raw_id: str) -> None:
    order = session.get(Order, int(raw_id)) if raw_id.isdigit() else None
    if order is None:
        show_menu(session, conversation, rider)
        return
    if not rider.on_shift:
        rider.on_shift = True
    try:
        side_effects = orders.transition(session, order, S.ASSIGNED, "rider", rider.id, at=event.received_at)
    except (InvalidTransition, TransitionConditionFailed):
        taken = "you" if order.rider_id == rider.id else "another rider"
        show_menu(session, conversation, rider, f"{order.code} was already taken by {taken}.")
        return
    clear(conversation)
    # notify_rider_assigned sends this rider the job card with the next button.
    effects.run(session, side_effects)


def _photo_step(session: Session, event: InboundEvent, conversation: Conversation, rider: Rider, step: str) -> None:
    phone, out = conversation.phone, box(session)
    order = _my_order(session, rider, str(ctx(conversation).get("order_id", "")))
    if order is None:
        show_menu(session, conversation, rider)
        return
    kind = "pickup" if step == "r:pickup_photo" else "delivery"
    if not (isinstance(event, MediaReceived) and event.kind == "image"):
        out.text(phone, f"Please send a *photo* of the parcel for {order.code} (📎 or 📷).")
        return
    url = save_media(event.media_id, f"{order.code}-{kind}-{event.provider_message_id or ''}"[:120])
    session.add(Media(order_id=order.id, kind=kind, url=url, at=event.received_at))
    session.flush()

    if kind == "pickup":
        try:
            side_effects = orders.transition(session, order, S.PICKED_UP, "rider", rider.id, at=event.received_at)
        except (InvalidTransition, TransitionConditionFailed):
            show_menu(session, conversation, rider, f"{order.code} can't be marked picked up now.")
            return
        clear(conversation)
        out.buttons(
            phone,
            f"✅ {order.code} picked up. Deliver to {order.customer_name}, {local_phone(order.customer_phone)}.",
            _next_buttons(order),
        )
        effects.run(session, side_effects, actor_phone=phone)
    else:
        go(conversation, "r:code", order_id=order.id)
        out.text(
            phone, f"📷 Photo saved. Now ask {order.customer_name} for their *4-digit delivery code* and type it here."
        )


def _code_step(session: Session, event: InboundEvent, conversation: Conversation, rider: Rider) -> None:
    phone, out = conversation.phone, box(session)
    order = _my_order(session, rider, str(ctx(conversation).get("order_id", "")))
    if order is None:
        show_menu(session, conversation, rider)
        return
    code = text_of(event).replace(" ", "")
    if not (code.isdigit() and len(code) == 4):
        out.text(phone, "Type the customer's 4-digit code, like 1234.")
        return
    try:
        side_effects = orders.transition(
            session, order, S.DELIVERED, "rider", rider.id, {"drop_code": code}, at=event.received_at
        )
    except DropCodeRejected as exc:
        # The wrong attempt is recorded; it must be committed, so nothing here raises.
        if exc.reason == "drop_code_locked":
            clear(conversation)
            out.text(
                phone,
                f"🔒 Too many wrong codes for {order.code}. The office has been told and will call you. Keep the parcel.",
            )
            effects.notify_admins(
                session,
                f"🔒 {order.code}: 3 wrong delivery codes from rider {rider.name}. Call the customer "
                f"({local_phone(order.customer_phone)}), then /unlock {order.code}",
            )
        else:
            attempts = orders.drop_code_attempts(session, order)
            out.text(
                phone,
                f"❌ That code is wrong ({attempts} of {orders.MAX_DROP_CODE_ATTEMPTS} tries). Ask the customer again and type it.",
            )
        return
    except (InvalidTransition, TransitionConditionFailed) as exc:
        if getattr(exc, "reason", "") == "delivery_photo_missing":
            go(conversation, "r:delivery_photo", order_id=order.id)
            out.text(phone, "Send a photo of the parcel at the customer first.")
            return
        show_menu(session, conversation, rider, f"{order.code} can't be marked delivered now.")
        return
    clear(conversation)
    show_menu(session, conversation, rider, f"🎉 {order.code} delivered. Well done!")
    effects.run(session, side_effects, actor_phone=phone)


def _fail(session: Session, event: InboundEvent, conversation: Conversation, rider: Rider, choice: str) -> None:
    phone, out = conversation.phone, box(session)
    if choice.startswith("fl:"):
        order = _my_order(session, rider, choice[3:])
        if order is None:
            show_menu(session, conversation, rider)
            return
        rows = [{"id": f"fr:{order.id}:{key}", "title": title} for key, title in FAIL_REASONS]
        out.list(phone, f"Why couldn't you deliver {order.code}?", "Choose reason", [{"title": "Reason", "rows": rows}])
        return
    _, raw_id, reason = (choice.split(":") + ["", ""])[:3]
    order = _my_order(session, rider, raw_id)
    if order is None:
        show_menu(session, conversation, rider)
        return
    try:
        side_effects = orders.transition(
            session, order, S.FAILED, "rider", rider.id, {"reason": reason}, at=event.received_at
        )
    except (InvalidTransition, TransitionConditionFailed):
        show_menu(session, conversation, rider, f"{order.code} can't be marked failed now.")
        return
    show_menu(
        session, conversation, rider, f"Noted: {order.code} not delivered. Please bring the parcel back to the office."
    )
    effects.run(session, side_effects, actor_phone=phone)


def handle_rider(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    rider = _rider(session, conversation.phone)
    phone, choice = conversation.phone, choice_of(event)

    if not rider.active:
        box(session).text(phone, "Your rider account is not active. Please talk to the office.")
        return
    if choice in ("r:on", "r:off"):
        rider.on_shift = choice == "r:on"
        if rider.on_shift:
            show_menu(session, conversation, rider, "🟢 You're on shift. Job offers will come here.")
            _available(session, conversation, rider)
        else:
            show_menu(session, conversation, rider, "⚪ You're off shift. No new job offers until you go back on.")
        return
    if choice == "r:jobs":
        _my_jobs(session, conversation, rider)
        return
    if choice == "r:open":
        _available(session, conversation, rider)
        return
    if choice.startswith("acc:"):
        _accept(session, event, conversation, rider, choice[4:])
        return
    if choice.startswith(("pu:", "dl:")):
        order = _my_order(session, rider, choice[3:])
        if order is None:
            show_menu(session, conversation, rider)
            return
        if choice.startswith("pu:"):
            go(conversation, "r:pickup_photo", order_id=order.id)
            box(session).text(phone, f"📷 Send a photo of the parcel for {order.code} as you collect it.")
        else:
            go(conversation, "r:delivery_photo", order_id=order.id)
            box(session).text(
                phone, f"📷 Send a photo of the parcel at {order.customer_name}'s, before you hand it over."
            )
        return
    if choice.startswith(("fl:", "fr:")):
        _fail(session, event, conversation, rider, choice)
        return

    step = step_of(conversation, "r:")
    if step and not is_reset(event):
        if step in ("r:pickup_photo", "r:delivery_photo"):
            _photo_step(session, event, conversation, rider, step)
            return
        if step == "r:code":
            _code_step(session, event, conversation, rider)
            return
    show_menu(session, conversation, rider)
