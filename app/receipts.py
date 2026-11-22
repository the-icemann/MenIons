"""Receipts: an append-only, hash-chained record of every handover of goods and cash.

issue_receipt() is the only way to create a receipt. Each receipt's hash covers the
previous receipt's hash, so changing or removing any earlier receipt breaks the chain
(verify_chain finds the first break). The database also rejects UPDATE and DELETE.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import format_kampala, kampala_day_bounds, utcnow
from app.config import get_settings
from app.db import get_session
from app.models import (
    AdminAlert,
    CashHandover,
    CodPayout,
    EventKind,
    Merchant,
    Order,
    OrderEvent,
    OrderStatus,
    PlanRequest,
    Receipt,
    ReceiptType,
    Rider,
)

GENESIS_HASH = "0" * 64
# Any fixed number: it names the advisory lock that serialises receipt numbering.
_CHAIN_LOCK_KEY = 5_210_011_001

TITLES = {
    ReceiptType.PLAN_PURCHASE: "Plan purchase receipt",
    ReceiptType.BOOKING_CONFIRMATION: "Booking confirmation",
    ReceiptType.DELIVERY_MERCHANT: "Delivery receipt (merchant copy)",
    ReceiptType.DELIVERY_CUSTOMER: "Delivery receipt",
    ReceiptType.DELIVERY_RIDER: "Delivery receipt (rider copy)",
    ReceiptType.FAILED_DELIVERY_MERCHANT: "Failed delivery notice",
    ReceiptType.CASH_HANDOVER: "Cash handover receipt",
    ReceiptType.COD_PAYOUT: "Cash-on-delivery payout receipt",
    ReceiptType.RIDER_PAY: "Rider pay receipt",
    ReceiptType.VOID: "Void notice",
}


class ReceiptError(Exception):
    pass


@dataclass(frozen=True)
class Recipient:
    role: str  # merchant | customer | rider | dispatcher
    phone: str | None
    name: str


@dataclass(frozen=True)
class Links:
    """What the receipt is about. Exactly one is set (none for rider pay)."""

    order_id: int | None = None
    plan_request_id: int | None = None
    handover_id: int | None = None
    payout_id: int | None = None


@dataclass(frozen=True)
class ChainBreak:
    number: str
    reason: str


def ugx(amount: int) -> str:
    return f"UGX {amount:,}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(
    prev_hash: str, *, number: str, type: str, amount_ugx: int | None, issued_at: datetime, snapshot: dict
) -> str:
    payload = canonical_json(
        {
            "amount_ugx": amount_ugx,
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "number": number,
            "snapshot": snapshot,
            "type": str(type),
        }
    )
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


def _next_number(last: Receipt | None) -> str:
    return f"RCT-{(int(last.number.removeprefix('RCT-')) if last else 0) + 1:06d}"


def issue_receipt(
    session: Session,
    type: str,
    recipient: Recipient,
    links: Links,
    amount_ugx: int | None,
    snapshot: dict[str, Any],
    *,
    at: datetime | None = None,
    efris_fdn: str | None = None,
    efris_verification_code: str | None = None,
    voids_receipt_id: int | None = None,
) -> Receipt:
    """The only way to create a receipt.

    `snapshot` is the content to print: {"lines": [[label, value], ...], "notes": [text, ...]}.
    The company details, recipient, links and EFRIS numbers are frozen into it here, so the
    stored snapshot is everything on the receipt and all of it is covered by the hash.

    The number is taken from the last receipt while holding a transaction-level lock, not
    from a Postgres sequence: a sequence would leave a gap whenever a transaction rolled back.
    """
    settings = get_settings()
    issued_at = at or utcnow()

    session.flush()
    session.execute(select(func.pg_advisory_xact_lock(_CHAIN_LOCK_KEY)))
    last = session.execute(select(Receipt).order_by(Receipt.id.desc()).limit(1)).scalar_one_or_none()
    number = _next_number(last)
    prev_hash = last.hash if last else GENESIS_HASH

    frozen = {
        "title": TITLES[ReceiptType(type)],
        "company": {
            "name": settings.company_name,
            "tin": settings.company_tin,
            "address": settings.company_address,
            "phone": settings.company_phone,
        },
        "recipient": {"role": recipient.role, "name": recipient.name, "phone": recipient.phone},
        "links": {k: v for k, v in vars(links).items() if v is not None},
        "lines": [[str(label), str(value)] for label, value in snapshot.get("lines", [])],
        "notes": [str(note) for note in snapshot.get("notes", [])],
    }
    if efris_fdn or efris_verification_code:
        frozen["efris"] = {"fdn": efris_fdn, "verification_code": efris_verification_code}
    # Round-trip through JSON so what we hash is exactly what Postgres will hand back.
    frozen = json.loads(canonical_json(frozen))

    receipt = Receipt(
        number=number,
        type=str(type),
        recipient_role=recipient.role,
        recipient_phone=recipient.phone,
        order_id=links.order_id,
        plan_request_id=links.plan_request_id,
        handover_id=links.handover_id,
        payout_id=links.payout_id,
        amount_ugx=amount_ugx,
        snapshot=frozen,
        efris_fdn=efris_fdn,
        efris_verification_code=efris_verification_code,
        voids_receipt_id=voids_receipt_id,
        view_token=secrets.token_urlsafe(24),
        prev_hash=prev_hash,
        hash=compute_hash(
            prev_hash, number=number, type=type, amount_ugx=amount_ugx, issued_at=issued_at, snapshot=frozen
        ),
        issued_at=issued_at,
    )
    session.add(receipt)
    session.flush()
    return receipt


def verify_chain(session: Session) -> ChainBreak | None:
    """Walk every receipt in order. Returns the first broken link, or None if the chain is whole."""
    prev_hash = GENESIS_HASH
    expected = 1
    rows = session.execute(select(Receipt).order_by(Receipt.id).execution_options(populate_existing=True))
    for receipt in rows.scalars():
        if receipt.number != f"RCT-{expected:06d}":
            return ChainBreak(receipt.number, f"expected number RCT-{expected:06d}: a receipt is missing")
        if receipt.prev_hash != prev_hash:
            return ChainBreak(receipt.number, "prev_hash does not match the previous receipt")
        recomputed = compute_hash(
            receipt.prev_hash,
            number=receipt.number,
            type=receipt.type,
            amount_ugx=receipt.amount_ugx,
            issued_at=receipt.issued_at,
            snapshot=receipt.snapshot,
        )
        if not hmac.compare_digest(recomputed, receipt.hash):
            return ChainBreak(receipt.number, "contents do not match the stored hash")
        prev_hash = receipt.hash
        expected += 1
    return None


def mark_sent(session: Session, receipt: Receipt, at: datetime | None = None) -> None:
    """Record that the receipt was sent. Allowed once; the trigger rejects any other update."""
    if receipt.sent_at is None:
        receipt.sent_at = at or utcnow()
        session.flush()


def void_receipt(session: Session, receipt: Receipt, reason: str, admin_id: str, at: datetime | None = None) -> Receipt:
    """Correct a receipt by issuing a void notice. The original stays exactly as it was."""
    reason = (reason or "").strip()
    if not reason:
        raise ReceiptError("a reason is required to void a receipt")
    if receipt.type == ReceiptType.VOID:
        raise ReceiptError("a void notice can't be voided")
    already = session.execute(select(Receipt.number).where(Receipt.voids_receipt_id == receipt.id)).scalar()
    if already:
        raise ReceiptError(f"{receipt.number} was already voided by {already}")
    original = receipt.snapshot
    return issue_receipt(
        session,
        ReceiptType.VOID,
        Recipient(receipt.recipient_role, receipt.recipient_phone, original["recipient"]["name"]),
        Links(receipt.order_id, receipt.plan_request_id, receipt.handover_id, receipt.payout_id),
        receipt.amount_ugx,
        {
            "lines": [
                ["Voids receipt", receipt.number],
                ["Original receipt", original["title"]],
                ["Reason", reason],
                ["Voided by", str(admin_id)],
            ],
            "notes": [f"Receipt {receipt.number} is void and must not be relied on."],
        },
        at=at,
        voids_receipt_id=receipt.id,
    )


# --- Receipts issued by the order state machine ---------------------------------------


def _order_lines(order: Order, merchant: Merchant) -> list[list[str]]:
    return [["Order", order.code], ["Shop", merchant.shop_name], ["Package", order.package]]


def _cash_line(order: Order) -> list[str]:
    return ["Cash on delivery", ugx(order.cod_ugx) if order.cod_ugx else "None"]


def issue_for_transition(
    session: Session, order: Order, to_status: str, at: datetime, data: dict[str, Any]
) -> list[Receipt]:
    """Issue the receipts that go with a status change: BOOKED, DELIVERED or FAILED."""
    if to_status not in (OrderStatus.BOOKED, OrderStatus.DELIVERED, OrderStatus.FAILED):
        return []
    merchant = session.get(Merchant, order.merchant_id)
    to_merchant = Recipient("merchant", merchant.phone, merchant.shop_name)
    links = Links(order_id=order.id)
    when = format_kampala(at)

    if to_status == OrderStatus.BOOKED:
        lines = _order_lines(order, merchant) + [
            ["Customer", f"{order.customer_name}, {order.customer_phone}"],
            *([_cash_line(order)] if order.cod_ugx else []),
            ["Booked", when],
            ["Drops left on plan", str(merchant.drops_left)],
        ]
        content = {"lines": lines}
        return [issue_receipt(session, ReceiptType.BOOKING_CONFIRMATION, to_merchant, links, None, content, at=at)]

    rider = session.get(Rider, order.rider_id)

    if to_status == OrderStatus.FAILED:
        lines = _order_lines(order, merchant) + [
            ["Customer", f"{order.customer_name}, {order.customer_phone}"],
            ["Rider", rider.name],
            ["Reason", str(data.get("reason", ""))],
            ["Attempted", when],
        ]
        notes = ["The parcel was not delivered. This drop stays used; a reschedule does not use another."]
        return [
            issue_receipt(
                session,
                ReceiptType.FAILED_DELIVERY_MERCHANT,
                to_merchant,
                links,
                None,
                {"lines": lines, "notes": notes},
                at=at,
            )
        ]

    cod = order.cod_ugx
    amount = cod if cod else None
    merchant_copy = issue_receipt(
        session,
        ReceiptType.DELIVERY_MERCHANT,
        to_merchant,
        links,
        amount,
        {
            "lines": _order_lines(order, merchant)
            + [
                ["Customer", f"{order.customer_name}, {order.customer_phone}"],
                ["Rider", rider.name],
                ["Delivered", when],
                *([["Cash collected for you", ugx(cod)]] if cod else []),
            ],
            "notes": [f"The rider collected {ugx(cod)} on your behalf. Men-Ions pays it out to you."] if cod else [],
        },
        at=at,
    )
    customer_copy = issue_receipt(
        session,
        ReceiptType.DELIVERY_CUSTOMER,
        Recipient("customer", order.customer_phone, order.customer_name),
        links,
        amount,
        {
            "lines": _order_lines(order, merchant)
            + [
                ["Delivered", when],
                *([["Cash paid to rider", f"{ugx(cod)}, collected on behalf of {merchant.shop_name}"]] if cod else []),
            ],
            "notes": [
                f"{get_settings().company_name} delivered this parcel for {merchant.shop_name}."
                + (f" The cash was collected on behalf of {merchant.shop_name}." if cod else ""),
                f"This is not a tax receipt for the goods. Ask {merchant.shop_name} for one.",
            ],
        },
        at=at,
    )
    rider_copy = issue_receipt(
        session,
        ReceiptType.DELIVERY_RIDER,
        Recipient("rider", rider.phone, rider.name),
        links,
        amount,
        {
            "lines": _order_lines(order, merchant)
            + [["Delivered", when], *([["Cash you collected", ugx(cod)]] if cod else [])],
            "notes": ["Hand this cash to the dispatcher at the end of your shift."] if cod else [],
        },
        at=at,
    )
    return [merchant_copy, customer_copy, rider_copy]


def issue_plan_purchase(
    session: Session,
    *,
    merchant: Merchant,
    request: PlanRequest,
    plan_name: str,
    drops: int,
    valid_until: datetime,
    paid_with: str,
    efris_fdn: str,
    efris_verification_code: str,
    at: datetime,
) -> Receipt:
    """Issued once URA's FDN is known, which can be a little after the plan started (so the
    dates come from the request, not from the merchant's current plan)."""
    lines = [
        ["Plan", plan_name],
        ["Drops", str(drops)],
        ["Paid", format_kampala(request.approved_at)],
        ["Paid with", paid_with],
        ["Payment reference", request.momo_reference],
        ["Plan valid from", format_kampala(request.approved_at)],
        ["Plan valid until", format_kampala(valid_until)],
    ]
    if merchant.tin:
        lines.append(["Buyer TIN", merchant.tin])
    lines += [
        ["EFRIS FDN", efris_fdn],
        ["EFRIS verification code", efris_verification_code],
    ]
    return issue_receipt(
        session,
        ReceiptType.PLAN_PURCHASE,
        Recipient("merchant", merchant.phone, merchant.shop_name),
        Links(plan_request_id=request.id),
        request.amount_ugx,
        {"lines": lines},
        at=at,
        efris_fdn=efris_fdn,
        efris_verification_code=efris_verification_code,
    )


# --- Cash handovers, payouts and rider pay --------------------------------------------


def expected_cash(session: Session, rider_id: int, day: date) -> int:
    """Cash this rider collected: the cod_ugx of their orders DELIVERED on this Kampala day."""
    start, end = kampala_day_bounds(day)
    total = session.execute(
        select(func.coalesce(func.sum(Order.cod_ugx), 0))
        .join(OrderEvent, OrderEvent.order_id == Order.id)
        .where(
            Order.rider_id == rider_id,
            Order.status == OrderStatus.DELIVERED,
            OrderEvent.kind == EventKind.TRANSITION,
            OrderEvent.to_status == OrderStatus.DELIVERED,
            OrderEvent.at >= start,
            OrderEvent.at < end,
        )
    ).scalar_one()
    return int(total)


def _handover_receipts(session: Session, handover_id: int) -> list[Receipt]:
    return list(
        session.execute(
            select(Receipt)
            .where(Receipt.handover_id == handover_id, Receipt.type == ReceiptType.CASH_HANDOVER)
            .order_by(Receipt.id)
        ).scalars()
    )


def _issue_handover_receipts(session: Session, handover: CashHandover, rider: Rider, at: datetime) -> list[Receipt]:
    if not (handover.rider_confirmed_at and handover.dispatcher_confirmed_at):
        return []
    if _handover_receipts(session, handover.id):
        return []
    lines = [
        ["Rider", rider.name],
        ["Date", handover.date.strftime("%d %b %Y")],
        ["Cash expected", ugx(handover.expected_ugx)],
        ["Cash received", ugx(handover.received_ugx)],
        ["Difference", ugx(handover.difference_ugx)],
        ["Dispatcher", str(handover.dispatcher_id)],
        ["Rider confirmed", format_kampala(handover.rider_confirmed_at)],
        ["Dispatcher confirmed", format_kampala(handover.dispatcher_confirmed_at)],
    ]
    notes = [handover.notes] if handover.notes else []
    links = Links(handover_id=handover.id)
    content = {"lines": lines, "notes": notes}
    return [
        issue_receipt(session, ReceiptType.CASH_HANDOVER, Recipient("rider", rider.phone, rider.name),
                      links, handover.received_ugx, content, at=at),
        issue_receipt(session, ReceiptType.CASH_HANDOVER, Recipient("dispatcher", None, str(handover.dispatcher_id)),
                      links, handover.received_ugx, content, at=at),
    ]  # fmt: skip


def record_cash_handover(
    session: Session,
    rider: Rider,
    day: date,
    received_ugx: int,
    dispatcher_id: str,
    *,
    notes: str | None = None,
    at: datetime | None = None,
) -> CashHandover:
    """The dispatcher records the cash a rider handed in for a day. The rider then confirms
    with confirm_cash_handover(); once both have confirmed, each gets a receipt.

    Until the rider confirms, the dispatcher may record again to fix a typo. After that the
    receipts exist and a correction means voiding them. A difference between expected and
    received cash records an admin alert.
    """
    if not isinstance(received_ugx, int) or isinstance(received_ugx, bool) or received_ugx < 0:
        raise ReceiptError("received_ugx must be a whole number of UGX, zero or more")
    at = at or utcnow()
    session.flush()
    handover = session.execute(
        select(CashHandover)
        .where(CashHandover.rider_id == rider.id, CashHandover.date == day)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if handover is not None and _handover_receipts(session, handover.id):
        raise ReceiptError("this handover already has receipts; void them to correct it")

    expected = expected_cash(session, rider.id, day)
    if handover is None:
        handover = CashHandover(rider_id=rider.id, date=day)
        session.add(handover)
    handover.expected_ugx = expected
    handover.received_ugx = received_ugx
    handover.difference_ugx = received_ugx - expected
    handover.dispatcher_confirmed_at = at
    handover.dispatcher_id = str(dispatcher_id)
    handover.notes = notes
    session.flush()

    if handover.difference_ugx != 0:
        session.add(
            AdminAlert(
                kind="cash_handover_difference",
                rider_id=rider.id,
                at=at,
                data={
                    "handover_id": handover.id,
                    "date": day.isoformat(),
                    "expected_ugx": expected,
                    "received_ugx": received_ugx,
                    "difference_ugx": handover.difference_ugx,
                },
            )
        )
        session.flush()
    _issue_handover_receipts(session, handover, rider, at)
    return handover


def confirm_cash_handover(session: Session, handover: CashHandover, at: datetime | None = None) -> list[Receipt]:
    """The rider agrees with the recorded amount. Returns the two receipts (rider's, dispatcher's)."""
    at = at or utcnow()
    session.flush()
    handover = session.execute(
        select(CashHandover)
        .where(CashHandover.id == handover.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if handover.rider_confirmed_at is None:
        handover.rider_confirmed_at = at
        session.flush()
    rider = session.get(Rider, handover.rider_id)
    _issue_handover_receipts(session, handover, rider, at)
    return _handover_receipts(session, handover.id)


def _positive_ugx(amount: int) -> None:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ReceiptError("amount_ugx must be a whole number of UGX above zero")


def record_cod_payout(
    session: Session,
    merchant: Merchant,
    day: date,
    amount_ugx: int,
    momo_reference: str,
    paid_by: str,
    *,
    at: datetime | None = None,
) -> tuple[CodPayout, Receipt]:
    """Record that Men-Ions sent a merchant the cash collected for them, and issue the receipt.
    The Mobile Money reference is unique, so the same transfer can't be recorded twice."""
    _positive_ugx(amount_ugx)
    momo_reference = (momo_reference or "").strip()
    if not momo_reference:
        raise ReceiptError("the Mobile Money reference is required")
    at = at or utcnow()
    if session.execute(select(CodPayout.id).where(CodPayout.momo_reference == momo_reference)).scalar():
        raise ReceiptError(f"a payout with reference {momo_reference} is already recorded")
    payout = CodPayout(
        merchant_id=merchant.id, date=day, amount_ugx=amount_ugx,
        momo_reference=momo_reference, paid_by=str(paid_by), paid_at=at,
    )  # fmt: skip
    session.add(payout)
    session.flush()
    receipt = issue_receipt(
        session,
        ReceiptType.COD_PAYOUT,
        Recipient("merchant", merchant.phone, merchant.shop_name),
        Links(payout_id=payout.id),
        amount_ugx,
        {
            "lines": [
                ["Shop", merchant.shop_name],
                ["Cash collected on", day.strftime("%d %b %Y")],
                ["Paid to", merchant.phone],
                ["Mobile Money reference", momo_reference],
                ["Paid by", str(paid_by)],
                ["Paid", format_kampala(at)],
            ],
            "notes": ["Cash collected from your customers on your behalf, paid out to you."],
        },
        at=at,
    )
    return payout, receipt


def record_rider_pay(
    session: Session,
    rider: Rider,
    day: date,
    amount_ugx: int,
    paid_by: str,
    *,
    momo_reference: str | None = None,
    at: datetime | None = None,
) -> Receipt:
    """Issue a rider's pay receipt. There is no pay table yet: the receipt is the record."""
    _positive_ugx(amount_ugx)
    at = at or utcnow()
    return issue_receipt(
        session,
        ReceiptType.RIDER_PAY,
        Recipient("rider", rider.phone, rider.name),
        Links(),
        amount_ugx,
        {
            "lines": [
                ["Rider", rider.name],
                ["For work on", day.strftime("%d %b %Y")],
                ["Mobile Money reference", momo_reference or "Paid in cash"],
                ["Paid by", str(paid_by)],
                ["Paid", format_kampala(at)],
            ],
        },
        at=at,
    )


# --- Receipt page ---------------------------------------------------------------------

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/r/{number}", response_class=HTMLResponse)
def view_receipt(
    request: Request,
    number: str,
    k: str = Query(default=""),
    session: Session = Depends(get_session),
):
    """A plain, printable receipt. A wrong or missing token gives 404, the same as no receipt."""
    receipt = session.execute(select(Receipt).where(Receipt.number == number)).scalar_one_or_none()
    if receipt is None or not k or not hmac.compare_digest(k.encode(), receipt.view_token.encode()):
        raise HTTPException(status_code=404, detail="Not found")
    voided_by = session.execute(select(Receipt.number).where(Receipt.voids_receipt_id == receipt.id)).scalar()
    return _templates.TemplateResponse(
        request,
        "receipt.html",
        {
            "receipt": receipt,
            "snapshot": receipt.snapshot,
            "issued": format_kampala(receipt.issued_at),
            "amount": ugx(receipt.amount_ugx) if receipt.amount_ugx is not None else None,
            "voided_by": voided_by,
        },
        headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"},
    )
