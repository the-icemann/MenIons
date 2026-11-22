"""Automatic plan payments (pawaPay), EFRIS documents, refunds and the daily check."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import billing, efris, messaging, payments, plans, worker
from app.config import get_settings
from app.messaging import take_outbox
from app.models import (
    AdminAlert,
    Deposit,
    FiscalDocument,
    JobRun,
    MerchantStatus,
    PlanRequest,
    Receipt,
    Refund,
)
from app.payments.base import AIRTEL, MTN, GatewayError
from app.payments.fake import FakeGateway
from app.payments.pawapay import PawaPay
from app.whatsapp.fake import FakeProvider
from tests.conftest import T0, make_merchant
from tests.test_bots import ADMIN, body, buttons, pick, rows, say, tap


@pytest.fixture(autouse=True)
def fake_provider():
    provider = FakeProvider()
    messaging.set_provider(provider)
    yield provider
    messaging.set_provider(None)


TOKEN = "test-callback-token-0123"
CALLBACK = f"/webhooks/pawapay?k={TOKEN}"


@pytest.fixture
def gateway(monkeypatch):
    gw = FakeGateway()
    payments.set_gateway(gw)
    monkeypatch.setattr(get_settings(), "payments_provider", "pawapay")
    monkeypatch.setattr(get_settings(), "pawapay_callback_token", TOKEN)
    yield gw
    payments.set_gateway(None)


@pytest.fixture(autouse=True)
def manual_efris():
    efris.set_provider(None)
    yield
    efris.set_provider(None)


def run(session, fn, *args, **kwargs):
    """Run domain code the way the webhook or worker does; return (result, queued messages)."""
    take_outbox(session)
    with session.begin_nested():
        result = fn(session, *args, **kwargs)
    return result, take_outbox(session)


def new_shop(session, phone=None):
    return make_merchant(session, plan=None, status=MerchantStatus.PENDING, phone=phone or "+256772000111")


def choose_and_pay(session, merchant, plan="weekly_gold"):
    say(session, merchant.phone, "hi")
    out = pick(session, merchant.phone, f"plan:{plan}")
    request = session.execute(
        select(PlanRequest).where(PlanRequest.merchant_id == merchant.id).order_by(PlanRequest.id.desc())
    ).scalars().first()  # fmt: skip
    return request, out


def only_live(session) -> Deposit:
    return session.execute(select(Deposit).where(Deposit.status == "ACCEPTED")).scalars().one()


def only_deposit(session, request) -> Deposit:
    return session.execute(select(Deposit).where(Deposit.plan_request_id == request.id)).scalars().one()


# --- Paying by PIN prompt ------------------------------------------------------------------------


def test_pay_by_pin_prompt_starts_the_plan_and_the_fdn_brings_the_receipt(session, gateway):
    merchant = new_shop(session)
    request, out = choose_and_pay(session, merchant)
    assert (f"pay:{request.id}", "Pay from this phone") in buttons(out, merchant.phone)
    assert "0772 000111" in body(out, merchant.phone)

    out = tap(session, merchant.phone, f"pay:{request.id}")
    deposit = only_deposit(session, request)
    sent = gateway.deposits[deposit.deposit_id]
    assert (sent["amount"], sent["phone"], sent["provider"]) == ("35000", "+256772000111", MTN)
    assert sent["message"] == "MenIons Weekly Gold" and deposit.status == "ACCEPTED"
    assert "MTN Mobile Money PIN" in body(out, merchant.phone)
    assert "Waiting for you to approve" in body(say(session, merchant.phone, "hello?"), merchant.phone)

    gateway.complete(deposit.deposit_id)
    result, out = run(session, billing.check_deposit, deposit, T0 + timedelta(minutes=2))
    assert result == "approved"
    assert merchant.status == MerchantStatus.ACTIVE and merchant.drops_left == 20
    assert request.status == "APPROVED" and request.approved_by == f"pawapay:{deposit.deposit_id}"
    assert "m:book" in rows(out, merchant.phone) and "EFRIS number follows" in body(out, merchant.phone)
    admin = body(out, ADMIN)
    assert "New shop now ACTIVE" in admin and f"/fdn {request.momo_reference}" in admin and "TIN: none" in admin
    doc = session.execute(select(FiscalDocument).where(FiscalDocument.plan_request_id == request.id)).scalar_one()
    assert doc.status == "PENDING" and session.query(Receipt).count() == 0

    # Callbacks and polls arriving again change nothing.
    assert run(session, billing.check_deposit, deposit)[0] == "final"

    out = say(session, ADMIN, f"/fdn {request.momo_reference} 3245678901 ABC123")
    assert doc.status == "ISSUED" and "Receipt RCT-" in body(out, ADMIN)
    receipt = session.get(Receipt, doc.receipt_id)
    lines = dict(receipt.snapshot["lines"])
    assert lines["Paid with"] == "MTN Mobile Money 0772 000111, transaction MP2611.1234.A00001"
    assert (receipt.efris_fdn, lines["EFRIS FDN"]) == ("3245678901", "3245678901")
    assert "/r/RCT-" in body(out, merchant.phone)
    assert "already has FDN" in body(say(session, ADMIN, f"/fdn {request.momo_reference} 999 X"), ADMIN)


def test_paying_from_another_number(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"paynum:{request.id}")
    assert "doesn't look like" in body(say(session, merchant.phone, "12345"))
    out = say(session, merchant.phone, "0701 234567")
    deposit = only_deposit(session, request)
    assert (deposit.payer_phone, deposit.provider) == ("+256701234567", AIRTEL)
    assert "Airtel Money PIN" in body(out, merchant.phone)


def test_a_failed_payment_explains_why_and_can_be_retried(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"paynum:{request.id}")
    say(session, merchant.phone, "0772 999888")  # a relative's phone
    first = only_deposit(session, request)
    gateway.fail(first.deposit_id, "INSUFFICIENT_BALANCE")
    result, out = run(session, billing.check_deposit, first)
    assert result == "failed" and first.failure_code == "INSUFFICIENT_BALANCE"
    assert "enough money" in body(out, merchant.phone)
    assert (f"retry:{first.id}", "Try again") in buttons(out, merchant.phone)
    assert (f"manual:{request.id}", "Pay by hand") in buttons(out, merchant.phone)
    assert merchant.status == MerchantStatus.PENDING and request.status == "AWAITING_PAYMENT"

    say(session, merchant.phone, "menu")  # the conversation is reset; the button still knows the phone
    tap(session, merchant.phone, f"retry:{first.id}")
    attempts = session.execute(select(Deposit).where(Deposit.plan_request_id == request.id)).scalars().all()
    assert len(attempts) == 2 and (attempts[1].status, attempts[1].payer_phone) == ("ACCEPTED", "+256772999888")
    gateway.fail(attempts[1].deposit_id, "PAYMENT_NOT_APPROVED")
    run(session, billing.check_deposit, attempts[1])

    tap(session, merchant.phone, f"pay:{request.id}")  # "Pay from this phone" is always the shop's own phone
    assert session.execute(select(Deposit).order_by(Deposit.id.desc())).scalars().first().payer_phone == merchant.phone

    gateway.fail(session.execute(select(Deposit).order_by(Deposit.id.desc())).scalars().first().deposit_id)
    run(session, billing.poll_deposits, T0 + timedelta(days=1))
    out = tap(session, merchant.phone, f"manual:{request.id}")
    assert request.momo_reference in body(out, merchant.phone) and "0700000099" in body(out, merchant.phone)


def test_rejected_at_once_and_one_attempt_at_a_time(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    gateway.reject_with = "PAYER_NOT_FOUND"
    out = tap(session, merchant.phone, f"pay:{request.id}")
    assert "isn't registered for Mobile Money" in body(out, merchant.phone)
    tap(session, merchant.phone, f"pay:{request.id}")
    out = tap(session, merchant.phone, f"pay:{request.id}")
    assert "Waiting for you to approve" in body(out, merchant.phone)
    assert buttons(out, merchant.phone) == [(f"chk:{only_live(session).id}", "Check payment")]
    out = tap(session, merchant.phone, f"paynum:{request.id}")  # can't start a second prompt either
    assert "Waiting for you to approve" in body(out, merchant.phone)
    assert len(gateway.deposits) == 1


def test_only_mtn_and_airtel(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"paynum:{request.id}")
    out = say(session, merchant.phone, "0790 123456")
    assert "MTN Mobile Money and Airtel Money only" in body(out) and not gateway.deposits


def test_gateway_down_then_never_received(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    gateway.down = True
    out = tap(session, merchant.phone, f"pay:{request.id}")
    assert "couldn't reach Mobile Money" in body(out, merchant.phone)
    deposit = only_deposit(session, request)
    assert deposit.status == "INITIATED"
    gateway.down = False
    assert (
        run(session, billing.poll_deposits, deposit.created_at + timedelta(seconds=90))[0] == 0
    )  # too soon to give up
    assert deposit.status == "INITIATED"
    _, out = run(session, billing.poll_deposits, deposit.created_at + timedelta(minutes=5))
    assert (deposit.status, deposit.failure_code) == ("FAILED", "NOT_FOUND")
    assert "no money was taken" in body(out, merchant.phone)
    assert (f"retry:{deposit.id}", "Try again") in buttons(out, merchant.phone)


def test_a_wrong_amount_does_not_start_the_plan(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"pay:{request.id}")
    deposit = only_deposit(session, request)
    gateway.complete(deposit.deposit_id, amount="3500")
    result, out = run(session, billing.check_deposit, deposit)
    assert result == "mismatch" and merchant.status == MerchantStatus.PENDING
    assert "NOT started" in body(out, ADMIN) and "hasn't started" in body(out, merchant.phone)
    assert session.scalar(select(AdminAlert.id).where(AdminAlert.kind == "payment_amount_mismatch"))


def test_manual_mode_is_unchanged(session):
    merchant = new_shop(session)
    request, out = choose_and_pay(session, merchant)
    assert (f"paid:{request.id}", "I've paid") in buttons(out, merchant.phone)
    out = tap(session, merchant.phone, f"paid:{request.id}")
    assert f"/approve {request.momo_reference}" in body(out, ADMIN)
    out = say(session, ADMIN, f"/approve {request.momo_reference}")
    assert merchant.status == MerchantStatus.ACTIVE and "EFRIS still owed" in body(out, ADMIN)
    assert "EFRIS number follows" in body(out, merchant.phone)
    assert request.momo_reference in body(say(session, ADMIN, "/fiscal"), ADMIN)


# --- The callback endpoint ------------------------------------------------------------------------


def test_callback_checks_with_pawapay_and_starts_the_plan(session, client, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"pay:{request.id}")
    deposit = only_deposit(session, request)

    # A callback claiming success means nothing until pawaPay itself says so.
    fake = {"depositId": deposit.deposit_id, "status": "COMPLETED", "amount": "35000", "currency": "UGX"}
    assert client.post(CALLBACK, json=fake).status_code == 200
    assert merchant.status == MerchantStatus.PENDING and deposit.status == "ACCEPTED"

    gateway.complete(deposit.deposit_id)
    assert client.post(CALLBACK, json=fake).status_code == 200
    session.refresh(merchant)
    assert merchant.status == MerchantStatus.ACTIVE and merchant.plan == "weekly_gold"


def test_callback_when_pawapay_is_unreachable_asks_for_a_retry(session, client, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"pay:{request.id}")
    deposit = only_deposit(session, request)
    gateway.down = True
    response = client.post(CALLBACK, json={"depositId": deposit.deposit_id, "status": "COMPLETED"})
    assert response.status_code == 503


def test_callback_for_a_deposit_we_never_made_alerts_admins(session, client, gateway):
    unknown = {"depositId": "0b5c5a3e-0000-4000-8000-000000000000", "status": "COMPLETED"}
    assert client.post(CALLBACK, json=unknown).status_code == 200
    assert client.post(CALLBACK, json=unknown).json()["duplicate"]  # a replay is ignored
    alerts = session.execute(select(AdminAlert).where(AdminAlert.kind == "unknown_deposit")).scalars().all()
    assert len(alerts) == 1


def test_callback_token_is_required(session, client, gateway, monkeypatch):
    assert client.post("/webhooks/pawapay", json={}).status_code == 403
    assert client.post("/webhooks/pawapay?k=wrong", json={}).status_code == 403
    assert client.post(CALLBACK, json={}).status_code == 200
    assert client.post(CALLBACK, content=b"nope").status_code == 400
    monkeypatch.setattr(get_settings(), "pawapay_callback_token", "")
    assert client.post("/webhooks/pawapay?k=", json={}).status_code == 403


def test_callbacks_are_off_when_pawapay_is(session, client):
    assert client.post("/webhooks/pawapay?k=anything", json={"depositId": "x"}).status_code == 404


# --- EFRIS ---------------------------------------------------------------------------------------


def _approved(session, gateway=None, at=T0):
    merchant = new_shop(session, phone=f"+25677{at.microsecond:07d}")
    request = plans.create_plan_request(session, merchant, "weekly_gold", at)
    approval = plans.approve_plan_request(session, request, "admin", at)
    return merchant, request, approval.fiscal


def test_automatic_efris_issues_and_sends_the_receipt(session):
    efris.set_provider(efris.FakeEfris())
    merchant, request, doc = _approved(session)
    merchant.tin = "1000123456"
    issued, out = run(session, billing.fiscalise_pending, T0)
    assert issued == 1 and doc.fdn == f"TEST-{request.momo_reference}" and doc.issued_by == "efris"
    receipt = session.get(Receipt, doc.receipt_id)
    assert dict(receipt.snapshot["lines"])["Buyer TIN"] == "1000123456"
    assert "/r/RCT-" in body(out, merchant.phone)


def test_efris_down_backs_off_and_admins_hear_before_the_deadline(session):
    class Down:
        automatic = True

        def issue(self, request):
            raise efris.EfrisUnavailable("URA timeout")

    efris.set_provider(Down())
    _, request, doc = _approved(session)
    run(session, billing.fiscalise_pending, T0)
    run(session, billing.fiscalise_pending, T0)  # not due again yet
    assert (doc.attempts, doc.last_error, doc.next_attempt_at) == (1, "URA timeout", T0 + timedelta(minutes=2))

    assert run(session, billing.alert_stale_fiscal, T0 + timedelta(hours=11))[0] == 0
    count, out = run(session, billing.alert_stale_fiscal, T0 + timedelta(hours=12))
    assert count == 1 and f"/fdn {request.momo_reference}" in body(out, ADMIN) and "URA timeout" in body(out, ADMIN)
    assert run(session, billing.alert_stale_fiscal, T0 + timedelta(hours=13))[0] == 0  # once


# --- Refunds -------------------------------------------------------------------------------------


def _paid_by_pin(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"pay:{request.id}")
    deposit = only_deposit(session, request)
    gateway.complete(deposit.deposit_id)
    run(session, billing.check_deposit, deposit)
    return merchant, request, deposit


def test_refunding_a_plan_after_its_fdn(session, gateway):
    merchant, request, deposit = _paid_by_pin(session, gateway)
    say(session, ADMIN, f"/fdn {request.momo_reference} FDN1 CODE1")
    out = say(session, ADMIN, f"/refund {request.momo_reference} shop closed")
    assert "sent to pawaPay" in body(out, ADMIN)
    refund = session.execute(select(Refund)).scalar_one()
    assert refund.status == "ACCEPTED" and gateway.refunds[refund.refund_id]["deposit_id"] == deposit.deposit_id
    assert "already being refunded" in body(say(session, ADMIN, f"/refund {request.momo_reference} again"), ADMIN)

    gateway.complete_refund(refund.refund_id)
    done, out = run(session, billing.poll_refunds, refund.created_at + timedelta(minutes=2))
    assert done == 1 and request.status == "REFUNDED"
    assert merchant.drops_left == 0 and "plan has ended" in body(out, merchant.phone)
    receipt = session.execute(
        select(Receipt).where(Receipt.plan_request_id == request.id, Receipt.type == "plan_purchase")
    ).scalar_one()
    assert session.scalar(select(Receipt.id).where(Receipt.voids_receipt_id == receipt.id))
    credit = session.execute(select(FiscalDocument).where(FiscalDocument.kind == "credit_note")).scalar_one()
    assert credit.reference == f"{request.momo_reference}-CN" and "Reverses FDN: FDN1" in body(out, ADMIN)
    say(session, ADMIN, f"/fdn {credit.reference} CN-FDN CN-CODE")
    assert credit.status == "ISSUED"


def test_refunding_before_the_fdn_cancels_the_efris_document(session, gateway):
    _, request, _ = _paid_by_pin(session, gateway)
    say(session, ADMIN, f"/refund {request.momo_reference} wrong plan")
    refund = session.execute(select(Refund)).scalar_one()
    gateway.complete_refund(refund.refund_id)
    run(session, billing.poll_refunds, refund.created_at + timedelta(minutes=2))
    docs = session.execute(select(FiscalDocument).where(FiscalDocument.plan_request_id == request.id)).scalars().all()
    assert [(d.kind, d.status) for d in docs] == [("invoice", "CANCELLED")]
    assert "cancelled" in body(say(session, ADMIN, f"/fdn {request.momo_reference} F C"), ADMIN)


def test_an_extra_payment_is_flagged_and_refunded_without_ending_the_plan(session, gateway):
    merchant, request, first = _paid_by_pin(session, gateway)
    second = Deposit(
        deposit_id="11111111-2222-4333-8444-555555555555", plan_request_id=request.id, payer_phone=merchant.phone,
        provider=MTN, amount_ugx=35_000, status="ACCEPTED", created_at=T0, updated_at=T0,
    )  # fmt: skip
    session.add(second)
    session.flush()
    gateway.deposits[second.deposit_id] = {**gateway.deposits[first.deposit_id]}
    result, out = run(session, billing.check_deposit, second)
    assert result == "extra" and f"/refund {request.momo_reference} extra" in body(out, ADMIN)
    assert any("no plan and no refund" in i for i in billing.reconcile(session, T0))

    say(session, ADMIN, f"/refund {request.momo_reference} extra paid twice")
    refund = session.execute(select(Refund)).scalar_one()
    assert refund.deposit_id == second.id
    gateway.complete_refund(refund.refund_id)
    _, out = run(session, billing.poll_refunds, refund.created_at + timedelta(minutes=2))
    assert request.status == "APPROVED" and merchant.drops_left == 20
    assert "extra" in body(out, merchant.phone)
    assert not any("no plan and no refund" in i for i in billing.reconcile(session, T0))


def test_a_hand_payment_is_refunded_by_hand(session):
    merchant, request, _ = _approved(session)
    out = say(session, ADMIN, f"/refund {request.momo_reference} duplicate shop")
    assert "refunded by hand" in body(out, ADMIN) and request.status == "REFUNDED"
    assert session.execute(select(Refund)).scalar_one().method == "manual"


# --- The daily check and the worker ----------------------------------------------------------------


def test_reconcile_finds_overdue_efris(session):
    _, request, _ = _approved(session)
    assert billing.reconcile(session, T0 + timedelta(hours=2)) == []
    (issue,) = billing.reconcile(session, T0 + timedelta(hours=25))
    assert request.momo_reference in issue and "OVERDUE" in issue
    assert "all agree" in body(say(session, ADMIN, "/reconcile"), ADMIN)  # the message is sent minutes after T0


def test_daily_summary(session):
    _, request, doc = _approved(session)
    billing.record_fdn(session, doc, "F1", "C1", "admin", T0)
    text = billing.daily_summary(session, T0 + timedelta(days=1))
    assert "1 plan sale(s), UGX 35,000" in text and "all agree" in text


def test_worker_runs_daily_jobs_once_a_day_after_seven(session, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "DAILY", [("daily_check", lambda s, now: calls.append(now))])
    monkeypatch.setattr(worker, "FREQUENT", [])
    early = T0.replace(hour=3)  # 06:00 in Kampala
    worker.tick(session, early)
    assert calls == []
    worker.tick(session, early + timedelta(hours=2))
    worker.tick(session, early + timedelta(hours=5))
    assert len(calls) == 1 and session.get(JobRun, "daily_check")
    worker.tick(session, early + timedelta(days=1, hours=2))
    assert len(calls) == 2


def test_a_failing_job_does_not_stop_the_others(session, monkeypatch):
    ran = []

    def broken(s, now):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "FREQUENT", [("broken", broken), ("fine", lambda s, now: ran.append(1))])
    monkeypatch.setattr(worker, "DAILY", [])
    worker.tick(session, T0)
    assert ran == [1]


# --- The pawaPay client (HTTP shapes) --------------------------------------------------------------


def _client(handler):
    return PawaPay("https://api.sandbox.pawapay.io", "tok", httpx.Client(
        base_url="https://api.sandbox.pawapay.io", transport=httpx.MockTransport(handler)
    ))  # fmt: skip


def test_pawapay_initiate_sends_v2_deposit(session):
    seen = {}

    def handler(request):
        seen["path"], seen["json"] = request.url.path, __import__("json").loads(request.content)
        return httpx.Response(200, json={"depositId": seen["json"]["depositId"], "status": "ACCEPTED"})

    result = _client(handler).initiate_deposit("d-1", 35000, "+256772000111", MTN, "MenIons Weekly Gold")
    assert result.status == "ACCEPTED" and seen["path"] == "/v2/deposits"
    assert seen["json"] == {
        "depositId": "d-1", "amount": "35000", "currency": "UGX", "customerMessage": "MenIons Weekly Gold",
        "payer": {"type": "MMO", "accountDetails": {"phoneNumber": "256772000111", "provider": MTN}},
    }  # fmt: skip


def test_pawapay_status_found_not_found_and_errors(session):
    found = {"status": "FOUND", "data": {
        "depositId": "d-1", "status": "FAILED", "amount": "35000", "currency": "UGX",
        "payer": {"type": "MMO", "accountDetails": {"phoneNumber": "256772000111", "provider": MTN}},
        "failureReason": {"failureCode": "PAYMENT_NOT_APPROVED", "failureMessage": "no PIN"},
    }}  # fmt: skip
    tx = _client(lambda r: httpx.Response(200, json=found)).get_deposit("d-1")
    assert (tx.found, tx.status, tx.failure_code, tx.provider) == (True, "FAILED", "PAYMENT_NOT_APPROVED", MTN)
    assert not _client(lambda r: httpx.Response(200, json={"status": "NOT_FOUND"})).get_deposit("d-1").found
    with pytest.raises(GatewayError):
        _client(lambda r: httpx.Response(500, json={})).get_deposit("d-1")
    with pytest.raises(GatewayError):
        _client(lambda r: httpx.Response(200, json={"status": "WEIRD"})).initiate_deposit(
            "d", 1, "+256772000111", MTN, "x"
        )


def test_settings_refuse_unsafe_payment_config():
    from app.config import Settings

    with pytest.raises(ValueError, match="PAWAPAY_API_TOKEN"):
        Settings(payments_provider="pawapay", pawapay_api_token="", pawapay_callback_token="x" * 20)
    with pytest.raises(ValueError, match="PAWAPAY_CALLBACK_TOKEN"):
        Settings(payments_provider="pawapay", pawapay_api_token="t", pawapay_callback_token="short")
    with pytest.raises(ValueError, match="EFRIS_PROVIDER=fake"):
        Settings(env="production", efris_provider="fake", whatsapp_webhook_signature="kapso", kapso_webhook_secret="x")


# --- Review fixes: one payment at a time, manual mode, batches --------------------------------------


def test_no_second_plan_while_a_prompt_is_pending_and_old_choices_close(session, gateway):
    merchant = new_shop(session)
    first, _ = choose_and_pay(session, merchant, "weekly_gold")
    tap(session, merchant.phone, f"pay:{first.id}")
    out = pick(session, merchant.phone, "plan:daily_bronze")  # "Change plan" mid-payment
    assert "Waiting for you to approve" in body(out, merchant.phone)
    assert session.query(PlanRequest).filter_by(merchant_id=merchant.id).count() == 1

    deposit = only_deposit(session, first)
    gateway.fail(deposit.deposit_id, "PAYMENT_NOT_APPROVED")
    run(session, billing.check_deposit, deposit)
    second, _ = choose_and_pay(session, merchant, "daily_bronze")
    assert second.id != first.id and first.status == "REJECTED" and first.approved_by == "superseded"
    out = tap(session, merchant.phone, f"pay:{first.id}")  # an old button can't charge the old plan
    assert "already rejected" in body(out, merchant.phone) and len(gateway.deposits) == 1


def test_check_payment_button(session, gateway):
    merchant = new_shop(session)
    request, _ = choose_and_pay(session, merchant)
    tap(session, merchant.phone, f"pay:{request.id}")
    deposit = only_deposit(session, request)
    out = tap(session, merchant.phone, f"chk:{deposit.id}")
    assert "Waiting for you to approve" in body(out, merchant.phone)
    gateway.complete(deposit.deposit_id)
    out = tap(session, merchant.phone, f"chk:{deposit.id}")
    assert merchant.status == MerchantStatus.ACTIVE and "Payment received" in body(out, merchant.phone)


def test_manual_mode_never_reaches_pawapay(session, monkeypatch):
    """Including a merchant whose saved step is the pre-upgrade "plan:pay" (the old manual step)."""
    broken = FakeGateway()
    broken.down = True
    payments.set_gateway(broken)
    try:
        merchant = new_shop(session)
        request = plans.create_plan_request(session, merchant, "weekly_gold", T0)
        say(session, merchant.phone, "hi")
        from app.models import Conversation

        conversation = session.get(Conversation, merchant.phone)
        conversation.step, conversation.context = "plan:pay", {"request_id": request.id}
        session.flush()
        out = say(session, merchant.phone, "how do I pay?")
        assert "I've paid" in str(buttons(out, merchant.phone)) and request.momo_reference in body(out)
        out = tap(session, merchant.phone, f"pay:{request.id}")
        assert "I've paid" in str(buttons(out, merchant.phone))
        assert session.query(Deposit).count() == 0
    finally:
        payments.set_gateway(None)


def test_batches_checkpoint_after_each_item_and_survive_a_bad_one(session, gateway, monkeypatch):
    merchants = [new_shop(session, phone=f"+25677200011{i}") for i in range(3)]
    deposits = []
    for m in merchants:
        request, _ = choose_and_pay(session, m)
        tap(session, m.phone, f"pay:{request.id}")
        deposits.append(only_deposit(session, request))
        gateway.complete(deposits[-1].deposit_id)
    real = billing.settle_deposit

    def flaky(s, deposit, tx, at=None):
        if deposit.id == deposits[1].id:
            raise RuntimeError("boom")
        return real(s, deposit, tx, at)

    monkeypatch.setattr(billing, "settle_deposit", flaky)
    checkpoints = []
    done = billing.poll_deposits(session, T0 + timedelta(minutes=5), lambda s: checkpoints.append(1))
    assert done == 2 and len(checkpoints) == 3
    assert [m.status for m in merchants] == [MerchantStatus.ACTIVE, MerchantStatus.PENDING, MerchantStatus.ACTIVE]


def test_an_fdn_that_cannot_be_recorded_alerts_and_is_never_reissued(session):
    efris.set_provider(efris.FakeEfris())
    _, request, doc = _approved(session)
    other_merchant = new_shop(session, phone="+256772000999")
    other = plans.create_plan_request(session, other_merchant, "daily_gold", T0)
    other_doc = plans.approve_plan_request(session, other, "admin", T0).fiscal
    real = billing.record_fdn

    def refuse(s, d, *args, **kwargs):
        if d.id == doc.id:
            raise billing.BillingError("cancelled meanwhile")
        return real(s, d, *args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(billing, "record_fdn", refuse):
        issued, out = run(session, billing.fiscalise_pending, T0)
    assert issued == 1 and other_doc.status == "ISSUED"
    session.refresh(doc)
    assert doc.status == "PENDING" and doc.next_attempt_at is None and "TEST-" in doc.last_error
    assert "couldn't record it" in body(out, ADMIN)
    assert run(session, billing.fiscalise_pending, T0 + timedelta(hours=1))[0] == 0
