"""Plan payments end to end: collecting the money (pawaPay), what we owe URA (EFRIS), refunds,
and the daily check that all three agree.

  merchant taps Pay -> start_deposit() -> PIN prompt on their phone
  pawaPay callback or the worker's poll -> settle_deposit()
      COMPLETED: plan starts (plans.approve_plan_request), an EFRIS document is opened
      FAILED:    the merchant is told why, with Try again
  EFRIS: fiscalise_pending() (automatic provider) or /fdn REF FDN CODE (manual)
      -> record_fdn() -> the plan_purchase receipt is issued and sent

Money moves are never trusted from a callback alone: every status comes from asking pawaPay.
Messages are queued on the session's outbox; the caller sends them after committing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import efris, plans, receipts
from app.bots.common import box, local_phone, ugx, when
from app.bots.effects import notify_admins, send_receipts
from app.clock import kampala_date, kampala_day_bounds, utcnow
from app.config import get_settings
from app.models import (
    AdminAlert,
    Deposit,
    DepositStatus,
    FiscalDocument,
    FiscalKind,
    FiscalStatus,
    Merchant,
    PlanRequest,
    PlanRequestStatus,
    Receipt,
    ReceiptType,
    Refund,
    RefundStatus,
)
from app.payments import get_gateway
from app.payments.base import PROVIDER_NAMES, GatewayError, Transaction, provider_from_prefix

log = logging.getLogger(__name__)

LIVE_DEPOSIT = (
    DepositStatus.INITIATED, DepositStatus.ACCEPTED, DepositStatus.PROCESSING, DepositStatus.IN_RECONCILIATION,
)  # fmt: skip
LIVE_REFUND = (
    RefundStatus.INITIATED, RefundStatus.ACCEPTED, RefundStatus.PROCESSING, RefundStatus.IN_RECONCILIATION,
)  # fmt: skip
PAYABLE = (PlanRequestStatus.AWAITING_PAYMENT, PlanRequestStatus.PAYMENT_CLAIMED)
AUTO_PREFIX = "pawapay:"

# What the merchant is told when a payment fails, by pawaPay's failure code.
FAILURE_TEXT = {
    "PAYMENT_NOT_APPROVED": "The payment wasn't approved: the PIN prompt was cancelled or timed out.",
    "INSUFFICIENT_BALANCE": "There wasn't enough money on that Mobile Money account.",
    "PAYER_LIMIT_REACHED": "That Mobile Money account has reached its limit.",
    "WALLET_LIMIT_REACHED": "That Mobile Money account has reached its limit.",
    "PAYER_NOT_FOUND": "That number isn't registered for Mobile Money.",
    "PAYMENT_IN_PROGRESS": "Another payment is still in progress on that number. Wait a few minutes, then try again.",
    "UNSUPPORTED_NETWORK": "We take MTN Mobile Money and Airtel Money only.",
    "PROVIDER_TEMPORARILY_UNAVAILABLE": "Mobile Money is down right now. Please try again in a few minutes.",
    "GATEWAY_ERROR": "We couldn't reach Mobile Money just now.",
}
DEFAULT_FAILURE = "The payment didn't go through."


class BillingError(Exception):
    pass


def _now(at: datetime | None) -> datetime:
    return at or utcnow()


def _lock(session: Session, model, row_id: int):
    session.flush()
    return session.execute(
        select(model).where(model.id == row_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def _merchant(session: Session, request: PlanRequest) -> Merchant:
    return session.get(Merchant, request.merchant_id)


# --- EFRIS documents ---------------------------------------------------------------------


def credit_reference(request: PlanRequest) -> str:
    return f"{request.momo_reference}-CN"


def open_invoice(session: Session, request: PlanRequest, at: datetime) -> FiscalDocument:
    """Record that URA's e-receipt is owed for this sale. Admins are asked for it when the
    EFRIS provider is manual."""
    doc = FiscalDocument(
        plan_request_id=request.id,
        kind=FiscalKind.INVOICE,
        reference=request.momo_reference,
        status=FiscalStatus.PENDING,
        amount_ugx=request.amount_ugx,
        created_at=at,
        next_attempt_at=at,
    )
    session.add(doc)
    session.flush()
    return doc


def _open_credit_note(session: Session, request: PlanRequest, invoice: FiscalDocument, at: datetime):
    doc = FiscalDocument(
        plan_request_id=request.id,
        kind=FiscalKind.CREDIT_NOTE,
        reference=credit_reference(request),
        status=FiscalStatus.PENDING,
        amount_ugx=invoice.amount_ugx,
        created_at=at,
        next_attempt_at=at,
    )
    session.add(doc)
    session.flush()
    ask_admins_for_fdn(session, doc)
    return doc


def fiscal_request(session: Session, doc: FiscalDocument) -> efris.FiscalRequest:
    request = session.get(PlanRequest, doc.plan_request_id)
    merchant = _merchant(session, request)
    plan = plans.get_plan(request.plan)
    original = None
    if doc.kind == FiscalKind.CREDIT_NOTE:
        original = session.execute(
            select(FiscalDocument.fdn).where(
                FiscalDocument.plan_request_id == request.id, FiscalDocument.kind == FiscalKind.INVOICE
            )
        ).scalar()
    return efris.FiscalRequest(
        reference=doc.reference,
        kind=doc.kind,
        amount_ugx=doc.amount_ugx,
        item=f"Delivery plan: {plan.name} ({plan.drops} drops)",
        buyer_name=merchant.shop_name,
        buyer_phone=merchant.phone,
        buyer_tin=merchant.tin,
        original_fdn=original,
    )


def ask_admins_for_fdn(session: Session, doc: FiscalDocument) -> None:
    """With the manual provider, tell the admins exactly what to issue on URA's portal."""
    if efris.get_provider().automatic:
        return
    req = fiscal_request(session, doc)
    what = "EFRIS credit note" if doc.kind == FiscalKind.CREDIT_NOTE else "EFRIS e-receipt"
    lines = [
        f"🧾 {what} needed for {doc.reference}",
        f"Buyer: {req.buyer_name}, {local_phone(req.buyer_phone)}",
        f"Buyer TIN: {req.buyer_tin or 'none (ordinary buyer)'}",
        f"Item: {req.item}",
        f"Amount: {ugx(req.amount_ugx)}",
    ]
    if req.original_fdn:
        lines.append(f"Reverses FDN: {req.original_fdn}")
    lines.append(f"\nIssue it on URA's portal, then send:\n/fdn {doc.reference} <FDN> <verification code>")
    notify_admins(session, "\n".join(lines))


def _paid_with(session: Session, request: PlanRequest) -> str:
    deposit = approving_deposit(session, request)
    if deposit is None:
        return "Mobile Money (sent by hand)"
    name = PROVIDER_NAMES.get(deposit.provider, "Mobile Money")
    txn = f", transaction {deposit.provider_transaction_id}" if deposit.provider_transaction_id else ""
    return f"{name} {local_phone(deposit.payer_phone)}{txn}"


def record_fdn(
    session: Session,
    doc: FiscalDocument,
    fdn: str,
    verification_code: str,
    by: str,
    at: datetime | None = None,
    qr_code: str | None = None,
) -> Receipt | None:
    """URA's numbers for this document are known. For a sale, issue the plan_purchase
    receipt (returned; the caller sends it). Recording the same FDN twice is harmless."""
    at = _now(at)
    fdn, verification_code = (fdn or "").strip(), (verification_code or "").strip()
    if not fdn or not verification_code:
        raise BillingError("both the FDN and the verification code are needed")
    doc = _lock(session, FiscalDocument, doc.id)
    if doc.status == FiscalStatus.CANCELLED:
        raise BillingError(f"{doc.reference} was cancelled (refunded before EFRIS); nothing to record")
    if doc.status == FiscalStatus.ISSUED:
        if doc.fdn == fdn:
            return None
        raise BillingError(f"{doc.reference} already has FDN {doc.fdn}")
    doc.status = FiscalStatus.ISSUED
    doc.fdn, doc.verification_code, doc.qr_code = fdn, verification_code, qr_code
    doc.issued_by, doc.issued_at = str(by), at
    session.flush()
    if doc.kind != FiscalKind.INVOICE:
        return None

    request = session.get(PlanRequest, doc.plan_request_id)
    merchant = _merchant(session, request)
    plan = plans.get_plan(request.plan)
    receipt = receipts.issue_plan_purchase(
        session,
        merchant=merchant,
        request=request,
        plan_name=plan.name,
        drops=plan.drops,
        valid_until=plans.plan_end(plan, request.approved_at),
        paid_with=_paid_with(session, request),
        efris_fdn=fdn,
        efris_verification_code=verification_code,
        at=at,
    )
    doc.receipt_id = receipt.id
    session.flush()
    return receipt


def fdn_recorded(session: Session, doc: FiscalDocument, receipt: Receipt | None) -> None:
    """Queue the messages that follow a recorded FDN."""
    if receipt is not None:
        request = session.get(PlanRequest, doc.plan_request_id)
        send_receipts(session, [receipt], plans.get_plan(request.plan).name)


def fiscalise_pending(session: Session, now: datetime | None = None) -> int:
    """Automatic EFRIS providers: issue every document that is due. Each in its own
    savepoint, with a growing wait after each failure. Returns how many were issued."""
    provider = efris.get_provider()
    if not provider.automatic:
        return 0
    now = _now(now)
    due = session.execute(
        select(FiscalDocument)
        .where(FiscalDocument.status == FiscalStatus.PENDING, FiscalDocument.next_attempt_at <= now)
        .order_by(FiscalDocument.id)
        .limit(50)
    ).scalars().all()  # fmt: skip
    issued = 0
    for doc in due:
        try:
            result = provider.issue(fiscal_request(session, doc))
        except efris.EfrisUnavailable as exc:
            doc.attempts += 1
            doc.last_error = str(exc)[:500]
            doc.next_attempt_at = now + timedelta(minutes=min(2**doc.attempts, 60))
            session.flush()
            continue
        with session.begin_nested():
            receipt = record_fdn(session, doc, result.fdn, result.verification_code, "efris", now, result.qr_code)
            fdn_recorded(session, doc, receipt)
        issued += 1
    return issued


def alert_stale_fiscal(session: Session, now: datetime | None = None) -> int:
    """Alert the admins, once per document, when an FDN has waited EFRIS_ALERT_HOURS."""
    now = _now(now)
    cutoff = now - timedelta(hours=get_settings().efris_alert_hours)
    stale = session.execute(
        select(FiscalDocument).where(
            FiscalDocument.status == FiscalStatus.PENDING,
            FiscalDocument.created_at <= cutoff,
            FiscalDocument.alerted_at.is_(None),
        )
    ).scalars().all()  # fmt: skip
    for doc in stale:
        doc.alerted_at = now
        deadline = when(doc.created_at + timedelta(hours=24))
        session.add(AdminAlert(kind="efris_overdue", at=now, data={"reference": doc.reference}))
        notify_admins(
            session,
            f"⏰ {doc.reference} still has no EFRIS FDN. URA's deadline is {deadline}.\n"
            f"/fdn {doc.reference} <FDN> <verification code>"
            + (f"\nLast error: {doc.last_error}" if doc.last_error else ""),
        )
    session.flush()
    return len(stale)


# --- Collecting payments (pawaPay) -------------------------------------------------------


@dataclass(frozen=True)
class Started:
    deposit: Deposit | None
    failure_code: str | None  # None: the PIN prompt is on its way


def _customer_message(request: PlanRequest) -> str:
    # pawaPay: 4-22 characters, letters, digits and spaces. Shown on the payer's statement.
    name = plans.get_plan(request.plan).name
    return "".join(c for c in f"MenIons {name}" if c.isalnum() or c == " ")[:22]


def live_deposit(session: Session, request: PlanRequest) -> Deposit | None:
    return session.execute(
        select(Deposit)
        .where(Deposit.plan_request_id == request.id, Deposit.status.in_(LIVE_DEPOSIT))
        .order_by(Deposit.id.desc())
    ).scalars().first()  # fmt: skip


def start_deposit(session: Session, request: PlanRequest, phone: str, at: datetime | None = None) -> Started:
    """Ask pawaPay to send a PIN prompt to `phone` for this plan request.

    The deposit row is written before pawaPay is called, so a lost answer can be checked
    later by its id. Only one attempt runs at a time per request.
    """
    at = _now(at)
    gateway = get_gateway()
    request = _lock(session, PlanRequest, request.id)
    if request.status not in PAYABLE:
        raise BillingError(f"request is {request.status}; it can't be paid")
    live = live_deposit(session, request)
    if live is not None:
        return Started(live, "PAYMENT_IN_PROGRESS")

    try:
        provider = gateway.predict_provider(phone) or provider_from_prefix(phone)
    except GatewayError:
        provider = provider_from_prefix(phone)
    if provider not in PROVIDER_NAMES:
        return Started(None, "UNSUPPORTED_NETWORK")

    deposit = Deposit(
        deposit_id=str(uuid.uuid4()),
        plan_request_id=request.id,
        payer_phone=phone,
        provider=provider,
        amount_ugx=request.amount_ugx,
        status=DepositStatus.INITIATED,
        created_at=at,
        updated_at=at,
    )
    session.add(deposit)
    session.flush()
    try:
        result = gateway.initiate_deposit(
            deposit.deposit_id, deposit.amount_ugx, phone, provider, _customer_message(request)
        )
    except GatewayError:
        log.exception("deposit %s: initiate failed; the worker will check its status", deposit.deposit_id)
        return Started(deposit, "GATEWAY_ERROR")
    if result.status == "REJECTED":
        deposit.status, deposit.failure_code = DepositStatus.FAILED, result.failure_code or "REJECTED"
        session.flush()
        return Started(deposit, deposit.failure_code)
    deposit.status = DepositStatus.ACCEPTED
    session.flush()
    return Started(deposit, None)


def failure_text(code: str | None) -> str:
    return FAILURE_TEXT.get(code or "", DEFAULT_FAILURE)


def retry_buttons(request: PlanRequest) -> list[tuple[str, str]]:
    third = (
        (f"manual:{request.id}", "Pay by hand") if get_settings().momo_pay_number else ("plan:change", "Change plan")
    )
    return [(f"pay:{request.id}", "Try again"), (f"paynum:{request.id}", "Another number"), third]


def approving_deposit(session: Session, request: PlanRequest) -> Deposit | None:
    by = request.approved_by or ""
    if not by.startswith(AUTO_PREFIX):
        return None
    return session.execute(
        select(Deposit).where(Deposit.deposit_id == by.removeprefix(AUTO_PREFIX))
    ).scalar_one_or_none()


def _amount_matches(tx: Transaction, expected: int) -> bool:
    try:
        return tx.currency == "UGX" and Decimal(str(tx.amount)) == Decimal(expected)
    except (InvalidOperation, TypeError):
        return False


def settle_deposit(session: Session, deposit: Deposit, tx: Transaction, at: datetime | None = None) -> str:
    """Apply what pawaPay says about a deposit. Safe to call any number of times.

    Returns: pending | failed | approved | extra (money in, but the request was already paid
    or closed) | mismatch (amount or currency differs) | final (nothing new).
    """
    at = _now(at)
    deposit = _lock(session, Deposit, deposit.id)
    if deposit.status in (DepositStatus.COMPLETED, DepositStatus.FAILED):
        return "final"
    request = session.get(PlanRequest, deposit.plan_request_id)
    merchant = _merchant(session, request)
    deposit.updated_at = at

    if not tx.found:
        # pawaPay never got it. Only conclude that once the initiate call can't still be in flight.
        if deposit.status == DepositStatus.INITIATED and at - deposit.created_at > timedelta(minutes=2):
            deposit.status, deposit.failure_code = DepositStatus.FAILED, "NOT_FOUND"
            session.flush()
            return "failed"
        session.flush()
        return "pending"

    if tx.status in ("ACCEPTED", "PROCESSING", "IN_RECONCILIATION"):
        deposit.status = tx.status
        session.flush()
        return "pending"

    if tx.status == "FAILED":
        deposit.status, deposit.failure_code = DepositStatus.FAILED, tx.failure_code or "UNSPECIFIED_FAILURE"
        session.flush()
        if request.status in PAYABLE:
            box(session).buttons(
                merchant.phone,
                f"❌ {failure_text(deposit.failure_code)}\n\nYour {plans.get_plan(request.plan).name} plan "
                f"({ugx(request.amount_ugx)}) is not paid yet.",
                retry_buttons(request),
            )
        return "failed"

    if tx.status != "COMPLETED":
        log.warning("deposit %s: unknown status %r", deposit.deposit_id, tx.status)
        session.flush()
        return "pending"

    deposit.status = DepositStatus.COMPLETED
    deposit.completed_at = at
    deposit.provider_transaction_id = tx.provider_transaction_id
    session.flush()
    network = PROVIDER_NAMES.get(deposit.provider, "Mobile Money")

    if not _amount_matches(tx, deposit.amount_ugx):
        session.add(AdminAlert(kind="payment_amount_mismatch", at=at, data={
            "deposit_id": deposit.deposit_id, "expected": deposit.amount_ugx, "amount": tx.amount, "currency": tx.currency,
        }))  # fmt: skip
        notify_admins(
            session,
            f"⚠️ Payment for {request.momo_reference} came in as {tx.currency} {tx.amount}, not {ugx(deposit.amount_ugx)}. "
            f"The plan was NOT started. Check pawaPay, send it back with /refund {request.momo_reference} extra, "
            "and ask the shop to pay again.",
        )
        return "mismatch"

    if request.status not in PAYABLE:
        session.add(AdminAlert(kind="extra_payment", at=at, data={
            "deposit_id": deposit.deposit_id, "reference": request.momo_reference, "request_status": request.status,
        }))  # fmt: skip
        notify_admins(
            session,
            f"⚠️ Extra payment: {merchant.shop_name} paid {ugx(deposit.amount_ugx)} again for {request.momo_reference} "
            f"(already {request.status.lower()}). Refund it: /refund {request.momo_reference} extra",
        )
        box(session).text(
            merchant.phone,
            f"We received {ugx(deposit.amount_ugx)} from {local_phone(deposit.payer_phone)}, but {request.momo_reference} "
            "was already paid. We'll send this extra payment back to you.",
        )
        return "extra"

    approval = plans.approve_plan_request(session, request, f"{AUTO_PREFIX}{deposit.deposit_id}", at)
    plan = plans.get_plan(request.plan)
    _welcome_paid_merchant(
        session, merchant, at,
        f"🎉 Payment received from {network} {local_phone(deposit.payer_phone)}. Your *{plan.name}* plan is "
        f"active: {plan.drops} drops until {when(merchant.plan_ends)}.\n\nYour receipt with URA's EFRIS number follows shortly.",
    )  # fmt: skip
    new = "🆕 New shop now ACTIVE. " if approval and approval.was_pending else ""
    notify_admins(
        session,
        f"💰 {new}{merchant.shop_name} paid {ugx(request.amount_ugx)} for {plan.name} by {network} "
        f"({request.momo_reference}). {local_phone(merchant.phone)}"
        + (f"\nAnything wrong? /suspend {local_phone(merchant.phone).replace(' ', '')}" if new else ""),
    )
    if approval:
        ask_admins_for_fdn(session, approval.fiscal)
    return "approved"


def _welcome_paid_merchant(session: Session, merchant: Merchant, at: datetime, intro: str) -> None:
    from app.bots import merchant as merchant_bot
    from app.models import Conversation

    conversation = session.get(Conversation, merchant.phone)
    if conversation is None:
        conversation = Conversation(phone=merchant.phone, role="merchant", context={})
        session.add(conversation)
    merchant_bot.show_menu(session, conversation, merchant, intro, now=at)


def check_deposit(session: Session, deposit: Deposit, at: datetime | None = None) -> str:
    """Ask pawaPay for this deposit's status and apply it."""
    return settle_deposit(session, deposit, get_gateway().get_deposit(deposit.deposit_id), at)


def poll_deposits(session: Session, now: datetime | None = None) -> int:
    """Backstop for missed callbacks: check every unfinished deposit older than a minute."""
    now = _now(now)
    rows = session.execute(
        select(Deposit)
        .where(Deposit.status.in_(LIVE_DEPOSIT), Deposit.created_at <= now - timedelta(minutes=1))
        .order_by(Deposit.id)
        .limit(50)
    ).scalars().all()  # fmt: skip
    done = 0
    for deposit in rows:
        try:
            with session.begin_nested():
                if check_deposit(session, deposit, now) != "pending":
                    done += 1
        except GatewayError:
            log.warning("deposit %s: status check failed; next round", deposit.deposit_id)
    return done


# --- Refunds -----------------------------------------------------------------------------


def _completed_deposits(session: Session, request: PlanRequest) -> list[Deposit]:
    return list(
        session.execute(
            select(Deposit)
            .where(Deposit.plan_request_id == request.id, Deposit.status == DepositStatus.COMPLETED)
            .order_by(Deposit.id)
        ).scalars()
    )


def _refunded(session: Session, request: PlanRequest, deposit: Deposit | None) -> Refund | None:
    """A refund for this payment that is done or under way."""
    q = select(Refund).where(Refund.plan_request_id == request.id, Refund.status != RefundStatus.FAILED)
    q = q.where(Refund.deposit_id == deposit.id) if deposit else q.where(Refund.deposit_id.is_(None))
    return session.execute(q).scalars().first()


def request_refund(
    session: Session, request: PlanRequest, reason: str, by: str, at: datetime | None = None, *, extra: bool = False
) -> Refund:
    """Send a plan payment back. extra=True refunds a second payment for an already paid
    request (the plan stays). Otherwise the plan payment itself: once the money is back the
    plan ends, the receipt is voided and an EFRIS credit note is owed.

    A payment collected through pawaPay is refunded through pawaPay. One sent by hand must be
    sent back by hand first: this then records it."""
    at = _now(at)
    reason = (reason or "").strip()
    if not reason:
        raise BillingError("give a reason for the refund")
    request = _lock(session, PlanRequest, request.id)
    approving = approving_deposit(session, request)

    if extra:
        extras = [d for d in _completed_deposits(session, request) if d.id != (approving.id if approving else None)]
        deposit = next((d for d in extras if _refunded(session, request, d) is None), None)
        if deposit is None:
            raise BillingError(f"{request.momo_reference} has no extra payment left to refund")
    else:
        if request.status != PlanRequestStatus.APPROVED:
            raise BillingError(f"{request.momo_reference} is {request.status}; only an approved plan can be refunded")
        deposit = approving
        if _refunded(session, request, deposit) is not None:
            raise BillingError(f"{request.momo_reference} is already being refunded")

    refund = Refund(
        refund_id=str(uuid.uuid4()),
        plan_request_id=request.id,
        deposit_id=deposit.id if deposit else None,
        method="pawapay" if deposit else "manual",
        amount_ugx=deposit.amount_ugx if deposit else request.amount_ugx,
        status=RefundStatus.INITIATED,
        reason=reason[:300],
        requested_by=str(by),
        created_at=at,
        updated_at=at,
    )
    session.add(refund)
    session.flush()

    if deposit is None:
        complete_refund(session, refund, at)
        return refund
    try:
        result = get_gateway().initiate_refund(refund.refund_id, deposit.deposit_id, refund.amount_ugx)
    except GatewayError:
        log.exception("refund %s: initiate failed; the worker will check its status", refund.refund_id)
        return refund
    if result.status == "REJECTED":
        refund.status, refund.failure_code = RefundStatus.FAILED, result.failure_code or "REJECTED"
    else:
        refund.status = RefundStatus.ACCEPTED
    session.flush()
    return refund


def complete_refund(session: Session, refund: Refund, at: datetime | None = None) -> None:
    """The money is back with the merchant."""
    at = _now(at)
    refund.status, refund.completed_at, refund.updated_at = RefundStatus.COMPLETED, at, at
    request = _lock(session, PlanRequest, refund.plan_request_id)
    merchant = session.execute(
        select(Merchant).where(Merchant.id == request.merchant_id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()  # fmt: skip
    approving = approving_deposit(session, request)
    is_extra = refund.deposit_id is not None and (approving is None or refund.deposit_id != approving.id)
    session.flush()

    if is_extra:
        box(session).text(
            merchant.phone,
            f"↩️ We've sent back the extra {ugx(refund.amount_ugx)} you paid for {request.momo_reference}.",
        )
        notify_admins(session, f"↩️ Extra payment for {request.momo_reference} refunded ({ugx(refund.amount_ugx)}).")
        return

    request.status = PlanRequestStatus.REFUNDED
    # End the plan if it is still the one this payment bought.
    if merchant.plan == request.plan and merchant.plan_starts == request.approved_at:
        merchant.drops_left = 0
        merchant.plan_ends = at
    invoice = session.execute(
        select(FiscalDocument).where(
            FiscalDocument.plan_request_id == request.id, FiscalDocument.kind == FiscalKind.INVOICE
        )
    ).scalar_one_or_none()
    if invoice is not None and invoice.status == FiscalStatus.PENDING:
        invoice.status = FiscalStatus.CANCELLED  # never reported to URA: nothing to reverse
    elif invoice is not None and invoice.status == FiscalStatus.ISSUED:
        _open_credit_note(session, request, invoice, at)
    receipt = session.execute(
        select(Receipt).where(Receipt.plan_request_id == request.id, Receipt.type == ReceiptType.PLAN_PURCHASE)
    ).scalar_one_or_none()
    if receipt is not None:
        receipts.void_receipt(session, receipt, f"Refunded: {refund.reason}", refund.requested_by, at)
    session.flush()
    box(session).text(
        merchant.phone,
        f"↩️ We've refunded {ugx(refund.amount_ugx)} for {request.momo_reference}. "
        f"That {plans.get_plan(request.plan).name} plan has ended.",
    )
    notify_admins(session, f"↩️ {request.momo_reference} refunded ({ugx(refund.amount_ugx)}): {refund.reason}")


def settle_refund(session: Session, refund: Refund, tx: Transaction, at: datetime | None = None) -> str:
    at = _now(at)
    refund = _lock(session, Refund, refund.id)
    if refund.status in (RefundStatus.COMPLETED, RefundStatus.FAILED):
        return "final"
    refund.updated_at = at
    if not tx.found:
        if refund.status == RefundStatus.INITIATED and at - refund.created_at > timedelta(minutes=2):
            refund.status, refund.failure_code = RefundStatus.FAILED, "NOT_FOUND"
            _refund_failed(session, refund)
            return "failed"
        return "pending"
    if tx.status == "COMPLETED":
        complete_refund(session, refund, at)
        return "completed"
    if tx.status == "FAILED":
        refund.status, refund.failure_code = RefundStatus.FAILED, tx.failure_code or "UNSPECIFIED_FAILURE"
        _refund_failed(session, refund)
        return "failed"
    if tx.status in ("ACCEPTED", "PROCESSING", "IN_RECONCILIATION"):
        refund.status = tx.status
    session.flush()
    return "pending"


def _refund_failed(session: Session, refund: Refund) -> None:
    session.flush()
    request = session.get(PlanRequest, refund.plan_request_id)
    notify_admins(
        session,
        f"⚠️ Refund for {request.momo_reference} failed ({refund.failure_code}). The money was NOT sent back. "
        "Check pawaPay, then try /refund again or send it by hand.",
    )


def poll_refunds(session: Session, now: datetime | None = None) -> int:
    now = _now(now)
    rows = session.execute(
        select(Refund)
        .where(Refund.status.in_(LIVE_REFUND), Refund.method == "pawapay", Refund.created_at <= now - timedelta(minutes=1))
        .order_by(Refund.id)
        .limit(50)
    ).scalars().all()  # fmt: skip
    done = 0
    for refund in rows:
        try:
            with session.begin_nested():
                if settle_refund(session, refund, get_gateway().get_refund(refund.refund_id), now) != "pending":
                    done += 1
        except GatewayError:
            log.warning("refund %s: status check failed; next round", refund.refund_id)
    return done


# --- The daily check -----------------------------------------------------------------------


def reconcile(session: Session, now: datetime | None = None) -> list[str]:
    """Everything that doesn't add up between payments, plans, EFRIS and receipts. Empty: clean."""
    now = _now(now)
    issues: list[str] = []

    for deposit in session.execute(
        select(Deposit).where(Deposit.status == DepositStatus.COMPLETED).order_by(Deposit.id)
    ).scalars():
        request = session.get(PlanRequest, deposit.plan_request_id)
        approving = approving_deposit(session, request)
        if approving is not None and approving.id == deposit.id:
            continue
        refund = _refunded(session, request, deposit)
        if refund is None or refund.status != RefundStatus.COMPLETED:
            issues.append(
                f"{request.momo_reference}: {ugx(deposit.amount_ugx)} received ({deposit.deposit_id[:8]}) with no plan "
                f"and no refund. /refund {request.momo_reference} extra"
            )

    for deposit in session.execute(
        select(Deposit).where(Deposit.status.in_(LIVE_DEPOSIT), Deposit.created_at <= now - timedelta(hours=1))
    ).scalars():
        request = session.get(PlanRequest, deposit.plan_request_id)
        issues.append(f"{request.momo_reference}: payment {deposit.deposit_id[:8]} unfinished for over an hour")

    for refund in session.execute(
        select(Refund).where(Refund.status.in_(LIVE_REFUND), Refund.created_at <= now - timedelta(hours=1))
    ).scalars():
        request = session.get(PlanRequest, refund.plan_request_id)
        issues.append(f"{request.momo_reference}: refund {refund.refund_id[:8]} unfinished for over an hour")

    for doc in session.execute(
        select(FiscalDocument).where(FiscalDocument.status == FiscalStatus.PENDING).order_by(FiscalDocument.id)
    ).scalars():
        age = now - doc.created_at
        if age > timedelta(hours=24):
            issues.append(f"{doc.reference}: EFRIS {doc.kind.replace('_', ' ')} OVERDUE (sale {when(doc.created_at)})")

    for doc in session.execute(
        select(FiscalDocument).where(
            FiscalDocument.status == FiscalStatus.ISSUED,
            FiscalDocument.kind == FiscalKind.INVOICE,
            FiscalDocument.receipt_id.is_(None),
        )
    ).scalars():
        issues.append(f"{doc.reference}: FDN {doc.fdn} recorded but no receipt issued")

    no_invoice = session.execute(
        select(PlanRequest).where(
            PlanRequest.status.in_((PlanRequestStatus.APPROVED, PlanRequestStatus.REFUNDED)),
            ~select(FiscalDocument.id)
            .where(FiscalDocument.plan_request_id == PlanRequest.id, FiscalDocument.kind == FiscalKind.INVOICE)
            .exists(),
        )
    ).scalars()
    for request in no_invoice:
        issues.append(f"{request.momo_reference}: approved with no EFRIS record")
    return issues


def daily_summary(session: Session, now: datetime | None = None) -> str:
    """Yesterday's plan sales plus anything that doesn't add up, for the admins."""
    now = _now(now)
    day = kampala_date(now) - timedelta(days=1)
    start, end = kampala_day_bounds(day)
    count, total = session.execute(
        select(func.count(PlanRequest.id), func.coalesce(func.sum(PlanRequest.amount_ugx), 0)).where(
            PlanRequest.approved_at >= start,
            PlanRequest.approved_at < end,
            PlanRequest.status.in_((PlanRequestStatus.APPROVED, PlanRequestStatus.REFUNDED)),
        )
    ).one()
    pending = session.execute(
        select(func.count(FiscalDocument.id)).where(FiscalDocument.status == FiscalStatus.PENDING)
    ).scalar_one()
    issues = reconcile(session, now)
    lines = [f"📊 {day.strftime('%a %d %b')}: {count} plan sale(s), {ugx(int(total))}."]
    lines.append(f"EFRIS documents waiting: {pending}." if pending else "Every sale has its EFRIS FDN.")
    if issues:
        lines.append("\n⚠️ Needs attention:\n" + "\n".join(f"• {i}" for i in issues[:15]))
        if len(issues) > 15:
            lines.append(f"…and {len(issues) - 15} more. Send /reconcile")
    else:
        lines.append("Payments, plans, EFRIS and receipts all agree ✅")
    return "\n".join(lines)
