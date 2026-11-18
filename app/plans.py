"""Plan catalogue, drop accounting and plan purchase requests."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import receipts
from app.clock import KAMPALA, utcnow
from app.models import (
    PLAN_KEYS,
    Merchant,
    MerchantStatus,
    Order,
    PlanRequest,
    PlanRequestStatus,
    Receipt,
    plan_request_ref_seq,
)


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_ugx: int
    drops: int
    period: str  # "day" | "week" | "month"


CATALOGUE: dict[str, Plan] = {
    p.key: p
    for p in (
        Plan("daily_bronze", "Daily Bronze", 5_000, 2, "day"),
        Plan("daily_silver", "Daily Silver", 7_000, 3, "day"),
        Plan("daily_gold", "Daily Gold", 9_000, 4, "day"),
        Plan("weekly_bronze", "Weekly Bronze", 29_000, 15, "week"),
        Plan("weekly_silver", "Weekly Silver", 32_000, 17, "week"),
        Plan("weekly_gold", "Weekly Gold", 35_000, 20, "week"),
        Plan("monthly", "Monthly", 91_000, 53, "month"),
    )
}
assert tuple(CATALOGUE) == PLAN_KEYS, "plans.CATALOGUE and models.PLAN_KEYS must list the same plans"

# One step above the stated volume, to steer merchants up. Change it here only.
RECOMMENDED_PLAN = {"1-2": "weekly_bronze", "3-5": "weekly_gold", "6+": "monthly"}

PRIORITY_RIDER_PLANS = frozenset({"weekly_gold", "monthly"})


class PlanError(Exception):
    pass


def get_plan(plan_key: str) -> Plan:
    try:
        return CATALOGUE[plan_key]
    except KeyError:
        raise PlanError(f"unknown plan: {plan_key!r}") from None


def plan_end(plan: Plan, at: datetime) -> datetime:
    """When a plan bought at `at` stops (exclusive)."""
    local = at.astimezone(KAMPALA)
    if plan.period == "day":
        # A full 24 hours from purchase, not midnight: drops left unused in the evening can
        # still be booked first thing the next morning. Nobody loses drops overnight.
        end = local + timedelta(hours=24)
    elif plan.period == "week":
        end = local + timedelta(days=7)
    else:
        # One calendar month: same day and time next month, clamped to the month's last day
        # (bought 31 Jan -> ends 28 or 29 Feb).
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        day = min(local.day, calendar.monthrange(year, month)[1])
        end = local.replace(year=year, month=month, day=day)
    return end.astimezone(UTC)


def activate_plan(merchant: Merchant, plan_key: str, at: datetime) -> None:
    """Start a plan now. It replaces any current plan; unused drops expire."""
    plan = get_plan(plan_key)
    merchant.plan = plan.key
    merchant.plan_starts = at
    merchant.plan_ends = plan_end(plan, at)
    merchant.drops_left = plan.drops


def drop_refusal(merchant: Merchant, at: datetime) -> str | None:
    """Why this merchant can't use a drop right now, or None if they can."""
    if merchant.plan is None or merchant.plan_starts is None or merchant.plan_ends is None:
        return "no_plan"
    if not (merchant.plan_starts <= at < merchant.plan_ends):
        return "plan_expired"
    if not merchant.drops_left or merchant.drops_left <= 0:
        return "no_drops_left"
    return None


def has_drop(merchant: Merchant, at: datetime) -> bool:
    return drop_refusal(merchant, at) is None


def use_drop(merchant: Merchant, order: Order) -> bool:
    """Take one drop for this order. Called only by the order state machine, with the
    merchant row locked. Does nothing if the order already holds a drop."""
    if order.drop_used:
        return False
    if not merchant.drops_left or merchant.drops_left <= 0:
        raise PlanError("no drops left")
    merchant.drops_left -= 1
    order.drop_used = True
    return True


def return_drop(merchant: Merchant, order: Order, at: datetime) -> bool:
    """Give this order's drop back. Called only by the order state machine, with the
    merchant row locked. Does nothing if the order holds no drop.

    The drop goes back only to the plan it came from: if that plan has ended or been
    replaced since the booking, the drop expired with it. Returns True if drops_left grew.
    """
    if not order.drop_used:
        return False
    order.drop_used = False
    same_plan = (
        merchant.plan is not None
        and merchant.plan_starts is not None
        and merchant.plan_ends is not None
        and merchant.plan_starts <= order.created_at
        and at < merchant.plan_ends
    )
    if same_plan:
        merchant.drops_left = (merchant.drops_left or 0) + 1
    return same_plan


def recommend_plan(expected_daily_drops: str) -> str:
    try:
        return RECOMMENDED_PLAN[expected_daily_drops]
    except KeyError:
        raise PlanError(f"unknown volume: {expected_daily_drops!r}") from None


def priority_riders(merchant: Merchant) -> list[int]:
    if merchant.plan in PRIORITY_RIDER_PLANS:
        return list(merchant.priority_rider_ids or [])
    return []


# --- Plan purchase requests ---------------------------------------------------------


def _momo_reference(session: Session, merchant: Merchant) -> str:
    """e.g. MNI-KISEME-014: what the merchant types as the MoMo payment reason."""
    slug = re.sub(r"[^A-Z0-9]", "", merchant.shop_name.upper())[:6] or "SHOP"
    number = session.execute(select(plan_request_ref_seq.next_value())).scalar_one()
    return f"MNI-{slug}-{number:03d}"


def create_plan_request(
    session: Session, merchant: Merchant, plan_key: str, at: datetime | None = None
) -> PlanRequest:
    plan = get_plan(plan_key)
    request = PlanRequest(
        merchant_id=merchant.id,
        plan=plan.key,
        amount_ugx=plan.price_ugx,
        momo_reference=_momo_reference(session, merchant),
        status=PlanRequestStatus.AWAITING_PAYMENT,
        created_at=at or utcnow(),
    )
    session.add(request)
    session.flush()
    return request


def _lock_request(session: Session, request: PlanRequest) -> PlanRequest:
    session.flush()
    return session.execute(
        select(PlanRequest)
        .where(PlanRequest.id == request.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def claim_payment(session: Session, request: PlanRequest, at: datetime | None = None) -> None:
    """The merchant says they have paid. Safe to call twice."""
    request = _lock_request(session, request)
    if request.status == PlanRequestStatus.PAYMENT_CLAIMED:
        return
    if request.status != PlanRequestStatus.AWAITING_PAYMENT:
        raise PlanError(f"request is {request.status}; payment can't be claimed")
    request.status = PlanRequestStatus.PAYMENT_CLAIMED
    request.paid_claimed_at = at or utcnow()
    session.flush()


def approve_plan_request(
    session: Session,
    request: PlanRequest,
    admin_id: str,
    at: datetime | None = None,
    *,
    efris_fdn: str,
    efris_verification_code: str,
) -> Receipt | None:
    """An admin confirms the money arrived. In one transaction: the request becomes APPROVED,
    the plan starts, a PENDING merchant becomes ACTIVE, and the plan_purchase receipt is issued.

    Returns the receipt, or None if the request was already approved (nothing changes then).

    TODO(efris-api): the dispatcher issues the EFRIS receipt on URA's portal and types the
    FDN and verification code in. Automate this through the EFRIS API later.
    """
    at = at or utcnow()
    with session.begin_nested():
        request = _lock_request(session, request)
        if request.status == PlanRequestStatus.APPROVED:
            return None
        if request.status not in (PlanRequestStatus.PAYMENT_CLAIMED, PlanRequestStatus.AWAITING_PAYMENT):
            raise PlanError(f"request is {request.status}; it can't be approved")
        efris_fdn = (efris_fdn or "").strip()
        efris_verification_code = (efris_verification_code or "").strip()
        if not efris_fdn or not efris_verification_code:
            raise PlanError("the EFRIS FDN and verification code are required to approve a plan purchase")

        merchant = session.execute(
            select(Merchant)
            .where(Merchant.id == request.merchant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

        request.status = PlanRequestStatus.APPROVED
        request.approved_by = str(admin_id)
        request.approved_at = at
        activate_plan(merchant, request.plan, at)
        if merchant.status == MerchantStatus.PENDING:
            merchant.status = MerchantStatus.ACTIVE
        session.flush()

        plan = get_plan(request.plan)
        return receipts.issue_plan_purchase(
            session,
            merchant=merchant,
            request=request,
            plan_name=plan.name,
            drops=plan.drops,
            efris_fdn=efris_fdn,
            efris_verification_code=efris_verification_code,
            at=at,
        )


def reject_plan_request(
    session: Session, request: PlanRequest, admin_id: str, at: datetime | None = None
) -> None:
    request = _lock_request(session, request)
    if request.status == PlanRequestStatus.REJECTED:
        return
    if request.status == PlanRequestStatus.APPROVED:
        raise PlanError("request is already approved; void the receipt instead")
    request.status = PlanRequestStatus.REJECTED
    request.approved_by = str(admin_id)
    request.approved_at = at or utcnow()
    session.flush()
