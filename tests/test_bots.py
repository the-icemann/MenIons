"""The bots, driven message by message through the router, as WhatsApp would."""

import itertools
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import messaging, plans, router
from app.config import get_settings
from app.events import ButtonReply, ContactShared, ListReply, LocationShared, MediaReceived, TextMessage
from app.messaging import Outgoing, deliver, take_outbox
from app.models import (
    AdminAlert,
    Conversation,
    Media,
    Merchant,
    MerchantStatus,
    Order,
    OrderStatus,
    PlanRequest,
    PlanRequestStatus,
    Receipt,
    Rider,
)
from app.whatsapp.fake import FakeProvider
from tests.conftest import T0, advance, book, fixture_payload, make_merchant, make_rider, phone

S = OrderStatus
ADMIN = "+256799999001"
_ids = itertools.count(1)
_clock = itertools.count(1)


@pytest.fixture(autouse=True)
def fake_provider():
    provider = FakeProvider()
    messaging.set_provider(provider)
    yield provider
    messaging.set_provider(None)


def _at():
    return T0 + timedelta(seconds=next(_clock))


def _base(sender):
    return {"provider_message_id": f"wamid.BOT{next(_ids)}", "received_at": _at(), "phone": sender}


def say(session, sender, text):
    return talk(session, TextMessage(text=text, **_base(sender)))


def tap(session, sender, button_id):
    return talk(session, ButtonReply(button_id=button_id, title="", **_base(sender)))


def pick(session, sender, row_id):
    return talk(session, ListReply(row_id=row_id, title="", **_base(sender)))


def pin(session, sender, lat=0.3136, lng=32.5811):
    return talk(session, LocationShared(lat=lat, lng=lng, **_base(sender)))


def contact(session, sender, name, *numbers):
    return talk(session, ContactShared(contacts=[(name, list(numbers))], **_base(sender)))


def photo(session, sender):
    return talk(session, MediaReceived(kind="image", media_id=f"media{next(_ids)}", **_base(sender)))


def talk(session, event) -> list[Outgoing]:
    take_outbox(session)
    with session.begin_nested():
        router.dispatch(session, event)
    return take_outbox(session)


def to(out, who):
    return [m for m in out if m.phone == who]


def body(out, who=None):
    return "\n".join(m.body for m in out if who is None or m.phone == who)


def buttons(out, who):
    return [b for m in to(out, who) for b in m.buttons]


def rows(out, who):
    return [r["id"] for m in to(out, who) for s in m.sections for r in s["rows"]]


# --- The whole journey -------------------------------------------------------------------


def test_from_first_hello_to_delivered(session):
    shop, customer, rider_phone = phone(), phone(), phone()

    # Onboarding
    out = say(session, shop, "Hello")
    assert ("onb:start", "Register my shop") in buttons(out, shop)
    assert "shop's name" in body(tap(session, shop, "onb:start"))
    say(session, shop, "Kisementi Threads")
    say(session, shop, "Akello Grace")
    out = say(session, shop, "Clothes")
    assert ("vol:3-5", "3-5 a day") in buttons(out, shop)
    out = tap(session, shop, "vol:3-5")
    assert to(out, shop)[0].kind == "location_request"
    pin(session, shop)
    out = say(session, shop, "Opposite Shell Kisementi, 1st floor")
    assert "Kisementi Threads" in body(out) and ("onb:confirm", "Confirm") in buttons(out, shop)
    out = tap(session, shop, "onb:confirm")
    merchant = session.execute(select(Merchant).where(Merchant.phone == shop)).scalar_one()
    assert merchant.status == MerchantStatus.PENDING and merchant.pickup_lat == 0.3136
    assert "plan:weekly_gold" in rows(out, shop) and "Weekly Gold" in body(out, shop)
    assert "New shop registered" in body(out, ADMIN)

    # Plan purchase
    out = pick(session, shop, "plan:weekly_gold")
    request = session.execute(select(PlanRequest).where(PlanRequest.merchant_id == merchant.id)).scalar_one()
    assert request.momo_reference in body(out, shop) and "0700000099" in body(out, shop)
    out = tap(session, shop, f"paid:{request.id}")
    assert request.status == PlanRequestStatus.PAYMENT_CLAIMED
    assert f"/approve {request.momo_reference}" in body(out, ADMIN)
    assert "checking" in body(say(session, shop, "hi"), shop)

    out = say(session, ADMIN, f"/approve {request.momo_reference} FDN-1001 VC-77")
    assert merchant.status == MerchantStatus.ACTIVE and merchant.drops_left == 20
    assert "Weekly Gold" in body(out, shop) and "https://menions.test/r/RCT-" in body(out, shop)
    assert "Approved" in body(out, ADMIN)

    # A rider joins and goes on shift
    say(session, ADMIN, f"/rider add {rider_phone} Musa K")
    rider = session.execute(select(Rider).where(Rider.phone == rider_phone)).scalar_one()
    assert ("r:on", "Go on shift") in buttons(say(session, rider_phone, "hi"), rider_phone)
    tap(session, rider_phone, "r:on")
    assert rider.on_shift

    # Booking
    out = say(session, shop, "menu")
    assert "m:book" in rows(out, shop)
    tap(session, shop, "m:book")
    say(session, shop, "Sarah N")
    say(session, shop, "0" + customer[4:])
    out = say(session, shop, "2 dresses in a black bag")
    assert "notes" in body(out) and "cash" not in body(out).lower()  # we only deliver; no money step
    out = tap(session, shop, "notes:none")
    assert "1 of your 20 drops" in body(out) and "UGX" not in body(out)
    out = tap(session, shop, "book:yes")
    order = session.execute(select(Order).where(Order.merchant_id == merchant.id)).scalar_one()
    assert (order.status, order.cod_ugx, order.customer_phone) == (S.BOOKED, 0, customer)
    assert f"https://wa.me/256700000099?text={order.code}" in body(out, shop)
    assert "Booking confirmation" in body(out, shop)
    assert to(out, customer)[0].optional  # likely outside the window; the link is the real path

    # The customer follows the link and shares a location
    out = say(session, customer, order.code)
    assert to(out, customer)[0].kind == "location_request" and "Kisementi Threads" in body(out)
    out = pin(session, customer, 0.30, 32.60)
    assert order.status == S.READY and order.drop_code in body(out, customer)
    assert (f"acc:{order.id}", "Accept job") in buttons(out, rider_phone)
    say(session, customer, "Green gate behind the church")
    assert "Green gate" in order.drop_text

    # The rider takes the job, picks up, delivers
    out = tap(session, rider_phone, f"acc:{order.id}")
    assert order.status == S.ASSIGNED and (f"pu:{order.id}", "Picked up") in buttons(out, rider_phone)
    assert "Green gate" in body(out, rider_phone) and "Musa K" in body(out, shop)
    tap(session, rider_phone, f"pu:{order.id}")
    assert "photo" in body(say(session, rider_phone, "done"), rider_phone)  # text where a photo is needed
    out = photo(session, rider_phone)
    assert order.status == S.PICKED_UP and order.drop_code in body(out, customer)
    tap(session, rider_phone, f"dl:{order.id}")
    photo(session, rider_phone)
    wrong = "0000" if order.drop_code != "0000" else "1111"
    assert "1 of 3" in body(say(session, rider_phone, wrong), rider_phone)
    out = say(session, rider_phone, order.drop_code)
    assert order.status == S.DELIVERED
    assert {m.phone for m in out if m.receipt_id} == {shop, customer, rider_phone}
    assert session.scalar(select(Media.id).where(Media.order_id == order.id, Media.kind == "delivery"))
    for receipt in session.execute(select(Receipt).where(Receipt.order_id == order.id)).scalars():
        assert receipt.amount_ugx is None and "Cash" not in str(receipt.snapshot)


# --- Merchants -----------------------------------------------------------------------------


def test_cancel_before_pickup_returns_the_drop(session):
    merchant = make_merchant(session)
    order = book(session, merchant)
    assert merchant.drops_left == 19
    out = tap(session, merchant.phone, f"cx:{order.id}")
    assert (f"cxy:{order.id}", "Yes, cancel it") in buttons(out, merchant.phone)
    out = tap(session, merchant.phone, f"cxy:{order.id}")
    assert order.status == S.CANCELLED and merchant.drops_left == 20
    assert "back on your plan" in body(out, merchant.phone)


def test_a_merchant_cannot_touch_another_merchants_order(session):
    mine, theirs = make_merchant(session), make_merchant(session)
    order = book(session, theirs)
    tap(session, mine.phone, f"cxy:{order.id}")
    assert order.status == S.BOOKED


def test_booking_without_drops_offers_a_plan(session):
    merchant = make_merchant(session, plan=None)
    out = tap(session, merchant.phone, "m:book")
    assert ("m:buy", "Buy a plan") in buttons(out, merchant.phone)
    assert session.scalar(select(Order.id).where(Order.merchant_id == merchant.id)) is None


def test_booking_rejects_a_bad_phone(session):
    merchant = make_merchant(session)
    tap(session, merchant.phone, "m:book")
    say(session, merchant.phone, "Sarah")
    assert "doesn't look like" in body(say(session, merchant.phone, "12345"))
    say(session, merchant.phone, "0772 123 456")
    assert "notes" in body(say(session, merchant.phone, "shoes"))
    conversation = session.get(Conversation, merchant.phone)
    assert conversation.context["phone"] == "+256772123456"


def test_a_suspended_merchant_is_told_and_not_onboarded(session):
    merchant = make_merchant(session, status=MerchantStatus.SUSPENDED)
    out = say(session, merchant.phone, "hi")
    assert "suspended" in body(out) and not buttons(out, merchant.phone)


def test_booking_from_a_shared_contact(session):
    merchant = make_merchant(session)
    tap(session, merchant.phone, "m:book")
    out = contact(session, merchant.phone, "Sarah Nakato", "+256 701 234567")
    assert "Sarah Nakato, 0701 234567" in body(out) and "What are you sending" in body(out)
    conversation = session.get(Conversation, merchant.phone)
    assert (conversation.step, conversation.context["phone"]) == ("book:package", "+256701234567")


def test_a_contact_without_a_ugandan_number_asks_for_one(session):
    merchant = make_merchant(session)
    tap(session, merchant.phone, "m:book")
    assert "no Ugandan mobile" in body(contact(session, merchant.phone, "Wanjiku", "+254 712 345678"))
    out = contact(session, merchant.phone, "Wanjiku", "0772 000111")
    assert "What are you sending" in body(out)
    assert session.get(Conversation, merchant.phone).context["name"] == "Wanjiku"


def test_booking_for_a_recent_customer(session):
    merchant, other = make_merchant(session), make_merchant(session)
    first = book(session, merchant, customer_name="Sarah N", customer_phone="0701111111")
    book(session, merchant, customer_name="Sarah N", customer_phone="0701111111")
    book(session, merchant, customer_name="Paul K", customer_phone="0702222222")
    stranger = book(session, other, customer_name="Not Mine")
    out = tap(session, merchant.phone, "m:book")
    assert rows(out, merchant.phone) == [f"cust:{first.id + 2}", f"cust:{first.id + 1}"]  # newest first, one each
    tap(session, merchant.phone, f"cust:{stranger.id}")  # another shop's customer is ignored
    assert session.get(Conversation, merchant.phone).step == "book:name"
    out = pick(session, merchant.phone, f"cust:{first.id}")
    assert "Sarah N, 0701 111111" in body(out)


def test_customers_get_an_arrival_estimate(session):
    merchant, rider = make_merchant(session, pickup_lat=0.3136, pickup_lng=32.5811), make_rider(session)
    order = book(session, merchant)
    out = pin(session, order.customer_phone, 0.3355, 32.5960)
    assert "Estimated arrival: about 20–40 min" in body(out, order.customer_phone)
    tap(session, rider.phone, f"acc:{order.id}")
    tap(session, rider.phone, f"pu:{order.id}")
    out = photo(session, rider.phone)
    assert "Expected in about 5–15 min" in body(out, order.customer_phone)


def test_approval_opens_the_menu(session):
    merchant = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    say(session, merchant.phone, "hi")
    request = plans.create_plan_request(session, merchant, "weekly_gold", at=T0)
    out = say(session, ADMIN, f"/approve {request.momo_reference} FDN-1 VC-1")
    assert "m:book" in rows(out, merchant.phone)
    assert "Payment received" in body(out, merchant.phone) and "20 drops left" in str(
        to(out, merchant.phone)[-1].sections
    )


# --- Riders --------------------------------------------------------------------------------


def test_only_one_rider_gets_a_job(session):
    merchant, first, second = make_merchant(session), make_rider(session), make_rider(session)
    order = advance(session, book(session, merchant), first, S.READY)
    tap(session, first.phone, f"acc:{order.id}")
    out = tap(session, second.phone, f"acc:{order.id}")
    assert order.rider_id == first.id and "already taken" in body(out)


def test_three_wrong_codes_lock_the_order_and_alert_admins(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.PICKED_UP)
    tap(session, rider.phone, f"dl:{order.id}")
    photo(session, rider.phone)
    wrong = "0000" if order.drop_code != "0000" else "1111"
    say(session, rider.phone, wrong)
    say(session, rider.phone, wrong)
    out = say(session, rider.phone, wrong)
    assert "Too many wrong codes" in body(out, rider.phone) and f"/unlock {order.code}" in body(out, ADMIN)
    assert session.scalar(select(AdminAlert.id).where(AdminAlert.kind == "drop_code_locked"))
    assert order.status == S.PICKED_UP

    say(session, ADMIN, f"/unlock {order.code}")
    tap(session, rider.phone, f"dl:{order.id}")
    photo(session, rider.phone)
    say(session, rider.phone, order.drop_code)
    assert order.status == S.DELIVERED


def test_failed_delivery_then_reschedule(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.PICKED_UP)
    out = tap(session, rider.phone, f"fl:{order.id}")
    assert f"fr:{order.id}:refused" in rows(out, rider.phone)
    out = pick(session, rider.phone, f"fr:{order.id}:refused")
    assert order.status == S.FAILED and f"/reschedule {order.code}" in body(out, ADMIN)
    out = say(session, ADMIN, f"/reschedule {order.code}")
    assert order.status == S.READY and order.rider_id is None
    assert (f"acc:{order.id}", "Accept job") in buttons(out, rider.phone)


def test_a_ready_job_with_nobody_on_shift_alerts_admins(session):
    merchant = make_merchant(session)
    order = book(session, merchant)
    out = pin(session, order.customer_phone)
    assert order.status == S.READY and "no rider is on shift" in body(out, ADMIN)


# --- Admin ---------------------------------------------------------------------------------


def test_only_admins_can_run_commands(session):
    merchant = make_merchant(session)
    out = say(session, merchant.phone, "/riders")
    assert "Admin" not in body(out) and "m:book" in rows(out, merchant.phone)


def test_admin_errors_are_explained(session):
    assert "no order" in body(say(session, ADMIN, "/cancel MNI-9999"))
    assert "usage" in body(say(session, ADMIN, "/approve"))
    merchant = make_merchant(session, plan=None, status=MerchantStatus.PENDING)
    request = plans.create_plan_request(session, merchant, "weekly_gold", at=T0)
    reply = body(say(session, ADMIN, f"/approve {request.momo_reference} 000gr"))
    assert "3 parts" in reply and f"/approve {request.momo_reference} " in reply
    assert request.status == PlanRequestStatus.AWAITING_PAYMENT
    assert "Unknown command" in body(say(session, ADMIN, "/dance"))
    assert "Unknown command" in body(say(session, ADMIN, "/cash 0772123456 5000"))  # riders don't handle money
    assert "/approve" in body(say(session, ADMIN, "/help"))


def test_as_lets_one_admin_phone_play_every_part(session):
    merchant = make_merchant(session, phone=ADMIN)
    session.add(Rider(name="Admin Rider", phone=ADMIN, on_shift=False))
    session.flush()
    order = book(session, merchant, customer_phone=ADMIN)
    assert "m:book" in rows(say(session, ADMIN, "hi"), ADMIN)  # merchant comes first
    say(session, ADMIN, "/as customer")
    out = say(session, ADMIN, "hi")
    assert to(out, ADMIN)[0].kind == "location_request"
    pin(session, ADMIN)
    assert order.status == S.READY
    say(session, ADMIN, "/as rider")
    assert ("r:on", "Go on shift") in buttons(say(session, ADMIN, "hi"), ADMIN)
    say(session, ADMIN, "/as auto")
    assert "m:book" in rows(say(session, ADMIN, "hi"), ADMIN)


def test_as_is_refused_in_production(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "env", "production")
    assert "only for testing" in body(say(session, ADMIN, "/as rider"))


# --- Sending -------------------------------------------------------------------------------


def test_messages_inside_the_window_are_sent_and_receipts_marked(session, fake_provider):
    merchant = make_merchant(session)
    say(session, merchant.phone, "hi")
    order = book(session, merchant, at=T0)
    receipt = session.execute(select(Receipt).where(Receipt.order_id == order.id)).scalar_one()
    items = [Outgoing(merchant.phone, "text", "hello", receipt_id=receipt.id)]
    deliver(session, items, now=T0 + timedelta(minutes=5))
    assert [m.method for m in fake_provider.sent] == ["send_text"] and receipt.sent_at is not None


def test_outside_the_window_without_a_template_nothing_is_sent(session, fake_provider):
    stranger = phone()
    deliver(
        session, [Outgoing(stranger, "text", "important"), Outgoing(stranger, "text", "nice", optional=True)], now=T0
    )
    assert fake_provider.sent == []
    alerts = session.execute(select(AdminAlert).where(AdminAlert.kind == "message_not_sent")).scalars().all()
    assert [a.data["text"] for a in alerts] == ["important"]


def test_outside_the_window_a_template_is_used_when_configured(session, fake_provider, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_update_template", "menions_update")
    deliver(session, [Outgoing(phone(), "buttons", "Line one\nLine two", buttons=[("a", "A")])], now=T0)
    (sent,) = fake_provider.sent
    assert sent.method == "send_template" and sent.payload["template_name"] == "menions_update"
    assert sent.payload["components"][0]["parameters"][0]["text"] == "Line one · Line two"


def test_a_failing_send_is_recorded_and_the_rest_still_go(session, fake_provider):
    who = phone()
    say(session, who, "hi")
    deliver(
        session,
        [Outgoing(who, "buttons", "bad", buttons=[]), Outgoing(who, "text", "good")],
        now=T0 + timedelta(minutes=1),
    )
    assert [m.payload.get("text") for m in fake_provider.sent] == ["good"]
    assert session.scalar(select(AdminAlert.id).where(AdminAlert.kind == "message_send_error"))


# --- Through the webhook -------------------------------------------------------------------


def test_a_kapso_hello_gets_the_welcome_reply(session, client, fake_provider):
    assert client.post("/webhooks/whatsapp", json=fixture_payload("kapso_text")).status_code == 200
    (sent,) = fake_provider.sent
    assert sent.phone == "+256772123456" and sent.method == "send_buttons" and "Register my shop" in str(sent.payload)


def test_a_handler_error_sends_nothing(session, client, fake_provider, monkeypatch):
    def broken(s, event, conversation):
        messaging.outbox(s).text(conversation.phone, "should never go out")
        raise RuntimeError("boom")

    monkeypatch.setattr(router, "handle_unknown", broken)
    assert client.post("/webhooks/whatsapp", json=fixture_payload("kapso_text")).status_code == 200
    assert fake_provider.sent == []
    row = session.execute(select(Media)).first()
    assert row is None


def test_photo_names_cannot_leave_the_media_folder(monkeypatch, tmp_path, fake_provider):
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path))
    fake_provider.media["m1"] = b"jpeg"
    saved = messaging.save_media("m1", "MNI-0001-pickup-../../etc/x")
    assert saved.startswith(str(tmp_path)) and "/" not in saved.removeprefix(str(tmp_path) + "/")
