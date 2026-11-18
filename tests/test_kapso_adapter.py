import json
from datetime import UTC, datetime

from sqlalchemy import select

from app.events import LocationShared, MediaReceived, StatusUpdate, TextMessage, UnknownEvent
from app.kapso_adapter import is_kapso_payload, parse
from app.models import AdminAlert, Conversation, WebhookEvent
from tests.conftest import fixture_payload
from tests.test_webhooks import URL, sign, stored, use_settings

AT = datetime(2026, 11, 18, 14, 52, tzinfo=UTC)
SENDER = "+256772123456"


def one(payload):
    events = parse(payload, AT)
    assert len(events) == 1
    return events[0]


def test_real_sandbox_text():
    assert one(fixture_payload("kapso_text")) == TextMessage(
        provider_message_id="wamid.KTEXT001", received_at=AT, phone=SENDER, text="Hey"
    )


def test_inbound_status_field_is_not_mistaken_for_a_delivery_status():
    # Kapso marks inbound messages status=delivered; direction decides, not status.
    assert isinstance(one(fixture_payload("kapso_text")), TextMessage)


def test_sender_falls_back_to_the_conversation_phone():
    payload = fixture_payload("kapso_text")
    del payload["message"]["from"]
    assert one(payload).phone == SENDER


def test_real_sandbox_location():
    assert one(fixture_payload("kapso_location")) == LocationShared(
        provider_message_id="wamid.KLOC001",
        received_at=AT,
        phone=SENDER,
        lat=0.3136,
        lng=32.5811,
    )  # a real "current location" pin from the sandbox: no name or address


def test_image():
    payload = fixture_payload("kapso_text")
    msg = payload["message"]
    del msg["text"]
    msg.update(type="image", image={"id": "media_123", "caption": "parcel"})
    assert one(payload) == MediaReceived(
        provider_message_id="wamid.KTEXT001", received_at=AT, phone=SENDER, kind="image", media_id="media_123", caption="parcel"
    )


def test_outbound_statuses():
    assert one(fixture_payload("kapso_status_delivered")) == StatusUpdate(
        provider_message_id="wamid.KOUT001", received_at=AT, message_id="wamid.KOUT001", status="delivered", error=None
    )
    failed = one(fixture_payload("kapso_status_failed"))
    assert failed.status == "failed" and failed.error["code"] == 131047


def test_odd_input_becomes_unknown():
    payload = fixture_payload("kapso_text")
    payload["message"]["kapso"]["direction"] = "sideways"
    assert isinstance(one(payload), UnknownEvent)
    broken = fixture_payload("kapso_text")
    broken["message"]["text"] = None
    event = one(broken)
    assert isinstance(event, UnknownEvent) and event.provider_message_id == "wamid.KTEXT001"
    assert isinstance(one({"message": "nope"}), UnknownEvent)


def test_format_detection():
    assert is_kapso_payload(fixture_payload("kapso_text"))
    assert not is_kapso_payload(fixture_payload("text"))
    assert not is_kapso_payload([1])


# --- Through the webhook ---------------------------------------------------------------------


def test_signed_kapso_text_is_stored_routed_and_processed(session, client):
    use_settings(client, whatsapp_webhook_signature="kapso", kapso_webhook_secret="kapso-secret")
    body = json.dumps(fixture_payload("kapso_text")).encode()
    headers = {"content-type": "application/json", "X-Webhook-Signature": sign("kapso-secret", body)}
    assert client.post(URL, content=body, headers=headers).status_code == 200
    assert client.post(URL, content=body, headers=headers).status_code == 200  # Kapso retry
    (row,) = stored(session)
    assert (row.provider_message_id, row.kind, row.processed, row.error) == ("wamid.KTEXT001", "message", True, None)
    assert session.get(Conversation, SENDER).last_inbound_at is not None


def test_kapso_failed_status_alerts_an_admin(session, client):
    for name in ("kapso_status_delivered", "kapso_status_failed"):
        assert client.post(URL, json=fixture_payload(name)).status_code == 200
    assert [(r.provider_message_id, r.kind) for r in stored(session)] == [
        ("wamid.KOUT001:delivered", "status"),
        ("wamid.KOUT001:failed", "status"),
    ]
    alert = session.execute(select(AdminAlert)).scalar_one()
    assert alert.kind == "message_failed" and alert.data["error"]["code"] == 131047
    assert session.scalar(select(WebhookEvent.processed).where(WebhookEvent.provider_message_id.like("%failed"))) is True
