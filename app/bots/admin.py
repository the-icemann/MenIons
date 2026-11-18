"""Admin commands over WhatsApp. Only phones in ADMIN_PHONES can use them, and only messages
starting with "/" are commands, so an admin can also be a merchant or rider on the same phone.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import orders, plans, receipts
from app.bots import effects
from app.bots.common import STATUS_LABELS, box, ctx, go, local_phone, parse_ugx, ugx, when
from app.clock import kampala_date
from app.config import get_settings
from app.events import InboundEvent
from app.models import (
    AdminAlert,
    Conversation,
    Merchant,
    MerchantStatus,
    Order,
    OrderStatus,
    PlanRequest,
    PlanRequestStatus,
    Rider,
    Role,
)
from app.orders import InvalidTransition, TransitionConditionFailed
from app.phones import InvalidPhone, normalize_phone
from app.plans import PlanError
from app.receipts import ReceiptError

S = OrderStatus

HELP = """*Admin commands*
/pending: payments waiting for approval
/approve REF FDN CODE: approve a plan payment (EFRIS FDN and verification code)
/reject REF
/orders: open orders
/assign MNI-0001 0772123456: give a job to a rider
/reschedule MNI-0001: put a failed delivery back in the queue
/cancel MNI-0001
/unlock MNI-0001: reset wrong delivery codes
/riders
/rider add 0772123456 Name
/rider remove 0772123456
/cash 0772123456 85000: record cash a rider handed in today
/merchants
/suspend 0772123456  /activate 0772123456
/alerts: latest alerts"""

TEST_HELP = "\n\n*Testing (not in production)*\n/as merchant | rider | customer | unknown | auto: which bot answers your normal messages"


class CommandError(Exception):
    pass


def is_command(text: str) -> bool:
    return text.startswith("/")


def _phone(raw: str) -> str:
    try:
        return normalize_phone(raw)
    except InvalidPhone:
        raise CommandError(f"{raw} is not a Ugandan mobile number") from None


def _order(session: Session, code: str) -> Order:
    order = session.execute(select(Order).where(Order.code == code.upper())).scalar_one_or_none()
    if order is None:
        raise CommandError(f"no order {code}")
    return order


def _request(session: Session, ref: str) -> PlanRequest:
    request = session.execute(select(PlanRequest).where(PlanRequest.momo_reference == ref.upper())).scalar_one_or_none()
    if request is None:
        raise CommandError(f"no payment request {ref}")
    return request


def _rider_by_phone(session: Session, raw: str) -> Rider:
    rider = session.execute(select(Rider).where(Rider.phone == _phone(raw))).scalar_one_or_none()
    if rider is None:
        raise CommandError(f"no rider with phone {raw}")
    return rider


def _merchant_by_phone(session: Session, raw: str) -> Merchant:
    merchant = session.execute(select(Merchant).where(Merchant.phone == _phone(raw))).scalar_one_or_none()
    if merchant is None:
        raise CommandError(f"no merchant with phone {raw}")
    return merchant


def _need(args: list[str], n: int, usage: str) -> None:
    if len(args) < n:
        raise CommandError(f"usage: {usage}")


# --- Commands --------------------------------------------------------------------------


def cmd_pending(session, admin, args, event):
    rows = (
        session.execute(
            select(PlanRequest).where(PlanRequest.status == PlanRequestStatus.PAYMENT_CLAIMED).order_by(PlanRequest.id)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "No payments waiting."
    lines = []
    for r in rows:
        m = session.get(Merchant, r.merchant_id)
        lines.append(
            f"{r.momo_reference} · {m.shop_name} · {plans.get_plan(r.plan).name} · {ugx(r.amount_ugx)} · {local_phone(m.phone)}"
        )
    return "Payments claimed:\n" + "\n".join(lines) + "\n\n/approve REF FDN CODE"


def cmd_approve(session, admin, args, event):
    _need(args, 3, "/approve REF FDN VERIFICATION-CODE")
    request = _request(session, args[0])
    try:
        receipt = plans.approve_plan_request(
            session, request, admin, event.received_at, efris_fdn=args[1], efris_verification_code=args[2]
        )
    except PlanError as exc:
        raise CommandError(str(exc)) from None
    if receipt is None:
        return f"{request.momo_reference} was already approved."
    merchant = session.get(Merchant, request.merchant_id)
    plan = plans.get_plan(request.plan)
    box(session).text(
        merchant.phone,
        f"🎉 Payment received! Your *{plan.name}* plan is active: {plan.drops} drops until {when(merchant.plan_ends)}.\n\n"
        "Send *menu* to book your first delivery.",
    )
    effects.send_receipts(session, [receipt], plan.name)
    return (
        f"✅ Approved {request.momo_reference}: {merchant.shop_name} now on {plan.name}. Receipt {receipt.number} sent."
    )


def cmd_reject(session, admin, args, event):
    _need(args, 1, "/reject REF")
    request = _request(session, args[0])
    try:
        plans.reject_plan_request(session, request, admin, event.received_at)
    except PlanError as exc:
        raise CommandError(str(exc)) from None
    merchant = session.get(Merchant, request.merchant_id)
    box(session).text(
        merchant.phone,
        f"We couldn't find your payment for reference {request.momo_reference}. "
        "Please check and try again, or call us. Send *menu* to choose a plan.",
    )
    return f"Rejected {request.momo_reference}; {merchant.shop_name} was told."


def cmd_orders(session, admin, args, event):
    rows = (
        session.execute(
            select(Order).where(Order.status.not_in((S.DELIVERED, S.CANCELLED))).order_by(Order.id).limit(30)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "No open orders."
    lines = []
    for o in rows:
        m = session.get(Merchant, o.merchant_id)
        rider = session.get(Rider, o.rider_id).name if o.rider_id else "-"
        lines.append(f"{o.code} · {m.shop_name} → {o.customer_name} · {STATUS_LABELS[S(o.status)]} · rider {rider}")
    return "\n".join(lines)


def _transition(session, order, to, admin, data, event, done: str):
    try:
        side_effects = orders.transition(session, order, to, "admin", admin, data, at=event.received_at)
    except (InvalidTransition, TransitionConditionFailed) as exc:
        raise CommandError(f"{order.code}: {exc}") from None
    effects.run(session, side_effects, actor_phone=admin)
    return done


def cmd_assign(session, admin, args, event):
    _need(args, 2, "/assign MNI-0001 RIDER-PHONE")
    order, rider = _order(session, args[0]), _rider_by_phone(session, args[1])
    return _transition(
        session, order, S.ASSIGNED, admin, {"rider_id": rider.id}, event, f"{order.code} assigned to {rider.name}."
    )


def cmd_reschedule(session, admin, args, event):
    _need(args, 1, "/reschedule MNI-0001")
    order = _order(session, args[0])
    return _transition(
        session, order, S.READY, admin, {}, event, f"{order.code} is back in the queue; riders were offered it."
    )


def cmd_cancel(session, admin, args, event):
    _need(args, 1, "/cancel MNI-0001")
    order = _order(session, args[0])
    return _transition(session, order, S.CANCELLED, admin, {}, event, f"{order.code} cancelled.")


def cmd_unlock(session, admin, args, event):
    _need(args, 1, "/unlock MNI-0001")
    order = _order(session, args[0])
    orders.reset_drop_code_attempts(session, order, admin, event.received_at)
    if order.rider_id:
        rider = session.get(Rider, order.rider_id)
        box(session).buttons(rider.phone, f"🔓 {order.code} is unlocked. Ask the customer for the code again.",
                             [(f"dl:{order.id}", "Delivered"), (f"fl:{order.id}", "Couldn't deliver")])  # fmt: skip
    return f"{order.code} unlocked."


def cmd_riders(session, admin, args, event):
    rows = session.execute(select(Rider).order_by(Rider.id)).scalars().all()
    if not rows:
        return "No riders yet. Add one: /rider add 0772123456 Name"
    return "\n".join(
        f"{r.name} · {local_phone(r.phone)} · {'on shift' if r.on_shift else 'off shift'}{'' if r.active else ' · REMOVED'}"
        for r in rows
    )


def cmd_rider(session, admin, args, event):
    _need(args, 2, "/rider add PHONE NAME  or  /rider remove PHONE")
    action, phone = args[0].lower(), _phone(args[1])
    rider = session.execute(select(Rider).where(Rider.phone == phone)).scalar_one_or_none()
    if action == "add":
        _need(args, 3, "/rider add PHONE NAME")
        name = " ".join(args[2:])
        if rider is None:
            rider = Rider(phone=phone, name=name, on_shift=False, active=True)
            session.add(rider)
        else:
            rider.name, rider.active = name, True
        session.flush()
        box(session).buttons(
            phone,
            f"Welcome to Men-Ions, {name}! 🛵 You're registered as a rider. Go on shift to get job offers.",
            [("r:on", "Go on shift")],
            optional=True,
        )
        return f"Rider {name} ({local_phone(phone)}) added. They get a welcome if they've messaged us in the last 24 hours; otherwise ask them to send *hi*."
    if action == "remove":
        if rider is None:
            raise CommandError(f"no rider with phone {args[1]}")
        rider.active, rider.on_shift = False, False
        return f"Rider {rider.name} removed."
    raise CommandError("usage: /rider add PHONE NAME  or  /rider remove PHONE")


def cmd_cash(session, admin, args, event):
    _need(args, 2, "/cash RIDER-PHONE AMOUNT [YYYY-MM-DD]")
    rider = _rider_by_phone(session, args[0])
    amount = parse_ugx(args[1])
    if amount is None:
        raise CommandError(f"{args[1]} is not an amount")
    try:
        day = date.fromisoformat(args[2]) if len(args) > 2 else kampala_date(event.received_at)
    except ValueError:
        raise CommandError("date must look like 2026-11-18") from None
    try:
        handover = receipts.record_cash_handover(session, rider, day, amount, admin, at=event.received_at)
    except ReceiptError as exc:
        raise CommandError(str(exc)) from None
    box(session).buttons(
        rider.phone,
        f"💵 The office recorded *{ugx(amount)}* received from you for {day:%d %b}"
        f" (your deliveries: {ugx(handover.expected_ugx)}). Tap *Confirm* if that's right.",
        [(f"cash:{handover.id}", "Confirm")],
    )
    diff = handover.difference_ugx
    note = "" if diff == 0 else f" ⚠️ Difference: {ugx(diff) if diff > 0 else '-' + ugx(-diff)}."
    return f"Recorded {ugx(amount)} from {rider.name} (expected {ugx(handover.expected_ugx)}).{note} Waiting for the rider to confirm."


def cmd_merchants(session, admin, args, event):
    rows = session.execute(select(Merchant).order_by(Merchant.id).limit(40)).scalars().all()
    if not rows:
        return "No merchants yet."
    return "\n".join(
        f"{m.shop_name} · {local_phone(m.phone)} · {m.status} · {plans.CATALOGUE[m.plan].name if m.plan else 'no plan'}"
        f" · {m.drops_left if m.drops_left is not None else 0} drops"
        for m in rows
    )


def _set_status(session, args, status, usage):
    _need(args, 1, usage)
    merchant = _merchant_by_phone(session, args[0])
    merchant.status = status
    return f"{merchant.shop_name} is now {status}."


def cmd_suspend(session, admin, args, event):
    return _set_status(session, args, MerchantStatus.SUSPENDED, "/suspend PHONE")


def cmd_activate(session, admin, args, event):
    return _set_status(session, args, MerchantStatus.ACTIVE, "/activate PHONE")


def cmd_alerts(session, admin, args, event):
    rows = session.execute(select(AdminAlert).order_by(AdminAlert.id.desc()).limit(10)).scalars().all()
    if not rows:
        return "No alerts."
    out = []
    for a in rows:
        detail = ", ".join(f"{k}={v}" for k, v in (a.data or {}).items() if k != "text")
        out.append(f"#{a.id} {when(a.at)} · {a.kind} · {detail}"[:300])
    return "\n".join(out)


def cmd_help(session, admin, args, event):
    return HELP + ("" if get_settings().env == "production" else TEST_HELP)


COMMANDS: dict[str, Callable] = {
    "help": cmd_help,
    "pending": cmd_pending,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "orders": cmd_orders,
    "assign": cmd_assign,
    "reschedule": cmd_reschedule,
    "cancel": cmd_cancel,
    "unlock": cmd_unlock,
    "riders": cmd_riders,
    "rider": cmd_rider,
    "cash": cmd_cash,
    "merchants": cmd_merchants,
    "suspend": cmd_suspend,
    "activate": cmd_activate,
    "alerts": cmd_alerts,
}

AS_ROLES = {"merchant": Role.MERCHANT, "rider": Role.RIDER, "customer": Role.CUSTOMER, "unknown": Role.UNKNOWN}


def handle(session: Session, event: InboundEvent, conversation: Conversation, text: str) -> None:
    admin = conversation.phone
    try:
        parts = shlex.split(text[1:])
    except ValueError:
        parts = text[1:].split()
    name, args = (parts[0].lower(), parts[1:]) if parts else ("help", [])

    if name == "as":
        if get_settings().env == "production":
            reply = "/as is only for testing."
        elif not args or args[0].lower() not in (*AS_ROLES, "auto"):
            reply = "usage: /as merchant | rider | customer | unknown | auto"
        else:
            role = args[0].lower()
            values = {k: v for k, v in ctx(conversation).items() if k != "as_role"}
            conversation.context = values if role == "auto" else {**values, "as_role": role}
            go(conversation, None)
            reply = (
                f"Your normal messages now go to the {role} bot."
                if role != "auto"
                else "Back to normal: your role decides."
            )
        box(session).text(admin, reply)
        return

    command = COMMANDS.get(name)
    if command is None:
        box(session).text(admin, f"Unknown command /{name}. Send /help")
        return
    try:
        with session.begin_nested():
            reply = command(session, admin, args, event)
    except CommandError as exc:
        reply = f"⚠️ {exc}"
    box(session).text(admin, reply)
