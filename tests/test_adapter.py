from datetime import UTC, datetime

import pytest

from app.cloud_api_adapter import parse
from app.events import (
    ButtonReply,
    FlowSubmit,
    ListReply,
    LocationShared,
    MediaReceived,
    StatusUpdate,
    TextMessage,
    UnknownEvent,
)
from tests.conftest import fixture_payload

AT = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
SENDER = "+256772123456"


def one(name):
    events = parse(fixture_payload(name), AT)
    assert len(events) == 1
    return events[0]


def test_text():
    assert one("text") == TextMessage(
        provider_message_id="wamid.TEXT001", received_at=AT, phone=SENDER, text="Hello, I want to book a delivery"
    )


def test_button_reply():
    assert one("button_reply") == ButtonReply(
        provider_message_id="wamid.BTN001", received_at=AT, phone=SENDER, button_id="confirm_booking", title="Confirm"
    )


def test_template_quick_reply_button():
    assert one("template_button") == ButtonReply(
        provider_message_id="wamid.TBTN001", received_at=AT, phone=SENDER, button_id="accept_job_17", title="Accept"
    )


def test_list_reply():
    assert one("list_reply") == ListReply(
        provider_message_id="wamid.LIST001", received_at=AT, phone=SENDER, row_id="weekly_gold", title="Weekly Gold"
    )


def test_flow_reply():
    assert one("flow_reply") == FlowSubmit(
        provider_message_id="wamid.FLOW001",
        received_at=AT,
        phone=SENDER,
        flow_token="onboard-42",
        fields={"shop_name": "Kisementi Threads", "expected_daily_drops": "3-5"},
    )


def test_location():
    assert one("location") == LocationShared(
        provider_message_id="wamid.LOC001", received_at=AT, phone=SENDER,
        lat=0.3476, lng=32.5825, name="Garden City", address="Yusuf Lule Road, Kampala",
    )  # fmt: skip


def test_image():
    assert one("image") == MediaReceived(
        provider_message_id="wamid.IMG001", received_at=AT, phone=SENDER,
        kind="image", media_id="1479537139650973", caption="Parcel at the door",
    )  # fmt: skip


@pytest.mark.parametrize("status", ["sent", "delivered", "read", "failed"])
def test_each_status(status):
    event = one(f"status_{status}")
    assert isinstance(event, StatusUpdate)
    assert (event.message_id, event.provider_message_id, event.status) == ("wamid.OUT001", "wamid.OUT001", status)
    if status == "failed":
        assert event.error["code"] == 131047
    else:
        assert event.error is None


def test_an_unknown_message_type_becomes_unknown_event():
    event = one("unknown_type")
    assert isinstance(event, UnknownEvent)
    assert event.provider_message_id == "wamid.REACT001" and event.raw["type"] == "reaction"


@pytest.mark.parametrize("raw", [{}, [], None, "text", {"entry": "nope"}, {"entry": [{"changes": [{"value": {}}]}]}])
def test_an_unrecognised_payload_becomes_one_unknown_event(raw):
    (event,) = parse(raw, AT)
    assert isinstance(event, UnknownEvent) and event.provider_message_id is None and event.raw == raw


def test_a_malformed_message_does_not_lose_the_others():
    payload = fixture_payload("text")
    messages = payload["entry"][0]["changes"][0]["value"]["messages"]
    messages.insert(0, {"id": "wamid.BROKEN", "from": "256772123456", "type": "text"})  # no text body
    messages.append({"id": "wamid.BADFLOW", "from": "256772123456", "type": "interactive",
                     "interactive": {"type": "nfm_reply", "nfm_reply": {"response_json": "not json"}}})
    broken, good, bad_flow = parse(payload, AT)
    assert isinstance(broken, UnknownEvent) and broken.provider_message_id == "wamid.BROKEN"
    assert isinstance(good, TextMessage)
    assert isinstance(bad_flow, UnknownEvent) and bad_flow.provider_message_id == "wamid.BADFLOW"


def test_messages_and_statuses_in_one_payload_and_a_foreign_sender():
    payload = fixture_payload("text")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"][0]["from"] = "254712345678"
    value["statuses"] = fixture_payload("status_read")["entry"][0]["changes"][0]["value"]["statuses"]
    message, status = parse(payload, AT)
    assert message.phone == "+254712345678"
    assert isinstance(status, StatusUpdate) and status.status == "read"
