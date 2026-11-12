from datetime import UTC, datetime, timedelta

import pytest

from app import plans
from app.clock import KAMPALA
from app.models import MerchantStatus, PlanRequest, PlanRequestStatus, Receipt
from tests.conftest import T0, make_merchant


def kampala(*args) -> datetime:
    return datetime(*args, tzinfo=KAMPALA)


def test_catalogue_matches_the_brief():
    got = {key: (p.price_ugx, p.drops, p.period) for key, p in plans.CATALOGUE.items()}
    assert got == {
        "daily_bronze": (5_000, 2, "day"),
        "daily_silver": (7_000, 3, "day"),
        "daily_gold": (9_000, 4, "day"),
        "weekly_bronze": (29_000, 15, "week"),
        "weekly_silver": (32_000, 17, "week"),
        "weekly_gold": (35_000, 20, "week"),
        "monthly": (91_000, 53, "month"),
    }


def test_daily_plan_ends_at_kampala_midnight(session):
    bought = kampala(2026, 11, 10, 8, 30)
    merchant = make_merchant(session, plan="daily_silver", at=bought)
    assert merchant.drops_left == 3
    assert merchant.plan_ends == kampala(2026, 11, 11, 0, 0)
    assert plans.has_drop(merchant, kampala(2026, 11, 10, 23, 59))
    assert not plans.has_drop(merchant, kampala(2026, 11, 11, 0, 0))
    assert plans.drop_refusal(merchant, kampala(2026, 11, 11, 0, 0)) == "plan_expired"
    # 23:59 Kampala is 20:59 UTC the same day: the UTC date must not decide it.
    assert plans.has_drop(merchant, datetime(2026, 11, 10, 20, 59, tzinfo=UTC))
    assert not plans.has_drop(merchant, datetime(2026, 11, 10, 21, 0, tzinfo=UTC))


def test_daily_plan_bought_just_before_midnight_still_ends_at_midnight(session):
    merchant = make_merchant(session, plan="daily_gold", at=kampala(2026, 11, 10, 23, 59))
    assert merchant.plan_ends == kampala(2026, 11, 11, 0, 0)


def test_weekly_is_seven_days_and_monthly_is_a_calendar_month():
    week = plans.plan_end(plans.get_plan("weekly_gold"), T0)
    assert week == T0 + timedelta(days=7)
    month = plans.get_plan("monthly")
    assert plans.plan_end(month, kampala(2026, 11, 10, 12, 0)) == kampala(2026, 12, 10, 12, 0)
    assert plans.plan_end(month, kampala(2026, 12, 15, 9, 0)) == kampala(2027, 1, 15, 9, 0)
    assert plans.plan_end(month, kampala(2027, 1, 31, 9, 0)) == kampala(2027, 2, 28, 9, 0)


def test_a_new_plan_replaces_the_old_one_and_unused_drops_expire(session):
    merchant = make_merchant(session, plan="weekly_gold")
    merchant.drops_left = 12
    plans.activate_plan(merchant, "daily_bronze", T0 + timedelta(days=1))
    assert (merchant.plan, merchant.drops_left) == ("daily_bronze", 2)


def test_has_drop_needs_a_plan_time_left_and_drops(session):
    assert plans.drop_refusal(make_merchant(session, plan=None), T0) == "no_plan"
    merchant = make_merchant(session, plan="weekly_bronze")
    assert plans.has_drop(merchant, T0)
    merchant.drops_left = 0
    assert plans.drop_refusal(merchant, T0) == "no_drops_left"


def test_recommend_plan_steers_one_step_up():
    assert plans.recommend_plan("1-2") == "weekly_bronze"
    assert plans.recommend_plan("3-5") == "weekly_gold"
    assert plans.recommend_plan("6+") == "monthly"
    with pytest.raises(plans.PlanError):
        plans.recommend_plan("lots")


def test_priority_riders_only_on_weekly_gold_and_monthly(session):
    for plan, expected in [("weekly_gold", [4, 9]), ("monthly", [4, 9]), ("weekly_silver", []), (None, [])]:
        merchant = make_merchant(session, plan=plan, priority_rider_ids=[4, 9])
        assert plans.priority_riders(merchant) == expected


def test_plan_request_carries_the_catalogue_price_and_a_unique_reference(session):
    merchant = make_merchant(session, plan=None, status=MerchantStatus.PENDING, shop_name="Kisementi Threads")
    requests = [plans.create_plan_request(session, merchant, "weekly_gold", T0) for _ in range(5)]
    assert all(r.amount_ugx == 35_000 and r.status == PlanRequestStatus.AWAITING_PAYMENT for r in requests)
    references = [r.momo_reference for r in requests]
    assert len(set(references)) == 5
    assert all(ref.startswith("MNI-KISEME-") for ref in references)
    with pytest.raises(plans.PlanError):
        plans.create_plan_request(session, merchant, "platinum", T0)


def test_approve_activates_plan_and_merchant_once(session):
    merchant = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    request = plans.create_plan_request(session, merchant, "weekly_gold", T0)
    plans.claim_payment(session, request, T0)
    plans.claim_payment(session, request, T0 + timedelta(minutes=1))  # twice is harmless
    assert request.status == PlanRequestStatus.PAYMENT_CLAIMED and request.paid_claimed_at == T0

    approved_at = T0 + timedelta(minutes=5)
    receipt = plans.approve_plan_request(
        session, request, "admin-1", approved_at, efris_fdn="FDN123", efris_verification_code="VC456"
    )
    assert request.status == PlanRequestStatus.APPROVED and request.approved_by == "admin-1"
    assert merchant.status == MerchantStatus.ACTIVE
    assert (merchant.plan, merchant.drops_left, merchant.plan_starts) == ("weekly_gold", 20, approved_at)
    assert receipt.type == "plan_purchase" and receipt.amount_ugx == 35_000
    assert (receipt.efris_fdn, receipt.efris_verification_code) == ("FDN123", "VC456")

    # Approving again changes nothing: no second receipt, no reset of used drops or dates.
    merchant.drops_left = 14
    again = plans.approve_plan_request(
        session, request, "admin-2", approved_at + timedelta(hours=3), efris_fdn="X", efris_verification_code="Y"
    )
    assert again is None
    assert (merchant.drops_left, merchant.plan_starts, request.approved_by) == (14, approved_at, "admin-1")
    assert session.query(Receipt).filter_by(plan_request_id=request.id).count() == 1


def test_admin_can_approve_before_the_merchant_claims(session):
    merchant = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    request = plans.create_plan_request(session, merchant, "daily_gold", T0)
    assert plans.approve_plan_request(session, request, "a", T0, efris_fdn="F", efris_verification_code="V")
    assert merchant.status == MerchantStatus.ACTIVE


def test_approval_without_efris_numbers_is_refused_and_changes_nothing(session):
    merchant = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    request = plans.create_plan_request(session, merchant, "weekly_gold", T0)
    for fdn, code in [("", "VC"), ("FDN", ""), ("  ", "  "), (None, None)]:
        with pytest.raises(plans.PlanError, match="EFRIS"):
            plans.approve_plan_request(session, request, "a", T0, efris_fdn=fdn, efris_verification_code=code)
    session.refresh(request)
    assert request.status == PlanRequestStatus.AWAITING_PAYMENT
    assert merchant.status == MerchantStatus.PENDING and merchant.plan is None
    assert session.query(Receipt).count() == 0


def test_approving_does_not_unsuspend_a_merchant(session):
    merchant = make_merchant(session, plan=None, status=MerchantStatus.SUSPENDED)
    request = plans.create_plan_request(session, merchant, "weekly_gold", T0)
    plans.approve_plan_request(session, request, "a", T0, efris_fdn="F", efris_verification_code="V")
    assert merchant.status == MerchantStatus.SUSPENDED


def test_reject_and_its_limits(session):
    merchant = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    request = plans.create_plan_request(session, merchant, "weekly_gold", T0)
    plans.reject_plan_request(session, request, "a", T0)
    assert request.status == PlanRequestStatus.REJECTED
    with pytest.raises(plans.PlanError):
        plans.approve_plan_request(session, request, "a", T0, efris_fdn="F", efris_verification_code="V")
    with pytest.raises(plans.PlanError):
        plans.claim_payment(session, request, T0)
    assert merchant.plan is None and session.get(PlanRequest, request.id).approved_by == "a"
