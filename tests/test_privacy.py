"""Privacy notice, consent, the data clean-up, access and deletion requests; TIN; rider documents."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import messaging, privacy
from app.config import get_settings
from app.models import Conversation, Media, OrderStatus, Receipt, Rider, WebhookEvent
from app.whatsapp.fake import FakeProvider
from tests.conftest import T0, add_photo, advance, book, make_merchant, make_rider
from tests.test_bots import ADMIN, body, buttons, rows, say, tap

S = OrderStatus


@pytest.fixture(autouse=True)
def fake_provider():
    provider = FakeProvider()
    messaging.set_provider(provider)
    yield provider
    messaging.set_provider(None)


def test_the_privacy_notice_page(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "privacy_contact", "privacy@menions.test")
    page = client.get("/privacy")
    assert page.status_code == 200
    assert "Data Protection and Privacy Act" in page.text and "90 days" in page.text
    assert "privacy@menions.test" in page.text and "outside Uganda" in page.text


def test_customers_are_told_why_we_have_their_number(session):
    merchant = make_merchant(session)
    order = book(session, merchant)
    out = say(session, order.customer_phone, order.code)
    assert "only for this delivery: https://menions.test/privacy" in body(out, order.customer_phone)


def test_an_existing_rider_must_accept_before_working(session):
    rider = make_rider(session, privacy_accepted_at=None)
    rider.on_shift = False
    out = say(session, rider.phone, "hi")
    assert buttons(out, rider.phone) == [("r:agree", "I agree")]
    out = tap(session, rider.phone, "r:on")  # can't skip it
    assert buttons(out, rider.phone) == [("r:agree", "I agree")] and not rider.on_shift
    tap(session, rider.phone, "r:agree")
    assert rider.privacy_accepted_at is not None


def _finished_order(session, merchant, rider, tmp_path, at):
    order = advance(session, book(session, merchant), rider, S.DELIVERED)
    photo = tmp_path / f"{order.code}.jpg"
    photo.write_bytes(b"jpeg")
    session.add(Media(order_id=order.id, kind="pickup", url=str(photo)))
    order.notes = "call Sarah on arrival"
    order.updated_at = at
    session.flush()
    return order, photo


def test_clean_up_removes_old_personal_data_and_keeps_receipts(session, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path))
    merchant, rider = make_merchant(session), make_rider(session)
    old, old_photo = _finished_order(session, merchant, rider, tmp_path, T0)
    recent, recent_photo = _finished_order(session, merchant, rider, tmp_path, T0 + timedelta(days=80))
    open_order = book(session, merchant, at=T0)
    open_order.drop_text = "Green gate"
    session.add(WebhookEvent(provider_message_id="wamid.OLD", kind="message", raw={"from": "256772"}, received_at=T0))
    receipts_before = session.query(Receipt).count()
    session.flush()

    result = privacy.purge_old_data(session, T0 + timedelta(days=91))
    assert result["orders"] == 1 and result["photos"] == 3 and result["messages"] >= 1  # pickup, delivery, file
    assert (old.drop_lat, old.drop_text, old.notes, old.purged_at) == (None, None, None, T0 + timedelta(days=91))
    assert not old_photo.exists() and recent_photo.exists()
    assert session.scalar(select(Media.id).where(Media.order_id == old.id)) is None
    assert recent.drop_lat is not None and open_order.drop_text == "Green gate"
    assert old.customer_phone and old.customer_name  # the shop's own record of who it sent to
    assert session.get(WebhookEvent, session.scalar(select(WebhookEvent.id).where(
        WebhookEvent.provider_message_id == "wamid.OLD"))).raw == {"purged": True}  # fmt: skip
    assert session.query(Receipt).count() == receipts_before
    assert privacy.purge_old_data(session, T0 + timedelta(days=92))["orders"] == 0  # done once


def test_mydata_and_forget_for_a_customer(session, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path))
    merchant, rider = make_merchant(session), make_rider(session)
    order = book(session, merchant, customer_phone="0701555666")
    say(session, "+256701555666", order.code)
    reply = body(say(session, ADMIN, "/forget 0701555666"), ADMIN)
    assert "still in progress" in reply and order.customer_name != "(removed)"

    advance(session, order, rider, S.DELIVERED)
    add_photo(session, order, "delivery")
    reply = body(say(session, ADMIN, "/mydata 0701555666"), ADMIN)
    assert "Deliveries to this number: 1" in reply and "Receipts addressed to it: 1" in reply

    reply = body(say(session, ADMIN, "/forget 0701555666"), ADMIN)
    assert "1 delivery(ies)" in reply and "Kept: receipts" in reply
    assert (order.customer_name, order.customer_phone, order.drop_lat) == ("(removed)", "removed", None)
    assert session.get(Conversation, "+256701555666") is None
    receipt = session.execute(select(Receipt).where(Receipt.type == "delivery_customer")).scalar_one()
    assert receipt.recipient_phone == "+256701555666"  # sealed: receipts are never changed


def test_forget_closes_a_rider_account(session):
    rider = make_rider(session, permit_number="DL1", national_id="CM90001234ABCD", plate="UFA123B")
    phone = rider.phone
    reply = privacy.forget(session, phone, T0)
    assert "rider account" in reply
    assert (rider.name, rider.national_id, rider.active, rider.phone) == (
        "(removed)",
        None,
        False,
        f"removed-r{rider.id}",
    )


# --- TIN and rider documents ------------------------------------------------------------------------


def test_a_merchant_can_add_and_remove_a_tin(session):
    merchant = make_merchant(session)
    out = say(session, merchant.phone, "menu")
    assert "m:tin" in rows(out, merchant.phone)
    tap(session, merchant.phone, "m:tin")
    assert "10 digits" in body(say(session, merchant.phone, "12345"))
    out = say(session, merchant.phone, "1000 123 456")
    assert merchant.tin == "1000123456" and "saved" in body(out)
    tap(session, merchant.phone, "m:tin")
    tap(session, merchant.phone, "tin:remove")
    assert merchant.tin is None


def test_rider_documents_are_required_and_checked(session):
    usage = body(say(session, ADMIN, "/rider add 0772333444 Musa K"), ADMIN)
    assert "PERMIT-NO NIN PLATE" in usage
    assert "national ID" in body(say(session, ADMIN, "/rider add 0772333444 DL1234567 123 UFA123B Musa K"), ADMIN)
    assert "number plate" in body(
        say(session, ADMIN, "/rider add 0772333444 DL1234567 CM90001234ABCD KBZ123 Musa"), ADMIN
    )
    assert session.execute(select(Rider)).first() is None
    say(session, ADMIN, "/rider add 0772333444 dl1234567 cm90001234abcd ufa123b Musa K")
    rider = session.execute(select(Rider)).scalar_one()
    assert (rider.permit_number, rider.national_id, rider.plate, rider.documents_checked_by) == (
        "DL1234567", "CM90001234ABCD", "UFA123B", ADMIN,
    )  # fmt: skip


def test_a_failed_delivery_nobody_rescheduled_is_cleaned_up_and_forgettable(session):
    merchant, rider = make_merchant(session), make_rider(session)
    order = advance(session, book(session, merchant), rider, S.FAILED)
    order.updated_at = T0
    session.flush()
    assert privacy.purge_old_data(session, T0 + timedelta(days=91))["orders"] == 1
    assert order.drop_lat is None and order.purged_at is not None
    assert "1 delivery(ies)" in privacy.forget(session, order.customer_phone, T0)
