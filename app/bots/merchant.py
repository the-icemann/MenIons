"""Merchants: self-onboarding (unknown senders), plan purchase, booking, orders, cancelling."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import orders, plans
from app.bots import effects
from app.bots.common import (
    STATUS_LABELS,
    box,
    chat_link,
    choice_of,
    clear,
    ctx,
    go,
    is_reset,
    local_phone,
    short,
    step_of,
    text_of,
    ugx,
    when,
)
from app.config import get_settings
from app.events import ContactShared, InboundEvent, LocationShared
from app.models import (
    EXPECTED_DAILY_DROPS,
    Conversation,
    Merchant,
    MerchantStatus,
    Order,
    OrderStatus,
    PlanRequest,
    PlanRequestStatus,
)
from app.orders import InvalidTransition, TransitionConditionFailed
from app.phones import InvalidPhone, normalize_phone

S = OrderStatus
CANCELLABLE = (S.BOOKED, S.READY, S.ASSIGNED)

REFUSALS = {
    "no_plan": "You don't have a plan yet.",
    "plan_expired": "Your plan has ended.",
    "no_drops_left": "You've used all the drops on your plan.",
    "merchant_not_active": "Your account isn't active. Please contact us.",
}


def _merchant(session: Session, phone: str) -> Merchant | None:
    return session.execute(select(Merchant).where(Merchant.phone == phone)).scalar_one_or_none()


# --- Onboarding (unknown senders) ------------------------------------------------------


def handle_unknown(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    phone = conversation.phone
    out = box(session)

    existing = _merchant(session, phone)
    if existing is not None and existing.status == MerchantStatus.SUSPENDED:
        contact = get_settings().company_phone
        out.text(phone, "Your Men-Ions account is suspended." + (f" Please call us on {contact}." if contact else ""))
        return

    choice, text = choice_of(event), text_of(event)
    step = step_of(conversation, "onb:")

    if choice == "onb:no":
        clear(conversation)
        out.text(phone, "No problem. Send *hi* any time you'd like to register your shop.")
        return
    if choice in ("onb:start", "onb:restart"):
        clear(conversation)
        go(conversation, "onb:shop")
        out.text(phone, "Great! Let's set up your shop. It takes about a minute.\n\nWhat is your *shop's name*?")
        return
    if not step or (is_reset(event) and step != "onb:shop"):
        go(conversation, "onb:welcome")
        out.buttons(
            phone,
            "Hi 👋 Men-Ions delivers parcels for shops in Kampala by boda, booked right here on WhatsApp.\n\n"
            "Would you like to register your shop?",
            [("onb:start", "Register my shop"), ("onb:no", "Not now")],
        )
        return

    if step == "onb:welcome":
        out.buttons(
            phone, "Tap *Register my shop* to begin.", [("onb:start", "Register my shop"), ("onb:no", "Not now")]
        )
    elif step == "onb:shop":
        if not 2 <= len(text) <= 80:
            out.text(phone, "Please type your shop's name.")
            return
        go(conversation, "onb:owner", shop_name=text)
        out.text(phone, "And your *full name*, as the owner?")
    elif step == "onb:owner":
        if not 2 <= len(text) <= 80:
            out.text(phone, "Please type your full name.")
            return
        go(conversation, "onb:sells", owner_name=text)
        out.text(phone, "What do you *sell*? (for example: clothes, phones, cakes)")
    elif step == "onb:sells":
        if not 2 <= len(text) <= 120:
            out.text(phone, "Tell us briefly what you sell.")
            return
        go(conversation, "onb:volume", sells=text)
        out.buttons(
            phone,
            "About how many deliveries do you send *a day*?",
            [(f"vol:{v}", f"{v} a day") for v in EXPECTED_DAILY_DROPS],
        )
    elif step == "onb:volume":
        volume = choice.removeprefix("vol:") if choice.startswith("vol:") else ""
        if volume not in EXPECTED_DAILY_DROPS:
            out.buttons(phone, "Please pick one:", [(f"vol:{v}", f"{v} a day") for v in EXPECTED_DAILY_DROPS])
            return
        go(conversation, "onb:pin", expected_daily_drops=volume)
        out.location_request(
            phone,
            "Where should riders pick up parcels? Tap *Send location* while you're at the shop " "(or 📎 → Location).",
        )
    elif step == "onb:pin":
        if not isinstance(event, LocationShared):
            out.location_request(phone, "Please share your shop's location: tap *Send location*, or 📎 → Location.")
            return
        go(conversation, "onb:landmark", lat=event.lat, lng=event.lng)
        out.text(
            phone,
            "📍 Got it. Now describe how a rider finds you: landmark, building, floor.\n"
            "For example: _Opposite Shell Kisementi, 1st floor, shop 12_",
        )
    elif step == "onb:landmark":
        if not 3 <= len(text) <= 200:
            out.text(phone, "Please type a short description of where your shop is.")
            return
        go(conversation, "onb:confirm", landmark=text)
        c = ctx(conversation)
        out.buttons(
            phone,
            "Please check:\n"
            f"Shop: {c['shop_name']}\nOwner: {c['owner_name']}\nSells: {c['sells']}\n"
            f"Deliveries a day: {c['expected_daily_drops']}\nPickup: {c['landmark']}\nPhone: {local_phone(phone)}",
            [("onb:confirm", "Confirm"), ("onb:restart", "Start again")],
        )
    elif step == "onb:confirm":
        if choice != "onb:confirm":
            out.buttons(phone, "Tap *Confirm* to register, or *Start again*.",
                        [("onb:confirm", "Confirm"), ("onb:restart", "Start again")])  # fmt: skip
            return
        c = ctx(conversation)
        merchant = existing or Merchant(phone=phone)
        merchant.shop_name = c["shop_name"]
        merchant.owner_name = c["owner_name"]
        merchant.sells = c["sells"]
        merchant.expected_daily_drops = c["expected_daily_drops"]
        merchant.pickup_lat, merchant.pickup_lng = c["lat"], c["lng"]
        merchant.pickup_text = c["landmark"]
        merchant.status = MerchantStatus.PENDING
        session.add(merchant)
        session.flush()
        clear(conversation)
        out.text(phone, f"✅ {merchant.shop_name} is registered. Last step: choose a delivery plan.")
        show_plans(session, conversation, merchant)
        effects.notify_admins(
            session, f"🆕 New shop registered: {merchant.shop_name} ({merchant.sells}), {local_phone(phone)}."
        )


# --- Plans -----------------------------------------------------------------------------


def show_plans(session: Session, conversation: Conversation, merchant: Merchant) -> None:
    recommended = plans.recommend_plan(merchant.expected_daily_drops) if merchant.expected_daily_drops else None
    period = {"day": "today only", "week": "7 days", "month": "1 month"}
    sections = []
    for title, keys in (
        ("Daily", ["daily_bronze", "daily_silver", "daily_gold"]),
        ("Weekly", ["weekly_bronze", "weekly_silver", "weekly_gold"]),
        ("Monthly", ["monthly"]),
    ):
        rows = []
        for key in keys:
            plan = plans.CATALOGUE[key]
            star = " ⭐" if key == recommended else ""
            rows.append({
                "id": f"plan:{key}",
                "title": f"{plan.name}{star}",
                "description": f"{ugx(plan.price_ugx)} · {plan.drops} drops · {period[plan.period]}",
            })  # fmt: skip
        sections.append({"title": title, "rows": rows})
    body = "Choose a plan. Each delivery uses one drop."
    if recommended:
        body += f"\n\nFor {merchant.expected_daily_drops} deliveries a day we recommend *{plans.CATALOGUE[recommended].name}* ⭐"
    go(conversation, "plan:choose")
    box(session).list(conversation.phone, body, "See plans", sections)


def _payment_instructions(request: PlanRequest) -> str:
    settings = get_settings()
    plan = plans.get_plan(request.plan)
    to = settings.momo_pay_number or "our Mobile Money number"
    name = f" ({settings.momo_pay_name})" if settings.momo_pay_name else ""
    return (
        f"*{plan.name}*: {ugx(plan.price_ugx)} for {plan.drops} drops.\n\n"
        f"1. Send {ugx(request.amount_ugx)} by Mobile Money to *{to}*{name}.\n"
        f"2. Use this as the reason/reference: *{request.momo_reference}*\n"
        "3. Tap *I've paid* below.\n\nYour plan starts as soon as we confirm the payment."
    )


def handle_plan_step(session: Session, event: InboundEvent, conversation: Conversation, merchant: Merchant) -> bool:
    """Plan choice and payment, for pending and active merchants. True if it handled the event."""
    phone = conversation.phone
    choice = choice_of(event)
    out = box(session)

    if choice.startswith("plan:"):
        key = choice.removeprefix("plan:")
        if key == "change":
            show_plans(session, conversation, merchant)
            return True
        if key not in plans.CATALOGUE:
            return False
        request = plans.create_plan_request(session, merchant, key, at=event.received_at)
        go(conversation, "plan:pay", request_id=request.id)
        out.buttons(
            phone, _payment_instructions(request), [(f"paid:{request.id}", "I've paid"), ("plan:change", "Change plan")]
        )
        return True

    if choice.startswith("paid:"):
        request = session.get(PlanRequest, int(choice.removeprefix("paid:")) if choice[5:].isdigit() else 0)
        if request is None or request.merchant_id != merchant.id:
            return False
        if request.status in (PlanRequestStatus.APPROVED, PlanRequestStatus.REJECTED):
            out.text(phone, f"That payment request is already {request.status.lower()}.")
            return True
        plans.claim_payment(session, request, at=event.received_at)
        clear(conversation)
        plan = plans.get_plan(request.plan)
        out.text(phone, "🙏 Thank you! We're checking the payment and will message you as soon as your plan starts.")
        effects.notify_admins(
            session,
            f"💰 Payment claimed: {merchant.shop_name}, {plan.name}, {ugx(request.amount_ugx)}, "
            f"ref {request.momo_reference}, from {local_phone(merchant.phone)}.\n"
            f"Check MoMo, then: /approve {request.momo_reference} <EFRIS FDN> <verification code>\n"
            f"or: /reject {request.momo_reference}",
        )
        return True

    if step_of(conversation, "plan:") == "plan:pay" and not is_reset(event):
        request = session.get(PlanRequest, ctx(conversation).get("request_id") or 0)
        if request is not None and request.status == PlanRequestStatus.AWAITING_PAYMENT:
            out.buttons(
                phone,
                _payment_instructions(request),
                [(f"paid:{request.id}", "I've paid"), ("plan:change", "Change plan")],
            )
            return True
    if step_of(conversation, "plan:") == "plan:choose" and not is_reset(event):
        show_plans(session, conversation, merchant)
        return True
    return False


def handle_merchant_pending(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    merchant = _merchant(session, conversation.phone)
    if handle_plan_step(session, event, conversation, merchant):
        return
    claimed = (
        session.execute(
            select(PlanRequest)
            .where(PlanRequest.merchant_id == merchant.id, PlanRequest.status == PlanRequestStatus.PAYMENT_CLAIMED)
            .order_by(PlanRequest.id.desc())
        )
        .scalars()
        .first()
    )
    if claimed:
        box(session).buttons(
            conversation.phone,
            f"⏳ We're checking your payment for {plans.get_plan(claimed.plan).name} (ref {claimed.momo_reference}). "
            "We'll message you as soon as your plan starts.",
            [("plan:change", "Choose another plan")],
        )
        return
    show_plans(session, conversation, merchant)


# --- Active merchants ------------------------------------------------------------------


def show_menu(
    session: Session, conversation: Conversation, merchant: Merchant, intro: str = "", now: datetime | None = None
) -> None:
    clear(conversation)
    has_drop = plans.has_drop(merchant, now or _now(conversation))
    drops = f"{merchant.drops_left} drops left" if has_drop else "no drops available"
    rows = [
        {"id": "m:book", "title": "Book a delivery", "description": "Send a parcel to a customer"},
        {"id": "m:orders", "title": "My orders", "description": "Where are my parcels?"},
        {"id": "m:cancel", "title": "Cancel an order", "description": "Before the rider picks it up"},
        {"id": "m:plan", "title": "My plan", "description": drops},
        {"id": "m:buy", "title": "Buy or renew plan", "description": "Top up your drops"},
        {"id": "m:help", "title": "Help", "description": "How Men-Ions works"},
    ]
    body = (intro + "\n\n" if intro else "") + f"*{merchant.shop_name}*: what would you like to do?"
    box(session).list(conversation.phone, body, "Menu", [{"title": "Men-Ions", "rows": rows}])


def _now(conversation: Conversation) -> datetime:
    return conversation.last_inbound_at


def _start_booking(session: Session, conversation: Conversation, merchant: Merchant) -> None:
    refusal = plans.drop_refusal(merchant, _now(conversation))
    if merchant.status != MerchantStatus.ACTIVE or not merchant.active:
        refusal = "merchant_not_active"
    if refusal:
        buttons = [("m:buy", "Buy a plan")] if refusal != "merchant_not_active" else [("m:menu", "Menu")]
        box(session).buttons(conversation.phone, REFUSALS[refusal] + " Buy a plan to keep booking.", buttons)
        return
    go(conversation, "book:name")
    how = (
        "📦 *New delivery.* Who is it for?\n\n"
        "• Share their contact: 📎 → *Contact*\n"
        "• Or type the *customer's name*"
    )
    recent = _recent_customers(session, merchant)
    if recent:
        rows = [
            {"id": f"cust:{o.id}", "title": short(o.customer_name, 24), "description": local_phone(o.customer_phone)}
            for o in recent
        ]
        box(session).list(
            conversation.phone, how + "\n• Or pick a recent customer below", "Recent customers",
            [{"title": "Recent customers", "rows": rows}],
        )  # fmt: skip
    else:
        box(session).text(conversation.phone, how + "\n\n(Send *menu* any time to stop.)")


def _recent_customers(session: Session, merchant: Merchant, limit: int = 9) -> list[Order]:
    """The newest order for each of the shop's latest distinct customers."""
    seen: dict[str, Order] = {}
    for order in session.execute(
        select(Order).where(Order.merchant_id == merchant.id).order_by(Order.id.desc()).limit(100)
    ).scalars():
        if order.customer_phone not in seen:
            seen[order.customer_phone] = order
            if len(seen) == limit:
                break
    return list(seen.values())


def _from_contact(event: InboundEvent) -> tuple[str, str | None] | None:
    """(name, phone) from a shared contact card; phone is None if no number is a Ugandan mobile."""
    if not isinstance(event, ContactShared) or not event.contacts:
        return None
    name, numbers = event.contacts[0]
    for number in numbers:
        try:
            return name, normalize_phone(number)
        except InvalidPhone:
            continue
    return name, None


def _ask_package(session: Session, conversation: Conversation, name: str, customer_phone: str) -> None:
    go(conversation, "book:package", name=name, phone=customer_phone)
    box(session).text(
        conversation.phone,
        f"👤 {name}, {local_phone(customer_phone)}\n\nWhat are you sending? (for example: _2 dresses in a black bag_)",
    )


def _booking_summary(c: dict, merchant: Merchant) -> str:
    return (
        "Please check this booking:\n"
        f"Customer: {c['name']}, {local_phone(c['phone'])}\n"
        f"Package: {c['package']}\n"
        f"Notes: {c.get('notes') or 'none'}\n\n"
        f"This uses 1 of your {merchant.drops_left} drops."
    )


def _booking_step(
    session: Session, event: InboundEvent, conversation: Conversation, merchant: Merchant, step: str
) -> None:
    phone, text, choice = conversation.phone, text_of(event), choice_of(event)
    out = box(session)

    if step == "book:name":
        contact = _from_contact(event)
        if choice.startswith("cust:"):
            past = session.get(Order, int(choice[5:])) if choice[5:].isdigit() else None
            if past is not None and past.merchant_id == merchant.id:
                _ask_package(session, conversation, past.customer_name, past.customer_phone)
                return
        if contact:
            name, customer_phone = contact
            name = name or "Customer"
            if customer_phone:
                _ask_package(session, conversation, name, customer_phone)
            else:
                go(conversation, "book:phone", name=name)
                out.text(
                    phone,
                    f"That contact has no Ugandan mobile number. Type {name}'s *phone number* (e.g. 0772 123456).",
                )
            return
        if not 2 <= len(text) <= 80:
            out.text(phone, "Please type the customer's name, or share their contact (📎 → Contact).")
            return
        go(conversation, "book:phone", name=text)
        out.text(phone, f"{text}'s *phone number*? (for example 0772 123456), or share their contact.")
    elif step == "book:phone":
        contact = _from_contact(event)
        try:
            if contact and contact[1]:
                customer_phone = contact[1]
            else:
                customer_phone = normalize_phone(text)
        except InvalidPhone:
            out.text(phone, "That doesn't look like a Ugandan mobile number. Try again, like 0772 123456.")
            return
        _ask_package(session, conversation, ctx(conversation).get("name") or "Customer", customer_phone)
    elif step == "book:package":
        if not 2 <= len(text) <= 200:
            out.text(phone, "Please describe the package briefly.")
            return
        go(conversation, "book:notes", package=text)
        out.buttons(
            phone, "Any *notes for the rider*? (e.g. fragile, call before coming)", [("notes:none", "No notes")]
        )
    elif step == "book:notes":
        notes = None if choice == "notes:none" else text[:300]
        if choice != "notes:none" and not text:
            out.buttons(phone, "Type a note, or tap *No notes*.", [("notes:none", "No notes")])
            return
        go(conversation, "book:confirm", notes=notes)
        out.buttons(phone, _booking_summary(ctx(conversation), merchant),
                    [("book:yes", "Confirm"), ("book:redo", "Start again"), ("m:menu", "Cancel")])  # fmt: skip
    elif step == "book:confirm":
        if choice == "book:redo":
            _start_booking(session, conversation, merchant)
            return
        if choice != "book:yes":
            out.buttons(
                phone,
                "Tap *Confirm* to book.",
                [("book:yes", "Confirm"), ("book:redo", "Start again"), ("m:menu", "Cancel")],
            )
            return
        c = ctx(conversation)
        try:
            order, side_effects = orders.create_order(
                session, merchant, customer_name=c["name"], customer_phone=c["phone"], package=c["package"],
                notes=c.get("notes"), at=event.received_at,
            )  # fmt: skip
        except TransitionConditionFailed as exc:
            clear(conversation)
            out.buttons(
                phone,
                REFUSALS.get(exc.reason, "Sorry, we couldn't book that."),
                [("m:buy", "Buy a plan"), ("m:menu", "Menu")],
            )
            return
        clear(conversation)
        out.text(phone, f"✅ Booked *{order.code}* for {order.customer_name}. {merchant.drops_left} drops left.")
        out.text(phone, effects.booking_link_message(order))
        effects.run(session, side_effects, actor_phone=phone)


def _list_orders(session: Session, conversation: Conversation, merchant: Merchant) -> None:
    rows = (
        session.execute(select(Order).where(Order.merchant_id == merchant.id).order_by(Order.id.desc()).limit(10))
        .scalars()
        .all()
    )
    if not rows:
        show_menu(session, conversation, merchant, "You haven't booked any deliveries yet.")
        return
    lines = [f"*{o.code}* · {o.customer_name} · {STATUS_LABELS[S(o.status)]}" for o in rows]
    waiting = [o for o in rows if o.status == S.BOOKED]
    text = "Your latest orders:\n" + "\n".join(lines)
    if waiting:
        link = chat_link(waiting[0].code)
        if link:
            text += f"\n\nCustomer for {waiting[0].code} hasn't shared a location yet. Their link:\n{link}"
    box(session).buttons(conversation.phone, short(text, 1000), [("m:book", "Book a delivery"), ("m:menu", "Menu")])


def _cancel_list(session: Session, conversation: Conversation, merchant: Merchant) -> None:
    rows = session.execute(
        select(Order).where(Order.merchant_id == merchant.id, Order.status.in_(CANCELLABLE))
        .order_by(Order.id.desc()).limit(10)
    ).scalars().all()  # fmt: skip
    if not rows:
        show_menu(session, conversation, merchant, "There's nothing you can cancel right now.")
        return
    items = [
        {"id": f"cx:{o.id}", "title": o.code, "description": short(f"{o.customer_name} · {o.package}", 72)}
        for o in rows
    ]
    box(session).list(
        conversation.phone, "Which order do you want to cancel?", "Choose order", [{"title": "Orders", "rows": items}]
    )


def _cancel(session: Session, event: InboundEvent, conversation: Conversation, merchant: Merchant, choice: str) -> None:
    phone, out = conversation.phone, box(session)
    order_id = choice.split(":", 1)[1]
    order = session.get(Order, int(order_id)) if order_id.isdigit() else None
    if order is None or order.merchant_id != merchant.id:
        show_menu(session, conversation, merchant)
        return
    if choice.startswith("cx:"):
        out.buttons(phone, f"Cancel *{order.code}* for {order.customer_name}?",
                    [(f"cxy:{order.id}", "Yes, cancel it"), ("m:menu", "Keep it")])  # fmt: skip
        return
    before = merchant.drops_left or 0
    try:
        side_effects = orders.transition(session, order, S.CANCELLED, "merchant", merchant.id, at=event.received_at)
    except (InvalidTransition, TransitionConditionFailed):
        label = STATUS_LABELS[S(order.status)].lower()
        show_menu(session, conversation, merchant, f"{order.code} can't be cancelled any more ({label}).")
        return
    # transition() re-reads the merchant row into this same object, so drops_left is current.
    if (merchant.drops_left or 0) > before:
        note = f"❌ {order.code} cancelled. The drop is back on your plan: {merchant.drops_left} left."
    else:
        note = f"❌ {order.code} cancelled."
    show_menu(session, conversation, merchant, note)
    effects.run(session, side_effects, actor_phone=phone)


def _plan_status(session: Session, conversation: Conversation, merchant: Merchant) -> None:
    now = _now(conversation)
    if merchant.plan is None:
        text = "You don't have a plan yet."
    else:
        plan = plans.get_plan(merchant.plan)
        if merchant.plan_ends and merchant.plan_ends > now:
            text = f"*{plan.name}*: {merchant.drops_left} of {plan.drops} drops left, until {when(merchant.plan_ends)}."
        else:
            text = f"Your *{plan.name}* plan ended on {when(merchant.plan_ends)}."
    box(session).buttons(conversation.phone, text, [("m:buy", "Buy or renew plan"), ("m:menu", "Menu")])


HELP = (
    "*How Men-Ions works*\n"
    "1. Book a delivery here (Menu → Book a delivery).\n"
    "2. Send your customer the link we give you. They share their location with us.\n"
    "3. A rider picks up from your shop and delivers. Your customer gives the rider a 4-digit code.\n"
    "4. You get a receipt for every step.\n\n"
    "We only deliver: payment for the goods is between you and your customer.\n\n"
    "Each delivery uses one drop from your plan. Cancelled before pickup? The drop comes back."
)


def handle_merchant(session: Session, event: InboundEvent, conversation: Conversation) -> None:
    merchant = _merchant(session, conversation.phone)
    choice = choice_of(event)

    if is_reset(event) or choice == "m:menu":
        show_menu(session, conversation, merchant)
        return
    if choice == "m:book":
        _start_booking(session, conversation, merchant)
        return
    if choice == "m:orders":
        _list_orders(session, conversation, merchant)
        return
    if choice == "m:cancel":
        _cancel_list(session, conversation, merchant)
        return
    if choice.startswith(("cx:", "cxy:")):
        _cancel(session, event, conversation, merchant, choice)
        return
    if choice == "m:plan":
        _plan_status(session, conversation, merchant)
        return
    if choice == "m:buy":
        show_plans(session, conversation, merchant)
        return
    if choice == "m:help":
        box(session).buttons(conversation.phone, HELP, [("m:book", "Book a delivery"), ("m:menu", "Menu")])
        return
    if handle_plan_step(session, event, conversation, merchant):
        return

    step = step_of(conversation, "book:")
    if step:
        _booking_step(session, event, conversation, merchant, step)
        return
    show_menu(session, conversation, merchant)
