"""Admin commands over WhatsApp. Only phones in ADMIN_PHONES can use them, and only messages
starting with "/" are commands, so an admin can also be a merchant or rider on the same phone.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import billing, orders, plans, privacy
from app.bots import effects
from app.bots import merchant as merchant_bot
from app.bots import rider as rider_bot
from app.bots.common import STATUS_LABELS, box, ctx, go, local_phone, ugx, when
from app.config import get_settings
from app.events import InboundEvent
from app.models import (
    AdminAlert,
    Conversation,
    FiscalDocument,
    FiscalKind,
    FiscalStatus,
    Merchant,
    MerchantStatus,
    Order,
    OrderStatus,
    PlanRequest,
    PlanRequestStatus,
    RefundStatus,
    Rider,
    Role,
)
from app.orders import InvalidTransition, TransitionConditionFailed
from app.phones import InvalidPhone, normalize_phone
from app.plans import PlanError

S = OrderStatus

HELP = """*Admin commands*
/pending: payments waiting for approval
/approve REF: approve a payment sent by hand (add FDN CODE if the EFRIS receipt is already issued)
/reject REF
/fiscal: sales still waiting for their EFRIS FDN
/fdn REF FDN CODE: record URA's numbers (REF-CN for a credit note)
/refund REF reason: send a plan payment back (ends the plan)
/refund REF extra reason: send back a second payment
/reconcile: check payments, plans, EFRIS and receipts agree
/orders: open orders
/assign MNI-0001 0772123456: give a job to a rider
/reschedule MNI-0001: put a failed delivery back in the queue
/cancel MNI-0001
/unlock MNI-0001: reset wrong delivery codes
/riders
/rider add 0772123456 PERMIT NIN PLATE Name
/rider remove 0772123456
/merchants
/suspend 0772123456  /activate 0772123456
/mydata 0772123456: what we hold about a number
/forget 0772123456: delete it (receipts and payments are kept)
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
    if not args:
        raise CommandError("usage: /approve REF  (or /approve REF FDN CODE). Send /pending to see references.")
    request = _request(session, args[0])
    if len(args) == 2:
        raise CommandError("give both the EFRIS FDN and the verification code, or neither")
    fdn, code = (args[1], args[2]) if len(args) >= 3 else (None, None)
    try:
        approval = plans.approve_plan_request(
            session, request, admin, event.received_at, efris_fdn=fdn, efris_verification_code=code
        )
    except PlanError as exc:
        raise CommandError(str(exc)) from None
    if approval is None:
        return f"{request.momo_reference} was already approved."
    merchant = session.get(Merchant, request.merchant_id)
    plan = plans.get_plan(request.plan)
    follows = "" if approval.receipt else "\n\nYour receipt with URA's EFRIS number follows shortly."
    if approval.receipt:
        effects.send_receipts(session, [approval.receipt], plan.name)
    # Straight into the menu, so the merchant can book right away.
    conversation = session.get(Conversation, merchant.phone) or Conversation(phone=merchant.phone, role="merchant")
    session.add(conversation)
    merchant_bot.show_menu(
        session,
        conversation,
        merchant,
        f"🎉 Payment received! Your *{plan.name}* plan is active: {plan.drops} drops until {when(merchant.plan_ends)}.{follows}",
        now=event.received_at,
    )
    if approval.receipt:
        return f"✅ Approved {request.momo_reference}: {merchant.shop_name} now on {plan.name}. Receipt {approval.receipt.number} sent."
    billing.ask_admins_for_fdn(session, approval.fiscal)
    return (
        f"✅ Approved {request.momo_reference}: {merchant.shop_name} now on {plan.name}.\n"
        f"EFRIS still owed (within 24 hours): /fdn {request.momo_reference} <FDN> <code>"
    )


def cmd_fiscal(session, admin, args, event):
    rows = (
        session.execute(
            select(FiscalDocument).where(FiscalDocument.status == FiscalStatus.PENDING).order_by(FiscalDocument.id)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "Every sale has its EFRIS FDN ✅"
    lines = []
    for doc in rows:
        request = session.get(PlanRequest, doc.plan_request_id)
        m = session.get(Merchant, request.merchant_id)
        kind = "credit note" if doc.kind == FiscalKind.CREDIT_NOTE else "e-receipt"
        lines.append(
            f"{doc.reference} · {kind} · {m.shop_name} · TIN {m.tin or 'none'} · {ugx(doc.amount_ugx)} · since {when(doc.created_at)}"
        )
    return "Waiting for EFRIS:\n" + "\n".join(lines) + "\n\n/fdn REF FDN CODE"


def cmd_fdn(session, admin, args, event):
    _need(args, 3, "/fdn REF FDN VERIFICATION-CODE")
    doc = session.execute(
        select(FiscalDocument).where(FiscalDocument.reference == args[0].upper())
    ).scalar_one_or_none()
    if doc is None:
        raise CommandError(f"nothing waiting for EFRIS under {args[0]}. Send /fiscal")
    try:
        receipt = billing.record_fdn(session, doc, args[1], args[2], admin, event.received_at)
    except billing.BillingError as exc:
        raise CommandError(str(exc)) from None
    billing.fdn_recorded(session, doc, receipt)
    if receipt is not None:
        return f"✅ FDN recorded for {doc.reference}. Receipt {receipt.number} sent to the shop."
    return f"✅ FDN recorded for {doc.reference}."


def cmd_refund(session, admin, args, event):
    _need(args, 2, "/refund REF reason  or  /refund REF extra reason")
    request = _request(session, args[0])
    extra = args[1].lower() == "extra"
    reason = " ".join(args[2:] if extra else args[1:])
    try:
        refund = billing.request_refund(session, request, reason, admin, event.received_at, extra=extra)
    except billing.BillingError as exc:
        raise CommandError(str(exc)) from None
    if refund.method == "manual":
        return f"↩️ Recorded: {request.momo_reference} refunded by hand ({ugx(refund.amount_ugx)})."
    if refund.status == RefundStatus.FAILED:
        return f"⚠️ pawaPay refused the refund ({refund.failure_code}). Nothing was sent."
    return f"↩️ Refund of {ugx(refund.amount_ugx)} for {request.momo_reference} sent to pawaPay. You'll be told when it's through."


def cmd_reconcile(session, admin, args, event):
    issues = billing.reconcile(session, event.received_at)
    if not issues:
        return "✅ Payments, plans, EFRIS and receipts all agree."
    return "⚠️ Needs attention:\n" + "\n".join(f"• {i}" for i in issues[:25])


def cmd_mydata(session, admin, args, event):
    _need(args, 1, "/mydata PHONE")
    return privacy.describe(session, _phone(args[0]))


def cmd_forget(session, admin, args, event):
    _need(args, 1, "/forget PHONE")
    try:
        return privacy.forget(session, _phone(args[0]), event.received_at)
    except privacy.ForgetRefused as exc:
        raise CommandError(str(exc)) from None


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


_NIN = re.compile(r"^[A-Z0-9]{14}$")
_DOC = re.compile(r"^[A-Z0-9/-]{4,32}$")
_PLATE = re.compile(r"^U[A-Z0-9]{4,9}$")  # old (UFA123B) and new digital plates
RIDER_ADD = (
    "/rider add PHONE PERMIT-NO NIN PLATE Name  (e.g. /rider add 0772123456 DL1234567 CM90001234ABCD UFA123B Musa K)"
)


def cmd_rider(session, admin, args, event):
    _need(args, 2, "/rider add PHONE PERMIT-NO NIN PLATE Name  or  /rider remove PHONE")
    action, phone = args[0].lower(), _phone(args[1])
    rider = session.execute(select(Rider).where(Rider.phone == phone)).scalar_one_or_none()
    if action == "add":
        _need(args, 6, RIDER_ADD)
        permit, nin, plate = (re.sub(r"\s", "", a).upper() for a in args[2:5])
        if not _DOC.match(permit):
            raise CommandError(f"{args[2]} doesn't look like a driving permit number. {RIDER_ADD}")
        if not _NIN.match(nin):
            raise CommandError(f"{args[3]} isn't a national ID number (NIN: 14 letters and digits). {RIDER_ADD}")
        if not _PLATE.match(plate):
            raise CommandError(f"{args[4]} isn't a Ugandan number plate (starts with U, like UFA123B). {RIDER_ADD}")
        name = " ".join(args[5:])
        if rider is None:
            rider = Rider(phone=phone, name=name, on_shift=False, active=True)
            session.add(rider)
        else:
            rider.name, rider.active = name, True
        rider.permit_number, rider.national_id, rider.plate = permit, nin, plate
        rider.documents_checked_at, rider.documents_checked_by = event.received_at, admin
        session.flush()
        if rider.privacy_accepted_at is None:
            box(session).buttons(phone, rider_bot.privacy_notice(name), [("r:agree", "I agree")], optional=True)
        else:
            box(session).buttons(phone, f"You're registered as a Men-Ions rider, {name}. 🛵", [("r:on", "Go on shift")],
                                 optional=True)  # fmt: skip
        return (
            f"Rider {name} ({local_phone(phone)}, {plate}) added; documents recorded as checked by you. "
            "They get a welcome if they've messaged us in the last 24 hours; otherwise ask them to send *hi*."
        )
    if action == "remove":
        if rider is None:
            raise CommandError(f"no rider with phone {args[1]}")
        rider.active, rider.on_shift = False, False
        return f"Rider {rider.name} removed."
    raise CommandError("usage: /rider add PHONE PERMIT-NO NIN PLATE Name  or  /rider remove PHONE")


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
    "fiscal": cmd_fiscal,
    "fdn": cmd_fdn,
    "refund": cmd_refund,
    "reconcile": cmd_reconcile,
    "mydata": cmd_mydata,
    "forget": cmd_forget,
    "orders": cmd_orders,
    "assign": cmd_assign,
    "reschedule": cmd_reschedule,
    "cancel": cmd_cancel,
    "unlock": cmd_unlock,
    "riders": cmd_riders,
    "rider": cmd_rider,
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
