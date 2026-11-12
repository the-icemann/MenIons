"""POST /webhooks/whatsapp: verify, store, de-duplicate, normalize, route."""

import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import cloud_api_adapter
from app import router as event_router
from app.clock import utcnow
from app.config import Settings, get_settings
from app.db import get_session
from app.events import InboundEvent, StatusUpdate, UnknownEvent
from app.models import WebhookEvent
from app.whatsapp.signatures import get_verifier

log = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 1_000_000


async def raw_body(request: Request) -> bytes:
    return await request.body()


@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
def verify_subscription(
    mode: str = Query(default="", alias="hub.mode"),
    token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
):
    """Meta's verification handshake when the webhook URL is registered."""
    expected = settings.whatsapp_verify_token
    if mode == "subscribe" and expected and hmac.compare_digest(token.encode(), expected.encode()):
        return challenge
    raise HTTPException(status_code=403, detail="Verification failed")


def _dedupe_key(event: InboundEvent) -> str | None:
    """A message is identified by its wamid. A status update shares the wamid of the message
    it describes, so its key also carries the status (sent, delivered, read, failed)."""
    if isinstance(event, StatusUpdate):
        return f"{event.message_id}:{event.status}"
    return event.provider_message_id


def _kind(event: InboundEvent) -> str:
    if isinstance(event, StatusUpdate):
        return "status"
    return "unknown" if isinstance(event, UnknownEvent) else "message"


def _store(session: Session, raw: Any, *, key: str | None, kind: str, error: str | None = None) -> int | None:
    """Insert a webhook_events row. Returns its id, or None if this key was already stored."""
    stmt = (
        insert(WebhookEvent)
        .values(provider_message_id=key, kind=kind, raw=raw, received_at=utcnow(), processed=False, error=error)
        .on_conflict_do_nothing(index_elements=[WebhookEvent.provider_message_id])
        .returning(WebhookEvent.id)
    )
    return session.execute(stmt).scalar()


@router.post("/webhooks/whatsapp")
def receive(
    request: Request,
    body: bytes = Depends(raw_body),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    # 1. Signature first, over the raw bytes. A bad one stores nothing.
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Body too large")
    if not get_verifier(settings)(body, request.headers):
        raise HTTPException(status_code=401, detail="Bad signature")

    # 2. Store before doing anything else. One row per message or status in the payload,
    #    each holding the full request; a retried webhook hits the unique id and is skipped.
    try:
        raw = json.loads(body)
    except ValueError:
        _store(session, {"unparsed": body.decode("utf-8", "replace")}, key=None, kind="unknown", error="invalid JSON")
        session.commit()
        return {"status": "ok"}

    fresh: list[tuple[int, InboundEvent]] = []
    for event in cloud_api_adapter.parse(raw):
        row_id = _store(session, raw, key=_dedupe_key(event), kind=_kind(event))
        if row_id is None:
            log.info("duplicate webhook item ignored: %s", _dedupe_key(event))
        else:
            fresh.append((row_id, event))
    session.commit()

    # 3. Route each new event. A handler error is saved on the row and we still answer 200,
    #    so the provider doesn't retry forever; the stored row is there to replay.
    for row_id, event in fresh:
        try:
            with session.begin_nested():
                event_router.dispatch(session, event)
            session.execute(update(WebhookEvent).where(WebhookEvent.id == row_id).values(processed=True))
        except Exception as exc:  # noqa: BLE001
            log.exception("webhook event %s failed", row_id)
            session.execute(update(WebhookEvent).where(WebhookEvent.id == row_id).values(error=repr(exc)[:2000]))
        session.commit()
    return {"status": "ok"}
