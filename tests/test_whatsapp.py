import json

import httpx
import pytest

from app.config import Settings
from app.whatsapp import build_provider
from app.whatsapp.base import MessageRejected
from app.whatsapp.cloud_api import CloudApiProvider, WhatsAppError
from app.whatsapp.fake import FakeProvider

PHONE = "+256772123456"
SECTIONS = [{"title": "Weekly", "rows": [{"id": "weekly_gold", "title": "Weekly Gold", "description": "20 drops"}]}]


def rows(n):
    return [{"title": "Plans", "rows": [{"id": f"row{i}", "title": f"Row {i}"} for i in range(n)]}]


# --- Limits: the fake and the real client apply the same ones -----------------------------


def cloud(handler=None, sleeps=None, **kwargs):
    handler = handler or (lambda request: httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]}))
    return CloudApiProvider(
        kwargs.pop("base_url", "https://api.kapso.ai/meta/whatsapp/v24.0"),
        "106540352242922",
        kwargs.pop("auth", {"X-API-Key": "kapso-key"}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


@pytest.mark.parametrize("provider", [FakeProvider(), cloud()], ids=["fake", "cloud"])
def test_limits_are_enforced_before_sending(provider):
    three = [("a", "Yes"), ("b", "No"), ("c", "Later")]
    assert provider.send_buttons(PHONE, "Confirm?", three)
    assert provider.send_list(PHONE, "Pick a plan", "Plans", rows(10))
    for bad in (
        lambda: provider.send_buttons(PHONE, "Confirm?", three + [("d", "Never")]),
        lambda: provider.send_buttons(PHONE, "Confirm?", [("a", "x" * 21)]),
        lambda: provider.send_buttons(PHONE, "Confirm?", []),
        lambda: provider.send_buttons(PHONE, "Confirm?", [("a", "Yes"), ("a", "No")]),
        lambda: provider.send_list(PHONE, "Pick a plan", "Plans", rows(11)),
        lambda: provider.send_list(PHONE, "Pick a plan", "x" * 21, rows(2)),
        lambda: provider.send_list(PHONE, "Pick", "Plans", [{"title": "T", "rows": [{"id": "r", "title": "x" * 25}]}]),
        lambda: provider.send_text(PHONE, ""),
        lambda: provider.send_text("0772123456x", "Hi"),
    ):
        with pytest.raises(MessageRejected):
            bad()
    assert provider.send_buttons(PHONE, "Confirm?", [("a", "x" * 20)])


def test_fake_provider_records_calls_and_sends_nothing():
    fake = FakeProvider()
    first = fake.send_text(PHONE, "Hello")
    second = fake.send_buttons(PHONE, "Confirm?", [("yes", "Yes")])
    with pytest.raises(MessageRejected):
        fake.send_buttons(PHONE, "Confirm?", [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")])
    assert first != second
    assert [(m.method, m.phone) for m in fake.sent] == [("send_text", PHONE), ("send_buttons", PHONE)]
    assert fake.sent[0].payload == {"text": "Hello"}
    fake.media["m1"] = b"jpeg-bytes"
    assert fake.download_media("m1") == b"jpeg-bytes"


# --- Request bodies -----------------------------------------------------------------------


def capture():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.SENT1"}]})

    return seen, handler


def sent_body(send):
    seen, handler = capture()
    assert send(cloud(handler)) == "wamid.SENT1"
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.kapso.ai/meta/whatsapp/v24.0/106540352242922/messages"
    body = json.loads(request.content)
    envelope = {key: body.pop(key) for key in ("messaging_product", "recipient_type", "to")}
    assert envelope == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "256772123456"}
    return body


def test_text_body():
    assert sent_body(lambda p: p.send_text(PHONE, "Your parcel is on the way")) == {
        "type": "text",
        "text": {"preview_url": False, "body": "Your parcel is on the way"},
    }


def test_buttons_body():
    body = sent_body(lambda p: p.send_buttons(PHONE, "Book this?", [("confirm", "Confirm"), ("change", "Change")]))
    assert body == {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Book this?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "confirm", "title": "Confirm"}},
                    {"type": "reply", "reply": {"id": "change", "title": "Change"}},
                ]
            },
        },
    }


def test_list_body():
    assert sent_body(lambda p: p.send_list(PHONE, "Pick a plan", "Plans", SECTIONS)) == {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "Pick a plan"},
            "action": {"button": "Plans", "sections": SECTIONS},
        },
    }


def test_location_request_body():
    assert sent_body(lambda p: p.send_location_request(PHONE, "Where should we deliver?")) == {
        "type": "interactive",
        "interactive": {
            "type": "location_request_message",
            "body": {"text": "Where should we deliver?"},
            "action": {"name": "send_location"},
        },
    }


def test_flow_body():
    body = sent_body(lambda p: p.send_flow(PHONE, "1234567890", "Register", "onboard-42", "WELCOME", body="Join us"))
    assert body == {
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": "Join us"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": "onboard-42",
                    "flow_id": "1234567890",
                    "flow_cta": "Register",
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": "WELCOME"},
                },
            },
        },
    }


def test_template_body():
    components = [{"type": "body", "parameters": [{"type": "text", "text": "MNI-0042"}]}]
    assert sent_body(lambda p: p.send_template(PHONE, "order_booked", "en", components)) == {
        "type": "template",
        "template": {"name": "order_booked", "language": {"code": "en"}, "components": components},
    }


def test_download_media_looks_up_the_url_then_fetches_it():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/MEDIA1"):
            return httpx.Response(200, json={"url": "https://media.example.test/file/abc", "mime_type": "image/jpeg"})
        return httpx.Response(200, content=b"\xff\xd8jpeg")

    assert cloud(handler).download_media("MEDIA1") == b"\xff\xd8jpeg"
    assert [str(r.url) for r in seen] == [
        "https://api.kapso.ai/meta/whatsapp/v24.0/MEDIA1",
        "https://media.example.test/file/abc",
    ]
    assert all(r.headers["x-api-key"] == "kapso-key" for r in seen)


# --- Provider is chosen by settings only ----------------------------------------------------


def test_provider_and_auth_header_come_from_settings():
    assert isinstance(build_provider(Settings(_env_file=None)), FakeProvider)

    kapso = build_provider(
        Settings(_env_file=None, whatsapp_provider="kapso", kapso_api_key="kapso-key", whatsapp_phone_number_id="111")
    )
    meta = build_provider(
        Settings(_env_file=None, whatsapp_provider="meta", meta_access_token="meta-token", whatsapp_phone_number_id="222")
    )
    urls = {}
    for name, provider in (("kapso", kapso), ("meta", meta)):
        seen, handler = capture()
        provider.client = httpx.Client(transport=httpx.MockTransport(handler))
        provider.send_text(PHONE, "Hi")
        urls[name] = (str(seen[0].url), seen[0].headers.get("x-api-key"), seen[0].headers.get("authorization"))
    assert urls["kapso"] == ("https://api.kapso.ai/meta/whatsapp/v24.0/111/messages", "kapso-key", None)
    assert urls["meta"] == ("https://graph.facebook.com/v24.0/222/messages", None, "Bearer meta-token")


# --- Retries --------------------------------------------------------------------------------


def scripted(*responses):
    calls, queue = [], list(responses)

    def handler(request):
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return calls, handler


OK = httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]})


@pytest.mark.parametrize("first", [httpx.Response(429, json={"error": {"code": 130429, "message": "Rate limit hit"}}),
                                   httpx.Response(500, text="oops"), httpx.Response(503), httpx.ConnectError("refused")])
def test_retries_on_429_5xx_and_connect_errors_with_backoff(first):
    sleeps = []
    calls, handler = scripted(first, httpx.Response(502), OK)
    assert cloud(handler, sleeps).send_text(PHONE, "Hi") == "wamid.OK"
    assert len(calls) == 3 and sleeps == [0.5, 2.0]


def test_gives_up_after_three_attempts():
    sleeps = []
    calls, handler = scripted(*[httpx.Response(503, json={"error": {"code": 131016, "message": "Service unavailable"}})] * 3)
    with pytest.raises(WhatsAppError) as info:
        cloud(handler, sleeps).send_text(PHONE, "Hi")
    assert len(calls) == 3 and info.value.status == 503 and info.value.code == 131016


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_never_retries_other_4xx(status):
    sleeps = []
    error = {"error": {"code": 131047, "message": "Re-engagement message", "type": "OAuthException"}}
    calls, handler = scripted(httpx.Response(status, json=error), OK)
    with pytest.raises(WhatsAppError, match="Re-engagement message") as info:
        cloud(handler, sleeps).send_text(PHONE, "Hi")
    assert len(calls) == 1 and sleeps == []
    assert (info.value.status, info.value.code) == (status, 131047)


def test_a_timeout_after_sending_is_not_retried():
    calls, handler = scripted(httpx.ReadTimeout("slow"), OK)
    with pytest.raises(httpx.ReadTimeout):
        cloud(handler).send_text(PHONE, "Hi")
    assert len(calls) == 1


def test_a_response_without_a_message_id_is_an_error():
    with pytest.raises(WhatsAppError, match="no message id"):
        cloud(lambda request: httpx.Response(200, json={"messages": []})).send_text(PHONE, "Hi")
