import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import orders, plans
from app.models import (
    Actor,
    AdminAlert,
    EventKind,
    Merchant,
    MerchantStatus,
    Order,
    OrderEvent,
    OrderStatus,
    Receipt,
    Rider,
)
from app.orders import DropCodeRejected, InvalidTransition, TransitionConditionFailed, transition
from tests.conftest import T0, add_photo, advance, book, make_merchant, make_rider, phone

S = OrderStatus


def events(session, order, kind=EventKind.TRANSITION):
    rows = session.execute(
        select(OrderEvent).where(OrderEvent.order_id == order.id, OrderEvent.kind == kind).order_by(OrderEvent.id)
    )
    return list(rows.scalars())


def receipt_types(session, order):
    rows = session.execute(select(Receipt.type).where(Receipt.order_id == order.id).order_by(Receipt.id))
    return list(rows.scalars())


# --- Allowed transitions ---------------------------------------------------------------


def test_happy_path_writes_one_event_per_transition(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = book(session, merchant, cod_ugx=85_000)
    assert order.code.startswith("MNI-") and len(order.drop_code) == 4 and order.drop_code.isdigit()
    assert [(e.from_status, e.to_status, e.actor) for e in events(session, order)] == [(None, "BOOKED", "merchant")]

    order.drop_lat, order.drop_lng = 0.3476, 32.5825
    steps = [
        (S.READY, "customer", None, None),
        (S.ASSIGNED, "rider", rider.id, None),
        (S.PICKED_UP, "rider", rider.id, None),
        (S.DELIVERED, "rider", rider.id, {"drop_code": order.drop_code}),
    ]
    for count, (to, actor, actor_id, data) in enumerate(steps, start=2):
        if to == S.PICKED_UP:
            add_photo(session, order, "pickup")
        if to == S.DELIVERED:
            add_photo(session, order, "delivery")
        before = order.status
        transition(session, order, to, actor, actor_id, data, at=T0 + timedelta(minutes=count))
        rows = events(session, order)
        assert len(rows) == count
        assert (rows[-1].from_status, rows[-1].to_status, rows[-1].actor) == (before, to, actor)
    assert order.status == S.DELIVERED and order.rider_id == rider.id
    assert "drop_code" not in events(session, order)[-1].data  # the code is never written to the log


def test_failed_then_rescheduled_by_admin(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.FAILED)
    assert events(session, order)[-1].data == {"reason": "unreachable"}
    transition(session, order, S.READY, "admin", "admin-1")
    assert order.status == S.READY and order.rider_id is None
    assert len(events(session, order)) == 6


@pytest.mark.parametrize("state", [S.BOOKED, S.READY, S.ASSIGNED])
@pytest.mark.parametrize("actor", ["merchant", "admin"])
def test_cancel_from_each_allowed_state(session, state, actor):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, state)
    before = len(events(session, order))
    transition(session, order, S.CANCELLED, actor, merchant.id if actor == "merchant" else "admin-1")
    assert order.status == S.CANCELLED and len(events(session, order)) == before + 1


def test_admin_assigns_a_rider(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.READY)
    transition(session, order, S.ASSIGNED, "admin", "admin-1", {"rider_id": rider.id})
    assert order.rider_id == rider.id


# --- Disallowed transitions --------------------------------------------------------------


def test_every_move_outside_the_table_raises_invalid_transition(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = book(session, merchant)
    order.rider_id = rider.id
    checked = 0
    for from_status in S:
        for to_status in S:
            for actor in Actor:
                if actor in orders.TRANSITIONS.get((from_status, to_status), ()):
                    continue
                order.status = from_status  # test set-up only: put the order in the starting state
                session.flush()
                with pytest.raises(InvalidTransition):
                    transition(session, order, to_status, actor, rider.id, {"reason": "other", "rider_id": rider.id})
                assert order.status == from_status
                checked += 1
    allowed = sum(len(actors) for actors in orders.TRANSITIONS.values())
    assert checked == len(S) * len(S) * len(Actor) - allowed
    assert len(events(session, order)) == 1  # only the booking; nothing was logged for refusals


def test_the_table_is_exactly_the_brief(session):
    assert orders.TRANSITIONS == {
        ("BOOKED", "READY"): {"customer"},
        ("READY", "ASSIGNED"): {"rider", "admin"},
        ("ASSIGNED", "PICKED_UP"): {"rider"},
        ("PICKED_UP", "DELIVERED"): {"rider"},
        ("PICKED_UP", "FAILED"): {"rider"},
        ("FAILED", "READY"): {"admin"},
        ("BOOKED", "CANCELLED"): {"merchant", "admin"},
        ("READY", "CANCELLED"): {"merchant", "admin"},
        ("ASSIGNED", "CANCELLED"): {"merchant", "admin"},
    }
    with pytest.raises(InvalidTransition):
        transition(session, book(session, make_merchant(session)), "LOST", "admin")


# --- Conditions --------------------------------------------------------------------------


def expect_refusal(reason, session, order, *args, **kwargs):
    before = (order.status, len(events(session, order)))
    with pytest.raises(TransitionConditionFailed) as info:
        transition(session, order, *args, **kwargs)
    assert info.value.reason == reason
    assert (order.status, len(events(session, order))) == before


def test_conditions_are_enforced(session):
    merchant, rider, other = make_merchant(session), make_rider(session), make_rider(session)
    order = book(session, merchant)

    expect_refusal("drop_location_missing", session, order, S.READY, "customer")
    advance(session, order, rider, S.READY)

    inactive = make_rider(session, active=False)
    expect_refusal("rider_not_active", session, order, S.ASSIGNED, "rider", inactive.id)
    expect_refusal("rider_not_active", session, order, S.ASSIGNED, "admin", "admin-1", {})
    expect_refusal("rider_not_active", session, order, S.ASSIGNED, "rider", 999_999)
    advance(session, order, rider, S.ASSIGNED)

    expect_refusal("pickup_photo_missing", session, order, S.PICKED_UP, "rider", rider.id)
    add_photo(session, order, "pickup")
    expect_refusal("not_assigned_rider", session, order, S.PICKED_UP, "rider", other.id)
    transition(session, order, S.PICKED_UP, "rider", rider.id)

    expect_refusal("fail_reason_missing", session, order, S.FAILED, "rider", rider.id, {"reason": "bored"})
    expect_refusal("fail_reason_missing", session, order, S.FAILED, "rider", rider.id)
    expect_refusal("delivery_photo_missing", session, order, S.DELIVERED, "rider", rider.id, {"drop_code": order.drop_code})
    add_photo(session, order, "delivery")
    expect_refusal("not_assigned_rider", session, order, S.DELIVERED, "rider", other.id, {"drop_code": order.drop_code})
    transition(session, order, S.DELIVERED, "rider", rider.id, {"drop_code": order.drop_code})


def test_a_merchant_cannot_cancel_another_merchants_order(session):
    mine, theirs = make_merchant(session), make_merchant(session)
    order = book(session, mine)
    expect_refusal("not_order_merchant", session, order, S.CANCELLED, "merchant", theirs.id)


# --- Booking refusals --------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup, reason",
    [
        (dict(plan=None), "no_plan"),
        (dict(plan="daily_bronze", at=T0 - timedelta(days=1)), "plan_expired"),
        (dict(status=MerchantStatus.PENDING), "merchant_not_active"),
        (dict(status=MerchantStatus.SUSPENDED), "merchant_not_active"),
        (dict(active=False), "merchant_not_active"),
    ],
)
def test_booking_is_refused(session, setup, reason):
    merchant = make_merchant(session, **setup)
    with pytest.raises(TransitionConditionFailed) as info:
        book(session, merchant)
    assert info.value.reason == reason
    assert session.scalar(select(func.count()).select_from(Order)) == 0
    assert session.scalar(select(func.count()).select_from(Receipt)) == 0


def test_booking_is_refused_with_zero_drops(session):
    merchant = make_merchant(session, plan="daily_bronze")
    book(session, merchant), book(session, merchant)
    assert merchant.drops_left == 0
    with pytest.raises(TransitionConditionFailed) as info:
        book(session, merchant)
    assert info.value.reason == "no_drops_left"
    assert merchant.drops_left == 0


def test_booking_rejects_bad_money_and_bad_phones(session):
    merchant = make_merchant(session)
    for bad in (-1, 85_000.0, "85000", True):
        with pytest.raises(TransitionConditionFailed):
            book(session, merchant, cod_ugx=bad)
    with pytest.raises(ValueError):
        book(session, merchant, customer_phone="12345")
    assert merchant.drops_left == 20


# --- Drop accounting ---------------------------------------------------------------------


def test_booking_uses_one_drop_and_cancel_before_pickup_returns_it(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = book(session, merchant)
    assert (merchant.drops_left, order.drop_used) == (19, True)
    advance(session, order, rider, S.ASSIGNED)
    transition(session, order, S.CANCELLED, "merchant", merchant.id)
    assert (merchant.drops_left, order.drop_used) == (20, False)
    assert events(session, order)[-1].data["drop_returned"] is True


def test_failed_keeps_the_drop_and_reschedule_is_free(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.FAILED)
    assert (merchant.drops_left, order.drop_used) == (19, True)
    transition(session, order, S.READY, "admin", "admin-1")
    advance(session, order, rider, S.DELIVERED)
    assert (merchant.drops_left, order.drop_used) == (19, True)


def test_no_double_use_or_double_return(session):
    merchant = make_merchant(session)
    order = book(session, merchant)
    assert plans.use_drop(merchant, order) is False
    assert merchant.drops_left == 19
    assert plans.return_drop(merchant, order, T0) is True
    assert plans.return_drop(merchant, order, T0) is False
    assert merchant.drops_left == 20


def test_a_drop_is_not_returned_to_a_different_or_ended_plan(session):
    merchant = make_merchant(session, plan="daily_bronze")
    order = book(session, merchant)
    plans.activate_plan(merchant, "weekly_gold", T0 + timedelta(hours=1))  # bought a new plan since
    transition(session, order, S.CANCELLED, "merchant", merchant.id, at=T0 + timedelta(hours=2))
    assert (merchant.drops_left, order.drop_used) == (20, False)

    expiring = make_merchant(session, plan="daily_bronze")
    late = book(session, expiring)
    transition(session, late, S.CANCELLED, "admin", "admin-1", at=T0 + timedelta(days=2))
    assert expiring.drops_left == 1


# --- Drop code ---------------------------------------------------------------------------


def wrong_code(order):
    return "0000" if order.drop_code != "0000" else "1111"


def test_three_wrong_drop_codes_lock_the_order_and_alert_an_admin(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.PICKED_UP)
    add_photo(session, order, "delivery")

    for attempt, reason in [(1, "drop_code_wrong"), (2, "drop_code_wrong"), (3, "drop_code_locked")]:
        with pytest.raises(DropCodeRejected) as info:
            transition(session, order, S.DELIVERED, "rider", rider.id, {"drop_code": wrong_code(order)})
        assert info.value.reason == reason
        assert len(events(session, order, EventKind.DROP_CODE_WRONG)) == attempt
    assert order.status == S.PICKED_UP

    alerts = list(session.execute(select(AdminAlert).where(AdminAlert.order_id == order.id)).scalars())
    assert [a.kind for a in alerts] == ["drop_code_locked"]
    assert alerts[0].data == {"order_code": order.code, "attempts": 3}

    # Locked: even the right code is refused now, and no further attempts are logged.
    with pytest.raises(DropCodeRejected) as info:
        transition(session, order, S.DELIVERED, "rider", rider.id, {"drop_code": order.drop_code})
    assert info.value.reason == "drop_code_locked"
    assert len(events(session, order, EventKind.DROP_CODE_WRONG)) == 3

    orders.reset_drop_code_attempts(session, order, "admin-1")
    transition(session, order, S.DELIVERED, "rider", rider.id, {"drop_code": order.drop_code})
    assert order.status == S.DELIVERED


def test_a_missing_drop_code_counts_as_wrong(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.PICKED_UP)
    add_photo(session, order, "delivery")
    with pytest.raises(DropCodeRejected):
        transition(session, order, S.DELIVERED, "rider", rider.id)
    assert len(events(session, order, EventKind.DROP_CODE_WRONG)) == 1


# --- Receipts and side effects -----------------------------------------------------------


def test_each_transition_issues_exactly_its_receipts(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = book(session, merchant, cod_ugx=85_000)
    assert receipt_types(session, order) == ["booking_confirmation"]
    for state in (S.READY, S.ASSIGNED, S.PICKED_UP):
        advance(session, order, rider, state)
        assert receipt_types(session, order) == ["booking_confirmation"]
    advance(session, order, rider, S.DELIVERED)
    assert receipt_types(session, order) == [
        "booking_confirmation", "delivery_merchant", "delivery_customer", "delivery_rider",
    ]  # fmt: skip

    failed = advance(session, book(session, merchant), rider, S.FAILED)
    assert receipt_types(session, failed) == ["booking_confirmation", "failed_delivery_merchant"]
    transition(session, failed, S.READY, "admin", "admin-1")
    assert receipt_types(session, failed) == ["booking_confirmation", "failed_delivery_merchant"]

    cancelled = book(session, merchant)
    transition(session, cancelled, S.CANCELLED, "merchant", merchant.id)
    assert receipt_types(session, cancelled) == ["booking_confirmation"]


def test_delivery_receipts_go_to_the_right_people_and_name_the_shop(session):
    merchant, rider = make_merchant(session, shop_name="Kisementi Threads"), make_rider(session)
    order = advance(session, book(session, merchant, cod_ugx=85_000), rider, S.DELIVERED)
    by_type = {r.type: r for r in session.execute(select(Receipt).where(Receipt.order_id == order.id)).scalars()}
    assert by_type["delivery_merchant"].recipient_phone == merchant.phone
    assert by_type["delivery_customer"].recipient_phone == order.customer_phone
    assert by_type["delivery_rider"].recipient_phone == rider.phone
    assert all(by_type[t].amount_ugx == 85_000 for t in ("delivery_merchant", "delivery_customer", "delivery_rider"))
    assert by_type["booking_confirmation"].sent_at is None

    customer = by_type["delivery_customer"].snapshot
    printed = " ".join([value for _, value in customer["lines"]] + customer["notes"])
    assert "UGX 85,000, collected on behalf of Kisementi Threads" in printed
    assert "not a tax receipt" in printed
    assert order.drop_code not in str(customer)


def test_transition_returns_side_effects_and_sends_nothing(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order, effects = orders.create_order(
        session, merchant, customer_name="Sarah N", customer_phone=phone(), package="Shoes", at=T0
    )
    assert [e.kind for e in effects] == ["notify_customer_booked", "send_receipt"]
    assert effects[1].receipt_id == session.scalar(select(Receipt.id).where(Receipt.order_id == order.id))

    order.drop_lat, order.drop_lng = 0.31, 32.58
    assert [e.kind for e in transition(session, order, S.READY, "customer")] == ["notify_riders_job_available"]
    advance(session, order, rider, S.PICKED_UP)
    add_photo(session, order, "delivery")
    effects = transition(session, order, S.DELIVERED, "rider", rider.id, {"drop_code": order.drop_code})
    assert [e.kind for e in effects] == ["send_receipt"] * 3
    assert len({e.receipt_id for e in effects}) == 3


# --- Concurrency -------------------------------------------------------------------------


def test_two_riders_accepting_at_once_exactly_one_wins(engine):
    """Real commits on two connections, so this test cleans up after itself."""
    with Session(engine, expire_on_commit=False) as setup:
        merchant = make_merchant(setup)
        riders = [make_rider(setup), make_rider(setup)]
        order = Order(
            code=f"MNI-RACE-{phone()[-4:]}", merchant_id=merchant.id, customer_name="Sarah N", customer_phone=phone(),
            package="Shoes", drop_lat=0.31, drop_lng=32.58, drop_code="1234", tracking_token=phone(), status=S.READY,
        )  # fmt: skip
        setup.add(order)
        setup.commit()
    ids = {"order": order.id, "merchant": merchant.id, "riders": [r.id for r in riders]}

    barrier = threading.Barrier(2)

    def accept(rider_id: int) -> str:
        with Session(engine) as s:
            mine = s.get(Order, ids["order"])
            barrier.wait(timeout=10)
            try:
                transition(s, mine, S.ASSIGNED, "rider", rider_id)
                s.commit()
                return "won"
            except InvalidTransition:
                s.rollback()
                return "lost"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, ids["riders"]))
        assert sorted(results) == ["lost", "won"]
        with Session(engine) as check:
            final = check.get(Order, ids["order"])
            winner = ids["riders"][results.index("won")]
            assert (final.status, final.rider_id) == (S.ASSIGNED, winner)
            assigned = check.scalar(
                select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == ids["order"])
            )
            assert assigned == 1
    finally:
        with engine.begin() as conn:
            # order_events is append-only; lift the triggers for this clean-up only.
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(OrderEvent.__table__.delete().where(OrderEvent.order_id == ids["order"]))
            conn.execute(Order.__table__.delete().where(Order.id == ids["order"]))
            conn.execute(Rider.__table__.delete().where(Rider.id.in_(ids["riders"])))
            conn.execute(Merchant.__table__.delete().where(Merchant.id == ids["merchant"]))
