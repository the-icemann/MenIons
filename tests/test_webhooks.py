import hashlib
import hmac
import json

import pytest
from sqlalchemy import func, select

from app import router
from app.config import Settings, get_settings
from app.models import AdminAlert, Conversation, WebhookEvent
from tests.conftest import fixture_payload

URL = "/webhooks/whatsapp"


def stored(session):
    return list(session.execute(select(WebhookEvent).order_by(WebhookEvent.id)).scalars())


def use_settings(client, **kwargs):
    client.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, **kwargs)


def sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok", "database": "ok"}


def test_a_message_is_stored_routed_and_marked_processed(session, client):
    payload = fixture_payload("text")
    assert client.post(URL, json=payload).status_code == 200
    (row,) = stored(session)
    assert (row.provider_message_id, row.kind, row.processed, row.error) == ("wamid.TEXT001", "message", True, None)
    assert row.raw == payload
    conversation = session.get(Conversation, "+256772123456")
    assert conversation.role == "unknown" and conversation.last_inbound_at is not None


def test_the_same_message_id_twice_is_processed_once(session, client, monkeypatch):
    calls = []
    monkeypatch.setattr(router, "handle_unknown", lambda s, e, c: calls.append(e.provider_message_id))
    for _ in range(3):
        assert client.post(URL, json=fixture_payload("text")).status_code == 200
    assert calls == ["wamid.TEXT001"]
    assert len(stored(session)) == 1


def test_each_status_of_one_message_is_stored_and_a_failure_alerts_an_admin(session, client):
    for name in ("status_sent", "status_delivered", "status_read", "status_failed", "status_failed"):
        assert client.post(URL, json=fixture_payload(name)).status_code == 200
    rows = stored(session)
    assert [(r.provider_message_id, r.kind, r.processed) for r in rows] == [
        ("wamid.OUT001:sent", "status", True),
        ("wamid.OUT001:delivered", "status", True),
        ("wamid.OUT001:read", "status", True),
        ("wamid.OUT001:failed", "status", True),
    ]
    alert = session.execute(select(AdminAlert)).scalar_one()
    assert alert.kind == "message_failed"
    assert alert.data["message_id"] == "wamid.OUT001" and alert.data["error"]["code"] == 131047


@pytest.mark.parametrize("payload", [{}, {"object": "page", "entry": []}, {"entry": [{"changes": [{"value": {}}]}]}, [1, 2]])
def test_an_unrecognised_payload_is_stored_and_does_not_crash(session, client, payload):
    assert client.post(URL, json=payload).status_code == 200
    (row,) = stored(session)
    assert (row.kind, row.provider_message_id, row.raw, row.error) == ("unknown", None, payload, None)


def test_an_unknown_message_type_and_invalid_json_are_stored(session, client):
    assert client.post(URL, json=fixture_payload("unknown_type")).status_code == 200
    assert client.post(URL, content=b"{not json", headers={"content-type": "application/json"}).status_code == 200
    first, second = stored(session)
    assert (first.kind, first.provider_message_id, first.processed) == ("unknown", "wamid.REACT001", True)
    assert (second.kind, second.error, second.raw) == ("unknown", "invalid JSON", {"unparsed": "{not json"})


def test_a_handler_exception_still_returns_200_and_stores_the_error(session, client, monkeypatch):
    def explode(s, event, conversation):
        conversation.step = "half-finished work"  # must be rolled back with the failure
        s.flush()
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(router, "handle_unknown", explode)
    assert client.post(URL, json=fixture_payload("text")).status_code == 200
    (row,) = stored(session)
    assert row.processed is False and "handler blew up" in row.error
    assert row.raw == fixture_payload("text")
    assert session.get(Conversation, "+256772123456") is None

    # One bad event doesn't stop the next one in the same request.
    monkeypatch.undo()
    payload = fixture_payload("button_reply")
    payload["entry"][0]["changes"][0]["value"]["statuses"] = [{"id": "wamid.OUT9", "status": "sent"}]
    monkeypatch.setattr(router, "handle_unknown", explode)
    assert client.post(URL, json=payload).status_code == 200
    rows = {r.provider_message_id: r for r in stored(session)}
    assert rows["wamid.BTN001"].processed is False and rows["wamid.OUT9:sent"].processed is True


# --- Signatures ----------------------------------------------------------------------------


def test_meta_signature_valid_passes_and_tampered_gets_401_and_stores_nothing(session, client):
    use_settings(client, whatsapp_webhook_signature="meta", meta_app_secret="app-secret")
    body = json.dumps(fixture_payload("text")).encode()
    headers = {"content-type": "application/json", "X-Hub-Signature-256": "sha256=" + sign("app-secret", body)}

    tampered = body.replace(b"Hello", b"Jello")
    assert client.post(URL, content=tampered, headers=headers).status_code == 401
    assert client.post(URL, content=body, headers={"content-type": "application/json"}).status_code == 401
    wrong = {**headers, "X-Hub-Signature-256": "sha256=" + sign("other-secret", body)}
    assert client.post(URL, content=body, headers=wrong).status_code == 401
    no_prefix = {**headers, "X-Hub-Signature-256": sign("app-secret", body)}
    assert client.post(URL, content=body, headers=no_prefix).status_code == 401
    assert session.scalar(select(func.count()).select_from(WebhookEvent)) == 0
    assert session.scalar(select(func.count()).select_from(Conversation)) == 0

    assert client.post(URL, content=body, headers=headers).status_code == 200
    assert len(stored(session)) == 1


def test_kapso_signature(session, client):
    use_settings(client, whatsapp_webhook_signature="kapso", kapso_webhook_secret="kapso-secret")
    body = json.dumps(fixture_payload("location")).encode()
    good = {"content-type": "application/json", "X-Webhook-Signature": sign("kapso-secret", body)}
    assert client.post(URL, content=body + b" ", headers=good).status_code == 401
    assert client.post(URL, content=body, headers={"content-type": "application/json"}).status_code == 401
    # A Meta-style header is not accepted in kapso mode.
    meta_style = {"content-type": "application/json", "X-Hub-Signature-256": "sha256=" + sign("kapso-secret", body)}
    assert client.post(URL, content=body, headers=meta_style).status_code == 401
    assert stored(session) == []
    assert client.post(URL, content=body, headers=good).status_code == 200
    assert len(stored(session)) == 1


def test_oversized_body_is_refused(session, client):
    response = client.post(URL, content=b"x" * 1_000_001, headers={"content-type": "application/json"})
    assert response.status_code == 413 and stored(session) == []


def test_verification_handshake_returns_the_challenge_only_with_the_right_token(client):
    ok = client.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    assert ok.headers["content-type"].startswith("text/plain")
    for params in (
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": "verify-me", "hub.challenge": "1"},
        {"hub.challenge": "1"},
    ):
        assert client.get(URL, params=params).status_code == 403
    # With no token configured, nothing verifies (an empty token must not match an empty setting).
    use_settings(client, whatsapp_verify_token="")
    assert client.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}).status_code == 403
