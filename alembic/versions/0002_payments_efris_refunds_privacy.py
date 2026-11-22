"""Automatic plan payments (pawaPay), EFRIS documents, refunds, privacy and rider documents.

- deposits, refunds: each attempt to collect or return a plan payment through pawaPay
- fiscal_documents: the EFRIS e-receipt (and credit note) owed for each plan sale. Plans approved
  before this migration already have their FDN on the receipt; they are backfilled as ISSUED.
- job_runs: when the worker last ran each daily job
- plan_requests.status gains REFUNDED; webhook_events.kind gains 'payment' (pawaPay callbacks)
- merchants: optional TIN, when the privacy notice was accepted
- riders: permit, NIN and plate as checked by the office, privacy acceptance
- orders.purged_at: when the data clean-up removed the customer's location and photos

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22 15:38:07.369291
"""
import sqlalchemy as sa

from alembic import op


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('job_runs',
    sa.Column('name', sa.String(length=48), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_job_runs'))
    )
    op.create_table('deposits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('deposit_id', sa.String(length=36), nullable=False),
    sa.Column('plan_request_id', sa.Integer(), nullable=False),
    sa.Column('payer_phone', sa.String(length=16), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('amount_ugx', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('failure_code', sa.Text(), nullable=True),
    sa.Column('provider_transaction_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('INITIATED', 'ACCEPTED', 'PROCESSING', 'IN_RECONCILIATION', 'COMPLETED', 'FAILED')", name=op.f('ck_deposits_status')),
    sa.CheckConstraint('amount_ugx > 0', name=op.f('ck_deposits_amount_positive')),
    sa.ForeignKeyConstraint(['plan_request_id'], ['plan_requests.id'], name=op.f('fk_deposits_plan_request_id_plan_requests')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deposits')),
    sa.UniqueConstraint('deposit_id', name=op.f('uq_deposits_deposit_id'))
    )
    op.create_index(op.f('ix_deposits_plan_request_id'), 'deposits', ['plan_request_id'], unique=False)
    op.create_index(op.f('ix_deposits_status'), 'deposits', ['status'], unique=False)
    op.create_table('fiscal_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('plan_request_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('reference', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('amount_ugx', sa.Integer(), nullable=False),
    sa.Column('fdn', sa.Text(), nullable=True),
    sa.Column('verification_code', sa.Text(), nullable=True),
    sa.Column('qr_code', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('alerted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('issued_by', sa.Text(), nullable=True),
    sa.Column('receipt_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('invoice', 'credit_note')", name=op.f('ck_fiscal_documents_kind')),
    sa.CheckConstraint("status <> 'ISSUED' OR (fdn IS NOT NULL AND verification_code IS NOT NULL)", name=op.f('ck_fiscal_documents_issued_has_fdn')),
    sa.CheckConstraint("status IN ('PENDING', 'ISSUED', 'CANCELLED')", name=op.f('ck_fiscal_documents_status')),
    sa.ForeignKeyConstraint(['plan_request_id'], ['plan_requests.id'], name=op.f('fk_fiscal_documents_plan_request_id_plan_requests')),
    sa.ForeignKeyConstraint(['receipt_id'], ['receipts.id'], name=op.f('fk_fiscal_documents_receipt_id_receipts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fiscal_documents')),
    sa.UniqueConstraint('plan_request_id', 'kind', name=op.f('uq_fiscal_documents_plan_request_id_kind')),
    sa.UniqueConstraint('reference', name=op.f('uq_fiscal_documents_reference'))
    )
    op.create_index(op.f('ix_fiscal_documents_status'), 'fiscal_documents', ['status'], unique=False)
    op.create_table('refunds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('refund_id', sa.String(length=36), nullable=False),
    sa.Column('plan_request_id', sa.Integer(), nullable=False),
    sa.Column('deposit_id', sa.Integer(), nullable=True),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('amount_ugx', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('failure_code', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('requested_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(method = 'pawapay') = (deposit_id IS NOT NULL)", name=op.f('ck_refunds_pawapay_has_deposit')),
    sa.CheckConstraint("method IN ('pawapay', 'manual')", name=op.f('ck_refunds_method')),
    sa.CheckConstraint("status IN ('INITIATED', 'ACCEPTED', 'PROCESSING', 'IN_RECONCILIATION', 'COMPLETED', 'FAILED')", name=op.f('ck_refunds_status')),
    sa.CheckConstraint('amount_ugx > 0', name=op.f('ck_refunds_amount_positive')),
    sa.ForeignKeyConstraint(['deposit_id'], ['deposits.id'], name=op.f('fk_refunds_deposit_id_deposits')),
    sa.ForeignKeyConstraint(['plan_request_id'], ['plan_requests.id'], name=op.f('fk_refunds_plan_request_id_plan_requests')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refunds')),
    sa.UniqueConstraint('refund_id', name=op.f('uq_refunds_refund_id'))
    )
    op.create_index(op.f('ix_refunds_plan_request_id'), 'refunds', ['plan_request_id'], unique=False)
    op.create_index(op.f('ix_refunds_status'), 'refunds', ['status'], unique=False)
    op.add_column('merchants', sa.Column('tin', sa.String(length=16), nullable=True))
    op.add_column('merchants', sa.Column('privacy_accepted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('riders', sa.Column('permit_number', sa.String(length=32), nullable=True))
    op.add_column('riders', sa.Column('national_id', sa.String(length=32), nullable=True))
    op.add_column('riders', sa.Column('plate', sa.String(length=16), nullable=True))
    op.add_column('riders', sa.Column('documents_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('riders', sa.Column('documents_checked_by', sa.Text(), nullable=True))
    op.add_column('riders', sa.Column('privacy_accepted_at', sa.DateTime(timezone=True), nullable=True))

    op.drop_constraint(op.f('ck_plan_requests_status'), 'plan_requests', type_='check')
    op.create_check_constraint(
        op.f('ck_plan_requests_status'), 'plan_requests',
        "status IN ('AWAITING_PAYMENT', 'PAYMENT_CLAIMED', 'APPROVED', 'REJECTED', 'REFUNDED')",
    )
    op.drop_constraint(op.f('ck_webhook_events_kind'), 'webhook_events', type_='check')
    op.create_check_constraint(
        op.f('ck_webhook_events_kind'), 'webhook_events', "kind IN ('message', 'status', 'payment', 'unknown')"
    )

    # Plans approved before this migration were approved with their FDN, on the receipt.
    op.execute("""
        INSERT INTO fiscal_documents
            (plan_request_id, kind, reference, status, amount_ugx, fdn, verification_code,
             issued_by, receipt_id, created_at, issued_at)
        SELECT p.id, 'invoice', p.momo_reference, 'ISSUED', p.amount_ugx, r.efris_fdn, r.efris_verification_code,
               p.approved_by, r.id, p.approved_at, r.issued_at
        FROM plan_requests p
        JOIN receipts r ON r.plan_request_id = p.id AND r.type = 'plan_purchase'
        WHERE p.status = 'APPROVED'
    """)


def downgrade() -> None:
    op.drop_constraint(op.f('ck_webhook_events_kind'), 'webhook_events', type_='check')
    op.create_check_constraint(
        op.f('ck_webhook_events_kind'), 'webhook_events', "kind IN ('message', 'status', 'unknown')"
    )
    op.drop_constraint(op.f('ck_plan_requests_status'), 'plan_requests', type_='check')
    op.create_check_constraint(
        op.f('ck_plan_requests_status'), 'plan_requests',
        "status IN ('AWAITING_PAYMENT', 'PAYMENT_CLAIMED', 'APPROVED', 'REJECTED')",
    )
    op.drop_column('riders', 'privacy_accepted_at')
    op.drop_column('riders', 'documents_checked_by')
    op.drop_column('riders', 'documents_checked_at')
    op.drop_column('riders', 'plate')
    op.drop_column('riders', 'national_id')
    op.drop_column('riders', 'permit_number')
    op.drop_column('orders', 'purged_at')
    op.drop_column('merchants', 'privacy_accepted_at')
    op.drop_column('merchants', 'tin')
    op.drop_index(op.f('ix_refunds_status'), table_name='refunds')
    op.drop_index(op.f('ix_refunds_plan_request_id'), table_name='refunds')
    op.drop_table('refunds')
    op.drop_index(op.f('ix_fiscal_documents_status'), table_name='fiscal_documents')
    op.drop_table('fiscal_documents')
    op.drop_index(op.f('ix_deposits_status'), table_name='deposits')
    op.drop_index(op.f('ix_deposits_plan_request_id'), table_name='deposits')
    op.drop_table('deposits')
    op.drop_table('job_runs')
