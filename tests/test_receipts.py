from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import receipts
from app.models import AdminAlert, OrderStatus, Receipt
from app.orders import transition
from app.receipts import GENESIS_HASH, Links, ReceiptError, Recipient, verify_chain
from tests.conftest import T0, advance, book, make_merchant, make_rider

DAY = date(2026, 11, 10)  # T0's date in Kampala


def all_receipts(session):
    return list(session.execute(select(Receipt).order_by(Receipt.id)).scalars())


def issue_mixed(session, count):
    """Issue at least `count` receipts of every kind the system produces."""
    merchant, rider = make_merchant(session, plan="monthly"), make_rider(session)
    i = 0
    while session.scalar(select(func.count()).select_from(Receipt)) < count:
        at = T0 + timedelta(minutes=i)
        match i % 5:
            case 0:
                book(session, merchant, at=at, cod_ugx=10_000 * (i % 3))
            case 1:
                receipts.record_rider_pay(session, rider, DAY, 20_000, "finance-1", at=at)
            case 2:
                receipts.record_cod_payout(session, merchant, DAY, 30_000 + i, f"MOMO-{i}", "finance-1", at=at)
            case 3:
                advance(session, book(session, merchant, at=at, cod_ugx=5_000), rider, OrderStatus.DELIVERED, at=at)
            case 4:
                advance(session, book(session, merchant, at=at), rider, OrderStatus.FAILED, at=at)
        i += 1


def test_numbers_are_sequential_and_the_chain_verifies_after_100_mixed_receipts(session):
    issue_mixed(session, 100)
    rows = all_receipts(session)
    assert len(rows) >= 100
    assert [r.number for r in rows] == [f"RCT-{n:06d}" for n in range(1, len(rows) + 1)]
    assert rows[0].prev_hash == GENESIS_HASH
    assert all(later.prev_hash == earlier.hash for earlier, later in zip(rows, rows[1:], strict=False))
    assert len({r.hash for r in rows}) == len(rows)
    assert len({r.type for r in rows}) >= 7
    assert verify_chain(session) is None


def test_a_rolled_back_receipt_leaves_no_gap(session):
    rider = make_rider(session)
    first = receipts.record_rider_pay(session, rider, DAY, 20_000, "finance-1", at=T0)
    savepoint = session.begin_nested()
    assert receipts.record_rider_pay(session, rider, DAY, 20_000, "finance-1", at=T0).number == "RCT-000002"
    savepoint.rollback()
    again = receipts.record_rider_pay(session, rider, DAY, 20_000, "finance-1", at=T0)
    assert (first.number, again.number) == ("RCT-000001", "RCT-000002")
    assert verify_chain(session) is None


def tamper(session, sql, **params):
    """Change a receipt directly in the database, going around the trigger the way someone
    with superuser access could."""
    session.execute(text("SET LOCAL session_replication_role = replica"))
    session.execute(text(sql), params)
    session.execute(text("SET LOCAL session_replication_role = origin"))


def test_verify_chain_finds_an_altered_snapshot(session):
    issue_mixed(session, 20)
    tamper(
        session,
        "UPDATE receipts SET snapshot = jsonb_set(snapshot, '{recipient,name}', '\"Someone Else\"') WHERE number = :n",
        n="RCT-000007",
    )
    broken = verify_chain(session)
    assert broken is not None and broken.number == "RCT-000007"
    assert "hash" in broken.reason


def test_verify_chain_finds_an_altered_amount_a_deleted_receipt_and_a_rewritten_hash(session):
    issue_mixed(session, 20)
    target = session.execute(select(Receipt).where(Receipt.amount_ugx.is_not(None)).limit(1)).scalar_one()
    tamper(session, "UPDATE receipts SET amount_ugx = amount_ugx + 1000 WHERE id = :id", id=target.id)
    assert verify_chain(session).number == target.number
    tamper(session, "UPDATE receipts SET amount_ugx = amount_ugx - 1000 WHERE id = :id", id=target.id)
    assert verify_chain(session) is None

    # Rewriting a receipt and recomputing its own hash still breaks the next link.
    tamper(session, "UPDATE receipts SET hash = repeat('a', 64) WHERE number = 'RCT-000010'")
    assert verify_chain(session).number == "RCT-000010"
    tamper(session, "DELETE FROM receipts WHERE number = 'RCT-000004'")
    assert verify_chain(session).number == "RCT-000005"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE receipts SET amount_ugx = 1",
        "UPDATE receipts SET snapshot = '{}'::jsonb",
        "UPDATE receipts SET hash = 'x', sent_at = now()",
        "DELETE FROM receipts",
        "TRUNCATE receipts CASCADE",
    ],
)
def test_the_database_rejects_update_delete_and_truncate(session, sql):
    receipts.record_rider_pay(session, make_rider(session), DAY, 20_000, "finance-1", at=T0)
    with pytest.raises(DBAPIError, match="append-only"):
        with session.begin_nested():
            session.execute(text(sql))
    assert session.scalar(select(Receipt.amount_ugx)) == 20_000


def test_sent_at_can_be_set_once_and_never_changed(session):
    receipt = receipts.record_rider_pay(session, make_rider(session), DAY, 20_000, "finance-1", at=T0)
    receipts.mark_sent(session, receipt, T0 + timedelta(minutes=1))
    assert receipt.sent_at == T0 + timedelta(minutes=1)
    for sql in ("UPDATE receipts SET sent_at = now()", "UPDATE receipts SET sent_at = NULL"):
        with pytest.raises(DBAPIError, match="append-only"):
            with session.begin_nested():
                session.execute(text(sql))
    assert verify_chain(session) is None  # sent_at is not part of the hash


def test_order_events_are_append_only_too(session):
    book(session, make_merchant(session))
    for sql in ("UPDATE order_events SET actor = 'admin'", "DELETE FROM order_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            with session.begin_nested():
                session.execute(text(sql))


def test_voiding_issues_a_new_receipt_and_keeps_the_original(session):
    rider = make_rider(session)
    original = receipts.record_rider_pay(session, rider, DAY, 20_000, "finance-1", at=T0)
    kept = (original.hash, dict(original.snapshot), original.amount_ugx)

    void = receipts.void_receipt(session, original, "Paid twice by mistake", "admin-1", at=T0 + timedelta(hours=1))
    session.refresh(original)
    assert (original.hash, original.snapshot, original.amount_ugx) == kept
    assert (void.type, void.voids_receipt_id, void.number) == ("void", original.id, "RCT-000002")
    assert void.recipient_phone == rider.phone and void.amount_ugx == 20_000
    assert ["Voids receipt", "RCT-000001"] in void.snapshot["lines"]
    assert ["Reason", "Paid twice by mistake"] in void.snapshot["lines"]
    assert verify_chain(session) is None

    with pytest.raises(ReceiptError, match="already voided"):
        receipts.void_receipt(session, original, "again", "admin-1")
    with pytest.raises(ReceiptError):
        receipts.void_receipt(session, void, "void the void", "admin-1")
    with pytest.raises(ReceiptError, match="reason"):
        receipts.void_receipt(session, void, "  ", "admin-1")


def test_a_receipt_links_to_exactly_one_thing(session):
    rider = make_rider(session)
    order = book(session, make_merchant(session))
    payout, _ = receipts.record_cod_payout(session, make_merchant(session), DAY, 5_000, "MOMO-1", "finance-1")
    to_rider = Recipient("rider", rider.phone, rider.name)
    for links in (Links(), Links(order_id=order.id, payout_id=payout.id)):
        with pytest.raises(DBAPIError, match="exactly_one_link"):
            with session.begin_nested():
                receipts.issue_receipt(session, "delivery_rider", to_rider, links, None, {})
    # Rider pay is the one type with no link, and EFRIS numbers belong only on plan purchases.
    assert receipts.issue_receipt(session, "rider_pay", to_rider, Links(), 20_000, {}).order_id is None
    with pytest.raises(DBAPIError, match="efris"):
        with session.begin_nested():
            receipts.issue_receipt(
                session, "rider_pay", to_rider, Links(), 1, {}, efris_fdn="F", efris_verification_code="V"
            )
    assert verify_chain(session) is None


# --- Cash handovers, payouts, rider pay --------------------------------------------------


def deliver_cash(session, merchant, rider, amount, at=T0):
    return advance(session, book(session, merchant, at=at, cod_ugx=amount), rider, OrderStatus.DELIVERED, at=at)


def test_cash_handover_receipts_come_once_both_sides_confirm(session):
    merchant, rider = make_merchant(session), make_rider(session)
    deliver_cash(session, merchant, rider, 85_000)
    deliver_cash(session, merchant, rider, 15_000)
    deliver_cash(session, merchant, rider, 0)
    deliver_cash(session, merchant, make_rider(session), 40_000)  # another rider's cash
    deliver_cash(session, merchant, rider, 9_000, at=T0 + timedelta(days=1))  # another day
    transition(session, book(session, merchant, cod_ugx=70_000), OrderStatus.CANCELLED, "admin", "a")  # never delivered

    assert receipts.expected_cash(session, rider.id, DAY) == 100_000
    handover = receipts.record_cash_handover(session, rider, DAY, 100_000, "dispatch-1", at=T0 + timedelta(hours=6))
    assert (handover.expected_ugx, handover.received_ugx, handover.difference_ugx) == (100_000, 100_000, 0)
    assert session.scalar(select(func.count()).select_from(Receipt).where(Receipt.handover_id == handover.id)) == 0
    assert session.scalar(select(func.count()).select_from(AdminAlert)) == 0

    issued = receipts.confirm_cash_handover(session, handover, at=T0 + timedelta(hours=7))
    assert [(r.type, r.recipient_role, r.amount_ugx) for r in issued] == [
        ("cash_handover", "rider", 100_000),
        ("cash_handover", "dispatcher", 100_000),
    ]
    assert issued[0].recipient_phone == rider.phone
    # Confirming again issues nothing more, and a closed handover can't be re-recorded.
    assert [r.id for r in receipts.confirm_cash_handover(session, handover)] == [r.id for r in issued]
    with pytest.raises(ReceiptError, match="already has receipts"):
        receipts.record_cash_handover(session, rider, DAY, 90_000, "dispatch-1")


def test_cash_handover_with_a_difference_records_an_admin_alert(session):
    merchant, rider = make_merchant(session), make_rider(session)
    deliver_cash(session, merchant, rider, 85_000)
    handover = receipts.record_cash_handover(session, rider, DAY, 80_000, "dispatch-1", notes="5k short", at=T0)
    assert handover.difference_ugx == -5_000
    alert = session.execute(select(AdminAlert)).scalar_one()
    assert (alert.kind, alert.rider_id) == ("cash_handover_difference", rider.id)
    assert alert.data["expected_ugx"] == 85_000 and alert.data["difference_ugx"] == -5_000


def test_the_dispatcher_can_fix_a_typo_until_the_rider_confirms(session):
    merchant, rider = make_merchant(session), make_rider(session)
    deliver_cash(session, merchant, rider, 85_000)
    handover = receipts.record_cash_handover(session, rider, DAY, 58_000, "dispatch-1", at=T0)  # typo
    fixed = receipts.record_cash_handover(session, rider, DAY, 85_000, "dispatch-1", at=T0)
    assert fixed.id == handover.id and fixed.difference_ugx == 0
    assert len(receipts.confirm_cash_handover(session, fixed)) == 2
    with pytest.raises(ReceiptError):
        receipts.record_cash_handover(session, rider, DAY, -1, "dispatch-1")
    with pytest.raises(ReceiptError):
        receipts.record_cash_handover(session, make_rider(session), DAY, 85_000.0, "dispatch-1")


def test_cod_payout_and_rider_pay_issue_receipts(session):
    merchant, rider = make_merchant(session), make_rider(session)
    payout, receipt = receipts.record_cod_payout(session, merchant, DAY, 100_000, "MOMO-771", "finance-1", at=T0)
    assert (receipt.type, receipt.payout_id, receipt.amount_ugx) == ("cod_payout", payout.id, 100_000)
    assert receipt.recipient_phone == merchant.phone
    with pytest.raises(ReceiptError, match="already recorded"):
        receipts.record_cod_payout(session, merchant, DAY, 100_000, "MOMO-771", "finance-1")
    for bad in (0, -5, 1000.0):
        with pytest.raises(ReceiptError):
            receipts.record_cod_payout(session, merchant, DAY, bad, "MOMO-772", "finance-1")

    pay = receipts.record_rider_pay(session, rider, DAY, 20_000, "finance-1", momo_reference="MOMO-900", at=T0)
    assert (pay.type, pay.recipient_role, pay.amount_ugx) == ("rider_pay", "rider", 20_000)
    assert (pay.order_id, pay.plan_request_id, pay.handover_id, pay.payout_id) == (None, None, None, None)


# --- Receipt page ------------------------------------------------------------------------


def test_receipt_page_needs_the_right_token(session, client):
    merchant, rider = make_merchant(session, shop_name="Akello's <Fashion> & Co"), make_rider(session)
    order = deliver_cash(session, merchant, rider, 85_000)
    receipt = session.execute(
        select(Receipt).where(Receipt.order_id == order.id, Receipt.type == "delivery_customer")
    ).scalar_one()

    page = client.get(f"/r/{receipt.number}", params={"k": receipt.view_token})
    assert page.status_code == 200
    assert receipt.number in page.text and "UGX 85,000" in page.text and receipt.hash in page.text
    assert "collected on behalf of Akello&#39;s &lt;Fashion&gt; &amp; Co" in page.text  # escaped, not raw HTML
    assert "<script" not in page.text.lower()
    assert "no-store" in page.headers["cache-control"]

    assert client.get(f"/r/{receipt.number}", params={"k": "wrong"}).status_code == 404
    assert client.get(f"/r/{receipt.number}").status_code == 404
    assert client.get("/r/RCT-999999", params={"k": receipt.view_token}).status_code == 404
    other = session.execute(select(Receipt).where(Receipt.id != receipt.id).limit(1)).scalar_one()
    assert client.get(f"/r/{receipt.number}", params={"k": other.view_token}).status_code == 404


def test_receipt_page_shows_when_a_receipt_was_voided(session, client):
    receipt = receipts.record_rider_pay(session, make_rider(session), DAY, 20_000, "finance-1", at=T0)
    void = receipts.void_receipt(session, receipt, "Wrong rider", "admin-1")
    page = client.get(f"/r/{receipt.number}", params={"k": receipt.view_token})
    assert "VOID" in page.text and void.number in page.text
