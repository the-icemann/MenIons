from datetime import timedelta

from app import router
from app.events import TextMessage
from app.models import Conversation, MerchantStatus, OrderStatus, Role
from app.orders import transition
from tests.conftest import T0, advance, book, make_merchant, make_rider, phone


def text_from(sender, at=T0, wamid="wamid.X"):
    return TextMessage(provider_message_id=wamid, received_at=at, phone=sender, text="Hi")


def test_roles_are_resolved_in_order(session):
    active = make_merchant(session)
    pending = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    suspended = make_merchant(session, status=MerchantStatus.SUSPENDED)
    rider = make_rider(session)
    order = book(session, active)
    assert router.resolve_role(session, active.phone) == Role.MERCHANT
    assert router.resolve_role(session, pending.phone) == Role.MERCHANT_PENDING
    assert router.resolve_role(session, rider.phone) == Role.RIDER
    assert router.resolve_role(session, order.customer_phone) == Role.CUSTOMER
    assert router.resolve_role(session, suspended.phone) == Role.UNKNOWN
    assert router.resolve_role(session, phone()) == Role.UNKNOWN


def test_a_customer_is_known_only_while_an_order_is_open(session):
    merchant, rider = make_merchant(session), make_rider(session)
    failed = advance(session, book(session, merchant), rider, OrderStatus.FAILED)
    assert router.resolve_role(session, failed.customer_phone) == Role.CUSTOMER  # may be rescheduled
    done = advance(session, book(session, merchant), rider, OrderStatus.DELIVERED)
    assert router.resolve_role(session, done.customer_phone) == Role.UNKNOWN
    cancelled = book(session, merchant)
    transition(session, cancelled, OrderStatus.CANCELLED, "admin", "a")
    assert router.resolve_role(session, cancelled.customer_phone) == Role.UNKNOWN


def test_dispatch_calls_the_handler_for_the_role_and_keeps_conversation_state(session, monkeypatch):
    calls = []
    for name in ("merchant", "merchant_pending", "rider", "customer", "unknown"):
        monkeypatch.setattr(
            router, f"handle_{name}", lambda s, e, c, name=name: calls.append((name, c.phone, c.role))
        )
    merchant = make_merchant(session)
    pending = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    rider = make_rider(session)
    customer = book(session, merchant).customer_phone
    stranger = phone()
    for sender in (merchant.phone, pending.phone, rider.phone, customer, stranger):
        router.dispatch(session, text_from(sender))
    assert calls == [
        ("merchant", merchant.phone, "merchant"),
        ("merchant_pending", pending.phone, "merchant_pending"),
        ("rider", rider.phone, "rider"),
        ("customer", customer, "customer"),
        ("unknown", stranger, "unknown"),
    ]

    # The role follows the sender's current state; step and context survive.
    conversation = session.get(Conversation, pending.phone)
    conversation.step, conversation.context = "awaiting_payment", {"plan": "weekly_gold"}
    pending.status = MerchantStatus.ACTIVE
    session.flush()
    later = T0 + timedelta(hours=2)
    router.dispatch(session, text_from(pending.phone, at=later))
    session.refresh(conversation)
    assert (conversation.role, conversation.step, conversation.context) == (
        "merchant", "awaiting_payment", {"plan": "weekly_gold"},
    )  # fmt: skip
    assert conversation.last_inbound_at == later


def test_in_free_window_is_24_hours_from_the_last_inbound_message(session):
    sender = phone()
    assert router.in_free_window(session, sender, T0) is False  # never wrote to us
    router.dispatch(session, text_from(sender, at=T0))
    assert router.in_free_window(session, sender, T0 + timedelta(hours=23, minutes=59)) is True
    assert router.in_free_window(session, sender, T0 + timedelta(hours=24)) is False
    assert router.in_free_window(session, sender, T0 + timedelta(hours=24, minutes=1)) is False
    router.dispatch(session, text_from(sender, at=T0 + timedelta(hours=30)))
    assert router.in_free_window(session, sender, T0 + timedelta(hours=40)) is True
