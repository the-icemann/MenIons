"""POST /webhooks/pawapay: pawaPay tells us a deposit or refund reached a final status.

The callback is only a nudge. We store it, then ask pawaPay for the status ourselves and act
on that answer, as pawaPay's own docs advise: a callback (even a correctly signed one) never
starts a plan by itself. So the endpoint needs no signature check to be safe; the optional
PAWAPAY_CALLBACK_TOKEN only keeps strangers from making us call pawaPay.

pawaPay retries for about 25 minutes until it gets a 200, so we answer 503 when pawaPay
itself can't be reached for the status check, and 200 otherwise.
"""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import billing
from app.bots.effects import notify_admins
from app.clock import utcnow
from app.config import Settings, get_settings
from app.db import get_session
from app.messaging import deliver, discard_outbox, take_outbox
from app.models import AdminAlert, Deposit, Refund, WebhookEvent
from app.payments import get_gateway
from app.payments.base import GatewayError
from app.webhooks import raw_body

log = logging.getLogger(__name__)
router = APIRouter()
MAX_BODY_BYTES = 100_000


def _store(session: Session, raw: dict, key: str) -> None:
    session.execute(
        insert(WebhookEvent)
        .values(provider_message_id=key, kind="payment", raw=raw, received_at=utcnow(), processed=True)
        .on_conflict_do_nothing(index_elements=[WebhookEvent.provider_message_id])
    )


@router.post("/webhooks/pawapay")
def pawapay_callback(
    body: bytes = Depends(raw_body),
    k: str = Query(default=""),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    expected = settings.pawapay_callback_token
    if expected and not hmac.compare_digest(k.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Body too large")
    try:
        raw = json.loads(body)
        assert isinstance(raw, dict)
    except (ValueError, AssertionError):
        raise HTTPException(status_code=400, detail="Expected a JSON object") from None

    discard_outbox(session)
    deposit_id, refund_id = raw.get("depositId"), raw.get("refundId")
    key = f"pawapay:{deposit_id or refund_id or 'unknown'}:{raw.get('status')}"
    _store(session, raw, key)
    session.commit()

    now = utcnow()
    try:
        if refund_id:
            refund = session.execute(select(Refund).where(Refund.refund_id == str(refund_id))).scalar_one_or_none()
            if refund is not None:
                billing.settle_refund(session, refund, get_gateway().get_refund(refund.refund_id), now)
            else:
                _unknown(session, "refund", str(refund_id), raw)
        elif deposit_id:
            deposit = session.execute(select(Deposit).where(Deposit.deposit_id == str(deposit_id))).scalar_one_or_none()
            if deposit is not None:
                billing.check_deposit(session, deposit, now)
            else:
                _unknown(session, "deposit", str(deposit_id), raw)
    except GatewayError:
        session.rollback()
        discard_outbox(session)
        log.warning("pawaPay callback %s: status check failed; pawaPay will retry", key)
        return JSONResponse({"status": "retry"}, status_code=503)
    session.commit()
    deliver(session, take_outbox(session))
    return {"status": "ok"}


def _unknown(session: Session, what: str, transaction_id: str, raw: dict) -> None:
    """A payment we have no record of: possible if our transaction rolled back after pawaPay
    accepted it. A person must look."""
    session.add(AdminAlert(kind=f"unknown_{what}", data={"id": transaction_id, "status": raw.get("status")}))
    notify_admins(
        session,
        f"⚠️ pawaPay reported a {what} we have no record of: {transaction_id} ({raw.get('status')}, "
        f"{raw.get('currency', '')} {raw.get('amount', '')}). Look it up in the pawaPay dashboard.",
    )
