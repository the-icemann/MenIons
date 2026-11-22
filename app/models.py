"""All tables. Statuses are plain strings guarded by CHECK constraints."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MerchantStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class OrderStatus(StrEnum):
    BOOKED = "BOOKED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class Actor(StrEnum):
    MERCHANT = "merchant"
    RIDER = "rider"
    CUSTOMER = "customer"
    ADMIN = "admin"
    SYSTEM = "system"


class EventKind(StrEnum):
    TRANSITION = "transition"
    DROP_CODE_WRONG = "drop_code_wrong"
    DROP_CODE_RESET = "drop_code_reset"


class Role(StrEnum):
    MERCHANT = "merchant"
    MERCHANT_PENDING = "merchant_pending"
    RIDER = "rider"
    CUSTOMER = "customer"
    UNKNOWN = "unknown"


class PlanRequestStatus(StrEnum):
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PAYMENT_CLAIMED = "PAYMENT_CLAIMED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"


class DepositStatus(StrEnum):
    """pawaPay's deposit statuses, plus INITIATED: saved here before pawaPay is called."""

    INITIATED = "INITIATED"
    ACCEPTED = "ACCEPTED"
    PROCESSING = "PROCESSING"
    IN_RECONCILIATION = "IN_RECONCILIATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RefundStatus(StrEnum):
    INITIATED = "INITIATED"
    ACCEPTED = "ACCEPTED"
    PROCESSING = "PROCESSING"
    IN_RECONCILIATION = "IN_RECONCILIATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FiscalStatus(StrEnum):
    PENDING = "PENDING"
    ISSUED = "ISSUED"
    CANCELLED = "CANCELLED"


class FiscalKind(StrEnum):
    INVOICE = "invoice"  # the e-receipt (or e-invoice) for a plan sale
    CREDIT_NOTE = "credit_note"  # reverses it after a refund


class ReceiptType(StrEnum):
    PLAN_PURCHASE = "plan_purchase"
    BOOKING_CONFIRMATION = "booking_confirmation"
    DELIVERY_MERCHANT = "delivery_merchant"
    DELIVERY_CUSTOMER = "delivery_customer"
    DELIVERY_RIDER = "delivery_rider"
    FAILED_DELIVERY_MERCHANT = "failed_delivery_merchant"
    CASH_HANDOVER = "cash_handover"
    COD_PAYOUT = "cod_payout"
    RIDER_PAY = "rider_pay"
    VOID = "void"


# Plan keys live here (not in plans.py) so the CHECK constraints can use them.
PLAN_KEYS = (
    "daily_bronze",
    "daily_silver",
    "daily_gold",
    "weekly_bronze",
    "weekly_silver",
    "weekly_gold",
    "monthly",
)
EXPECTED_DAILY_DROPS = ("1-2", "3-5", "6+")
MEDIA_KINDS = ("pickup", "delivery")
RECIPIENT_ROLES = ("merchant", "customer", "rider", "dispatcher")
WEBHOOK_KINDS = ("message", "status", "payment", "unknown")
REFUND_METHODS = ("pawapay", "manual")


def _one_of(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


order_code_seq = Sequence("order_code_seq", metadata=Base.metadata)
plan_request_ref_seq = Sequence("plan_request_ref_seq", metadata=Base.metadata)

TZDateTime = DateTime(timezone=True)


class Merchant(Base):
    __tablename__ = "merchants"
    __table_args__ = (
        CheckConstraint(_one_of("status", MerchantStatus), name="status"),
        CheckConstraint(_one_of("expected_daily_drops", EXPECTED_DAILY_DROPS), name="expected_daily_drops"),
        CheckConstraint(_one_of("plan", PLAN_KEYS), name="plan"),
        CheckConstraint("drops_left >= 0", name="drops_left_not_negative"),
        CheckConstraint("cardinality(priority_rider_ids) <= 2", name="max_two_priority_riders"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_name: Mapped[str] = mapped_column(Text)
    owner_name: Mapped[str] = mapped_column(Text)
    sells: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(String(16), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=MerchantStatus.PENDING, server_default="PENDING")
    expected_daily_drops: Mapped[str | None] = mapped_column(String(8))
    pickup_text: Mapped[str | None] = mapped_column(Text)
    pickup_lat: Mapped[float | None] = mapped_column(Float)
    pickup_lng: Mapped[float | None] = mapped_column(Float)
    plan: Mapped[str | None] = mapped_column(String(32))
    plan_starts: Mapped[datetime | None] = mapped_column(TZDateTime)
    plan_ends: Mapped[datetime | None] = mapped_column(TZDateTime)
    drops_left: Mapped[int | None] = mapped_column(Integer)
    priority_rider_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, server_default="{}")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Optional. Only VAT-registered shops need it, so it never blocks sign-up.
    tin: Mapped[str | None] = mapped_column(String(16))
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class Rider(Base):
    __tablename__ = "riders"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(String(16), unique=True)
    on_shift: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Checked by the office before the rider is added (/rider add).
    permit_number: Mapped[str | None] = mapped_column(String(32))
    national_id: Mapped[str | None] = mapped_column(String(32))
    plate: Mapped[str | None] = mapped_column(String(16))
    documents_checked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    documents_checked_by: Mapped[str | None] = mapped_column(Text)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(_one_of("status", OrderStatus), name="status"),
        CheckConstraint("cod_ugx >= 0", name="cod_not_negative"),
        CheckConstraint("drop_code ~ '^[0-9]{4}$'", name="drop_code_four_digits"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    rider_id: Mapped[int | None] = mapped_column(ForeignKey("riders.id"), index=True)
    customer_name: Mapped[str] = mapped_column(Text)
    customer_phone: Mapped[str] = mapped_column(String(16), index=True)
    drop_text: Mapped[str | None] = mapped_column(Text)
    drop_lat: Mapped[float | None] = mapped_column(Float)
    drop_lng: Mapped[float | None] = mapped_column(Float)
    package: Mapped[str] = mapped_column(Text)
    cod_ugx: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text)
    drop_code: Mapped[str] = mapped_column(String(4))
    tracking_token: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    drop_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    # Set when the data clean-up removed the customer's pin, directions and the photos.
    purged_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class OrderEvent(Base):
    """Append-only (enforced by a database trigger)."""

    __tablename__ = "order_events"
    __table_args__ = (
        CheckConstraint(_one_of("kind", EventKind), name="kind"),
        CheckConstraint(_one_of("actor", Actor), name="actor"),
        CheckConstraint(_one_of("to_status", OrderStatus), name="to_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    # 'transition' rows are status changes. The other kinds record drop-code attempts;
    # for those, from_status and to_status are both the order's status at the time.
    kind: Mapped[str] = mapped_column(String(24), default=EventKind.TRANSITION, server_default="transition")
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class Media(Base):
    __tablename__ = "media"
    __table_args__ = (CheckConstraint(_one_of("kind", MEDIA_KINDS), name="kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (CheckConstraint(_one_of("role", Role), name="role"),)

    phone: Mapped[str] = mapped_column(String(16), primary_key=True)
    role: Mapped[str] = mapped_column(String(24))
    step: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    last_inbound_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class PlanRequest(Base):
    __tablename__ = "plan_requests"
    __table_args__ = (
        CheckConstraint(_one_of("status", PlanRequestStatus), name="status"),
        CheckConstraint(_one_of("plan", PLAN_KEYS), name="plan"),
        CheckConstraint("amount_ugx > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    plan: Mapped[str] = mapped_column(String(32))
    amount_ugx: Mapped[int] = mapped_column(Integer)
    momo_reference: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(24), default=PlanRequestStatus.AWAITING_PAYMENT)
    paid_claimed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())


class Deposit(Base):
    """One attempt to collect a plan payment through pawaPay. A failed attempt can be
    followed by another for the same plan request, each with its own deposit_id."""

    __tablename__ = "deposits"
    __table_args__ = (
        CheckConstraint(_one_of("status", DepositStatus), name="status"),
        CheckConstraint("amount_ugx > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deposit_id: Mapped[str] = mapped_column(String(36), unique=True)  # UUIDv4, ours, sent to pawaPay
    plan_request_id: Mapped[int] = mapped_column(ForeignKey("plan_requests.id"), index=True)
    payer_phone: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))  # MTN_MOMO_UGA | AIRTEL_OAPI_UGA
    amount_ugx: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), index=True)
    failure_code: Mapped[str | None] = mapped_column(Text)
    provider_transaction_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint(_one_of("status", RefundStatus), name="status"),
        CheckConstraint(_one_of("method", REFUND_METHODS), name="method"),
        CheckConstraint("amount_ugx > 0", name="amount_positive"),
        CheckConstraint("(method = 'pawapay') = (deposit_id IS NOT NULL)", name="pawapay_has_deposit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    refund_id: Mapped[str] = mapped_column(String(36), unique=True)
    plan_request_id: Mapped[int] = mapped_column(ForeignKey("plan_requests.id"), index=True)
    deposit_id: Mapped[int | None] = mapped_column(ForeignKey("deposits.id"))
    method: Mapped[str] = mapped_column(String(16))
    amount_ugx: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), index=True)
    failure_code: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class FiscalDocument(Base):
    """An EFRIS document we owe URA: the e-receipt for a plan sale, or the credit note that
    reverses it. Created PENDING the moment the money is in; ISSUED once URA's FDN is known.
    URA allows 24 hours between the sale and the upload."""

    __tablename__ = "fiscal_documents"
    __table_args__ = (
        CheckConstraint(_one_of("status", FiscalStatus), name="status"),
        CheckConstraint(_one_of("kind", FiscalKind), name="kind"),
        CheckConstraint(
            "status <> 'ISSUED' OR (fdn IS NOT NULL AND verification_code IS NOT NULL)", name="issued_has_fdn"
        ),
        UniqueConstraint("plan_request_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_request_id: Mapped[int] = mapped_column(ForeignKey("plan_requests.id"))
    kind: Mapped[str] = mapped_column(String(16))
    # What admins type in /fdn: the payment reference, plus "-CN" for a credit note.
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    amount_ugx: Mapped[int] = mapped_column(Integer)
    fdn: Mapped[str | None] = mapped_column(Text)
    verification_code: Mapped[str | None] = mapped_column(Text)
    qr_code: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    alerted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    issued_by: Mapped[str | None] = mapped_column(Text)
    receipt_id: Mapped[int | None] = mapped_column(ForeignKey("receipts.id"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    issued_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class JobRun(Base):
    """When each scheduled job last ran, so the worker runs daily jobs once a day."""

    __tablename__ = "job_runs"

    name: Mapped[str] = mapped_column(String(48), primary_key=True)
    last_run_at: Mapped[datetime] = mapped_column(TZDateTime)


class CashHandover(Base):
    __tablename__ = "cash_handovers"
    __table_args__ = (UniqueConstraint("rider_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rider_id: Mapped[int] = mapped_column(ForeignKey("riders.id"))
    date: Mapped[date] = mapped_column(Date)
    expected_ugx: Mapped[int] = mapped_column(Integer)
    received_ugx: Mapped[int] = mapped_column(Integer)
    difference_ugx: Mapped[int] = mapped_column(Integer)
    rider_confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    dispatcher_confirmed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    dispatcher_id: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class CodPayout(Base):
    __tablename__ = "cod_payouts"
    __table_args__ = (CheckConstraint("amount_ugx > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    amount_ugx: Mapped[int] = mapped_column(Integer)
    momo_reference: Mapped[str] = mapped_column(String(64), unique=True)
    paid_by: Mapped[str] = mapped_column(Text)
    paid_at: Mapped[datetime] = mapped_column(TZDateTime)


_RECEIPT_LINKS = "num_nonnulls(order_id, plan_request_id, handover_id, payout_id)"


class Receipt(Base):
    """Append-only and hash-chained. A trigger rejects UPDATE (except setting sent_at once),
    DELETE and TRUNCATE. Create rows only through app.receipts.issue_receipt."""

    __tablename__ = "receipts"
    __table_args__ = (
        CheckConstraint(_one_of("type", ReceiptType), name="type"),
        CheckConstraint(_one_of("recipient_role", RECIPIENT_ROLES), name="recipient_role"),
        CheckConstraint(
            f"{_RECEIPT_LINKS} = 1 OR ({_RECEIPT_LINKS} = 0 AND type IN ('rider_pay', 'void'))",
            name="exactly_one_link",
        ),
        CheckConstraint(
            "(efris_fdn IS NULL AND efris_verification_code IS NULL) OR type = 'plan_purchase'",
            name="efris_only_on_plan_purchase",
        ),
        CheckConstraint(
            "type <> 'plan_purchase' OR (efris_fdn IS NOT NULL AND efris_verification_code IS NOT NULL)",
            name="plan_purchase_has_efris",
        ),
        CheckConstraint("(type = 'void') = (voids_receipt_id IS NOT NULL)", name="void_points_at_a_receipt"),
        Index("ix_receipts_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(16), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    recipient_role: Mapped[str] = mapped_column(String(16))
    recipient_phone: Mapped[str | None] = mapped_column(String(16))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    plan_request_id: Mapped[int | None] = mapped_column(ForeignKey("plan_requests.id"))
    handover_id: Mapped[int | None] = mapped_column(ForeignKey("cash_handovers.id"))
    payout_id: Mapped[int | None] = mapped_column(ForeignKey("cod_payouts.id"))
    amount_ugx: Mapped[int | None] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    efris_fdn: Mapped[str | None] = mapped_column(Text)
    efris_verification_code: Mapped[str | None] = mapped_column(Text)
    voids_receipt_id: Mapped[int | None] = mapped_column(ForeignKey("receipts.id"), unique=True)
    view_token: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(TZDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (CheckConstraint(_one_of("kind", WEBHOOK_KINDS), name="kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # A message's wamid, or "<wamid>:<status>" for a status update. Unique, so a retried
    # webhook is stored and processed once.
    provider_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    received_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    error: Mapped[str | None] = mapped_column(Text)


class AdminAlert(Base):
    """Things a human must look at. Data only in milestone 1: nothing is sent yet."""

    __tablename__ = "admin_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    rider_id: Mapped[int | None] = mapped_column(ForeignKey("riders.id"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now())
