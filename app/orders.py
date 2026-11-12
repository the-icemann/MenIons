"""The order state machine.

create_order() and transition() are the only ways to set orders.status. Each validates the
move, checks its condition, writes one order_events row, issues the receipts that go with
it, and returns the side effects (messages to send) for the caller to perform. Neither
sends anything itself, and neither commits: the caller owns the transaction.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import plans, receipts
from app.clock import utcnow
from app.models import (
    Actor,
    AdminAlert,
    EventKind,
    Media,
    Merchant,
    MerchantStatus,
    Order,
    OrderEvent,
    OrderStatus,
    Rider,
    order_code_seq,
)
from app.phones import normalize_phone

S = OrderStatus
A = Actor

# (from, to) -> who may trigger it. Anything not listed here is not allowed.
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    (S.BOOKED, S.READY): frozenset({A.CUSTOMER}),
    (S.READY, S.ASSIGNED): frozenset({A.RIDER, A.ADMIN}),
    (S.ASSIGNED, S.PICKED_UP): frozenset({A.RIDER}),
    (S.PICKED_UP, S.DELIVERED): frozenset({A.RIDER}),
    (S.PICKED_UP, S.FAILED): frozenset({A.RIDER}),
    (S.FAILED, S.READY): frozenset({A.ADMIN}),
    (S.BOOKED, S.CANCELLED): frozenset({A.MERCHANT, A.ADMIN}),
    (S.READY, S.CANCELLED): frozenset({A.MERCHANT, A.ADMIN}),
    (S.ASSIGNED, S.CANCELLED): frozenset({A.MERCHANT, A.ADMIN}),
}

FAIL_REASONS = frozenset({"unreachable", "refused", "wrong_address", "other"})
MAX_DROP_CODE_ATTEMPTS = 3


class InvalidTransition(Exception):
    """The move is not in the transition table, or this actor may not make it."""


class TransitionConditionFailed(Exception):
    """The move is allowed but its condition is not met. `reason` is a stable code."""

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


class DropCodeRejected(TransitionConditionFailed):
    """A wrong drop code, or the order is locked after too many.

    The attempt has been written to order_events (and, on the last one, an admin alert).
    The caller MUST COMMIT after catching this, not roll back, or the attempt count is
    lost and the 4-digit code could be guessed by trying again and again.
    """


@dataclass(frozen=True)
class SideEffect:
    """Something to do after the transaction commits. Milestone 2 performs these."""

    kind: str
    order_id: int
    receipt_id: int | None = None


def _lock(session: Session, model, row_id: int):
    session.flush()
    return session.execute(
        select(model).where(model.id == row_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def _record(
    session: Session,
    order: Order,
    *,
    kind: str = EventKind.TRANSITION,
    from_status: str | None,
    to_status: str,
    actor: str,
    actor_id: Any,
    at: datetime,
    data: dict[str, Any],
) -> None:
    session.add(
        OrderEvent(
            order_id=order.id,
            kind=str(kind),
            from_status=from_status,
            to_status=str(to_status),
            actor=str(actor),
            actor_id=None if actor_id is None else str(actor_id),
            at=at,
            data=data,
        )
    )


def _effects(order: Order, to_status: str, issued: list) -> list[SideEffect]:
    names = {
        S.BOOKED: ["notify_customer_booked"],
        S.READY: ["notify_riders_job_available"],
        S.ASSIGNED: ["notify_rider_assigned", "notify_merchant_rider_assigned"],
        S.PICKED_UP: ["notify_customer_picked_up"],
        S.DELIVERED: [],
        S.FAILED: ["notify_admin_delivery_failed"],
        S.CANCELLED: ["notify_order_cancelled"],
    }[S(to_status)]
    effects = [SideEffect(name, order.id) for name in names]
    effects += [SideEffect("send_receipt", order.id, receipt.id) for receipt in issued]
    return effects


def create_order(
    session: Session,
    merchant: Merchant,
    *,
    customer_name: str,
    customer_phone: str,
    package: str,
    drop_text: str | None = None,
    drop_lat: float | None = None,
    drop_lng: float | None = None,
    cod_ugx: int = 0,
    notes: str | None = None,
    at: datetime | None = None,
) -> tuple[Order, list[SideEffect]]:
    """(new) -> BOOKED, by the merchant. Uses one drop from the merchant's plan."""
    at = at or utcnow()
    if not isinstance(cod_ugx, int) or isinstance(cod_ugx, bool) or cod_ugx < 0:
        raise TransitionConditionFailed("bad_cod_amount", "cod_ugx must be a whole number of UGX, zero or more")
    customer_phone = normalize_phone(customer_phone)

    with session.begin_nested():
        merchant = _lock(session, Merchant, merchant.id)
        if merchant.status != MerchantStatus.ACTIVE or not merchant.active:
            raise TransitionConditionFailed("merchant_not_active")
        refusal = plans.drop_refusal(merchant, at)
        if refusal:
            raise TransitionConditionFailed(refusal)

        number = session.execute(select(order_code_seq.next_value())).scalar_one()
        order = Order(
            code=f"MNI-{number:04d}",
            merchant_id=merchant.id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            drop_text=drop_text,
            drop_lat=drop_lat,
            drop_lng=drop_lng,
            package=package,
            cod_ugx=cod_ugx,
            notes=notes,
            drop_code=f"{secrets.randbelow(10_000):04d}",
            tracking_token=secrets.token_urlsafe(16),
            status=S.BOOKED,
            drop_used=False,
            created_at=at,
            updated_at=at,
        )
        session.add(order)
        plans.use_drop(merchant, order)
        session.flush()
        _record(session, order, from_status=None, to_status=S.BOOKED, actor=A.MERCHANT,
                actor_id=merchant.id, at=at, data={})  # fmt: skip
        issued = receipts.issue_for_transition(session, order, S.BOOKED, at, {})
        session.flush()
    return order, _effects(order, S.BOOKED, issued)


def _require_assigned_rider(order: Order, actor: str, actor_id: Any) -> None:
    if actor == A.RIDER and (actor_id is None or order.rider_id != actor_id):
        raise TransitionConditionFailed("not_assigned_rider", "only the rider assigned to this order can do this")


def _has_photo(session: Session, order: Order, kind: str) -> bool:
    photo = select(Media.id).where(Media.order_id == order.id, Media.kind == kind).limit(1)
    return session.execute(photo).scalar() is not None


def drop_code_attempts(session: Session, order: Order) -> int:
    """Wrong drop codes entered since the last admin reset."""
    last_reset = session.execute(
        select(func.coalesce(func.max(OrderEvent.id), 0)).where(
            OrderEvent.order_id == order.id, OrderEvent.kind == EventKind.DROP_CODE_RESET
        )
    ).scalar_one()
    return session.execute(
        select(func.count()).where(
            OrderEvent.order_id == order.id,
            OrderEvent.kind == EventKind.DROP_CODE_WRONG,
            OrderEvent.id > last_reset,
        )
    ).scalar_one()


def reset_drop_code_attempts(session: Session, order: Order, admin_id: str, at: datetime | None = None) -> None:
    """An admin unlocks an order after checking with the customer."""
    order = _lock(session, Order, order.id)
    _record(session, order, kind=EventKind.DROP_CODE_RESET, from_status=order.status, to_status=order.status,
            actor=A.ADMIN, actor_id=admin_id, at=at or utcnow(), data={})  # fmt: skip
    session.flush()


def _check_drop_code(session: Session, order: Order, code: Any, actor: str, actor_id: Any, at: datetime) -> None:
    attempts = drop_code_attempts(session, order)
    if attempts >= MAX_DROP_CODE_ATTEMPTS:
        raise DropCodeRejected("drop_code_locked", "too many wrong codes; an admin must unlock this order")
    given = "" if code is None else str(code).strip()
    if hmac.compare_digest(given.encode(), order.drop_code.encode()):
        return
    attempts += 1
    _record(session, order, kind=EventKind.DROP_CODE_WRONG, from_status=order.status, to_status=order.status,
            actor=actor, actor_id=actor_id, at=at, data={"attempt": attempts})  # fmt: skip
    locked = attempts >= MAX_DROP_CODE_ATTEMPTS
    if locked:
        session.add(AdminAlert(kind="drop_code_locked", order_id=order.id, rider_id=order.rider_id, at=at,
                               data={"order_code": order.code, "attempts": attempts}))  # fmt: skip
    session.flush()
    raise DropCodeRejected("drop_code_locked" if locked else "drop_code_wrong")


def transition(
    session: Session,
    order: Order,
    to_status: str,
    actor: str,
    actor_id: Any = None,
    data: dict[str, Any] | None = None,
    *,
    at: datetime | None = None,
) -> list[SideEffect]:
    """Move an order to `to_status`. The only way to change orders.status after booking.

    actor_id: the merchant's or rider's id, or the admin's identifier.
    data: READY -> ASSIGNED by an admin needs {"rider_id": ...};
          PICKED_UP -> DELIVERED needs {"drop_code": "1234"};
          PICKED_UP -> FAILED needs {"reason": one of FAIL_REASONS}.

    Raises InvalidTransition or TransitionConditionFailed. See DropCodeRejected: after that
    one the caller must commit.
    """
    at = at or utcnow()
    data = dict(data or {})
    try:
        to_status = S(to_status)
    except ValueError:
        raise InvalidTransition(f"unknown status {to_status!r}") from None

    # Lock the order row. Two riders accepting at once queue up here; the second then sees
    # ASSIGNED and is refused.
    order = _lock(session, Order, order.id)
    from_status = S(order.status)
    allowed = TRANSITIONS.get((from_status, to_status))
    if allowed is None:
        raise InvalidTransition(f"{order.code}: {from_status} -> {to_status} is not allowed")
    if actor not in allowed:
        raise InvalidTransition(f"{order.code}: {from_status} -> {to_status} can't be done by {actor}")

    if to_status == S.DELIVERED:
        # Checked outside the savepoint below, so a wrong attempt stays recorded.
        _require_assigned_rider(order, actor, actor_id)
        if not _has_photo(session, order, "delivery"):
            raise TransitionConditionFailed("delivery_photo_missing")
        _check_drop_code(session, order, data.pop("drop_code", None), actor, actor_id, at)

    with session.begin_nested():
        if to_status == S.READY:
            if from_status == S.BOOKED:
                if order.drop_lat is None or order.drop_lng is None:
                    raise TransitionConditionFailed("drop_location_missing")
            else:
                # Reschedule after a failed attempt: back in the pool, no second drop used.
                order.rider_id = None

        elif to_status == S.ASSIGNED:
            rider_id = actor_id if actor == A.RIDER else data.get("rider_id")
            rider = session.get(Rider, rider_id) if isinstance(rider_id, int) else None
            if rider is None or not rider.active:
                raise TransitionConditionFailed("rider_not_active")
            order.rider_id = rider.id
            data["rider_id"] = rider.id

        elif to_status == S.PICKED_UP:
            _require_assigned_rider(order, actor, actor_id)
            if not _has_photo(session, order, "pickup"):
                raise TransitionConditionFailed("pickup_photo_missing")

        elif to_status == S.FAILED:
            _require_assigned_rider(order, actor, actor_id)
            if data.get("reason") not in FAIL_REASONS:
                raise TransitionConditionFailed("fail_reason_missing", f"reason must be one of {sorted(FAIL_REASONS)}")

        elif to_status == S.CANCELLED:
            if actor == A.MERCHANT and actor_id != order.merchant_id:
                raise TransitionConditionFailed("not_order_merchant", "only the order's own merchant can cancel it")
            merchant = _lock(session, Merchant, order.merchant_id)
            data["drop_returned"] = plans.return_drop(merchant, order, at)

        order.status = to_status
        order.updated_at = at
        _record(session, order, from_status=from_status, to_status=to_status, actor=actor,
                actor_id=actor_id, at=at, data=data)  # fmt: skip
        session.flush()
        issued = receipts.issue_for_transition(session, order, to_status, at, data)
    return _effects(order, to_status, issued)
