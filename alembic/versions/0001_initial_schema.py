"""Initial schema: every table, the two code sequences, and the append-only triggers.

Revision ID: 0001
Revises: 
Create Date: 2026-11-12 18:32:19.899817
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

# order_events and receipts are append-only. This is enforced here, in the database, so no
# code path (or person with a SQL prompt) can quietly change history.
APPEND_ONLY_FUNCTION = """
CREATE FUNCTION reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

# The single change a receipt allows: setting sent_at once, with every other column untouched.
RECEIPTS_GUARD_FUNCTION = """
CREATE FUNCTION receipts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.sent_at IS NULL
       AND NEW.sent_at IS NOT NULL
       AND (to_jsonb(NEW) - 'sent_at') = (to_jsonb(OLD) - 'sent_at') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'receipts is append-only: % is not allowed (void the receipt instead)', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$
"""


def upgrade() -> None:
    op.execute("CREATE SEQUENCE order_code_seq")
    op.execute("CREATE SEQUENCE plan_request_ref_seq")
    op.create_table('conversations',
    sa.Column('phone', sa.String(length=16), nullable=False),
    sa.Column('role', sa.String(length=24), nullable=False),
    sa.Column('step', sa.Text(), nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('last_inbound_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('merchant', 'merchant_pending', 'rider', 'customer', 'unknown')", name=op.f('ck_conversations_role')),
    sa.PrimaryKeyConstraint('phone', name=op.f('pk_conversations'))
    )
    op.create_table('merchants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('shop_name', sa.Text(), nullable=False),
    sa.Column('owner_name', sa.Text(), nullable=False),
    sa.Column('sells', sa.Text(), nullable=True),
    sa.Column('phone', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='PENDING', nullable=False),
    sa.Column('expected_daily_drops', sa.String(length=8), nullable=True),
    sa.Column('pickup_text', sa.Text(), nullable=True),
    sa.Column('pickup_lat', sa.Float(), nullable=True),
    sa.Column('pickup_lng', sa.Float(), nullable=True),
    sa.Column('plan', sa.String(length=32), nullable=True),
    sa.Column('plan_starts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('plan_ends', sa.DateTime(timezone=True), nullable=True),
    sa.Column('drops_left', sa.Integer(), nullable=True),
    sa.Column('priority_rider_ids', postgresql.ARRAY(sa.Integer()), server_default='{}', nullable=False),
    sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("expected_daily_drops IN ('1-2', '3-5', '6+')", name=op.f('ck_merchants_expected_daily_drops')),
    sa.CheckConstraint("plan IN ('daily_bronze', 'daily_silver', 'daily_gold', 'weekly_bronze', 'weekly_silver', 'weekly_gold', 'monthly')", name=op.f('ck_merchants_plan')),
    sa.CheckConstraint("status IN ('PENDING', 'ACTIVE', 'SUSPENDED')", name=op.f('ck_merchants_status')),
    sa.CheckConstraint('cardinality(priority_rider_ids) <= 2', name=op.f('ck_merchants_max_two_priority_riders')),
    sa.CheckConstraint('drops_left >= 0', name=op.f('ck_merchants_drops_left_not_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_merchants')),
    sa.UniqueConstraint('phone', name=op.f('uq_merchants_phone'))
    )
    op.create_table('riders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('phone', sa.String(length=16), nullable=False),
    sa.Column('on_shift', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_riders')),
    sa.UniqueConstraint('phone', name=op.f('uq_riders_phone'))
    )
    op.create_table('webhook_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider_message_id', sa.Text(), nullable=True),
    sa.Column('kind', sa.String(length=16), server_default='unknown', nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('processed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.CheckConstraint("kind IN ('message', 'status', 'unknown')", name=op.f('ck_webhook_events_kind')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_events')),
    sa.UniqueConstraint('provider_message_id', name=op.f('uq_webhook_events_provider_message_id'))
    )
    op.create_table('cash_handovers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('rider_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('expected_ugx', sa.Integer(), nullable=False),
    sa.Column('received_ugx', sa.Integer(), nullable=False),
    sa.Column('difference_ugx', sa.Integer(), nullable=False),
    sa.Column('rider_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dispatcher_confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dispatcher_id', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['rider_id'], ['riders.id'], name=op.f('fk_cash_handovers_rider_id_riders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_handovers')),
    sa.UniqueConstraint('rider_id', 'date', name=op.f('uq_cash_handovers_rider_id_date'))
    )
    op.create_table('cod_payouts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('merchant_id', sa.Integer(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('amount_ugx', sa.Integer(), nullable=False),
    sa.Column('momo_reference', sa.String(length=64), nullable=False),
    sa.Column('paid_by', sa.Text(), nullable=False),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('amount_ugx > 0', name=op.f('ck_cod_payouts_amount_positive')),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name=op.f('fk_cod_payouts_merchant_id_merchants')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cod_payouts')),
    sa.UniqueConstraint('momo_reference', name=op.f('uq_cod_payouts_momo_reference'))
    )
    op.create_index(op.f('ix_cod_payouts_merchant_id'), 'cod_payouts', ['merchant_id'], unique=False)
    op.create_table('orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('merchant_id', sa.Integer(), nullable=False),
    sa.Column('rider_id', sa.Integer(), nullable=True),
    sa.Column('customer_name', sa.Text(), nullable=False),
    sa.Column('customer_phone', sa.String(length=16), nullable=False),
    sa.Column('drop_text', sa.Text(), nullable=True),
    sa.Column('drop_lat', sa.Float(), nullable=True),
    sa.Column('drop_lng', sa.Float(), nullable=True),
    sa.Column('package', sa.Text(), nullable=False),
    sa.Column('cod_ugx', sa.Integer(), server_default='0', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('drop_code', sa.String(length=4), nullable=False),
    sa.Column('tracking_token', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('drop_used', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("drop_code ~ '^[0-9]{4}$'", name=op.f('ck_orders_drop_code_four_digits')),
    sa.CheckConstraint("status IN ('BOOKED', 'READY', 'ASSIGNED', 'PICKED_UP', 'DELIVERED', 'CANCELLED', 'FAILED')", name=op.f('ck_orders_status')),
    sa.CheckConstraint('cod_ugx >= 0', name=op.f('ck_orders_cod_not_negative')),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name=op.f('fk_orders_merchant_id_merchants')),
    sa.ForeignKeyConstraint(['rider_id'], ['riders.id'], name=op.f('fk_orders_rider_id_riders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('code', name=op.f('uq_orders_code')),
    sa.UniqueConstraint('tracking_token', name=op.f('uq_orders_tracking_token'))
    )
    op.create_index(op.f('ix_orders_customer_phone'), 'orders', ['customer_phone'], unique=False)
    op.create_index(op.f('ix_orders_merchant_id'), 'orders', ['merchant_id'], unique=False)
    op.create_index(op.f('ix_orders_rider_id'), 'orders', ['rider_id'], unique=False)
    op.create_index(op.f('ix_orders_status'), 'orders', ['status'], unique=False)
    op.create_table('plan_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('merchant_id', sa.Integer(), nullable=False),
    sa.Column('plan', sa.String(length=32), nullable=False),
    sa.Column('amount_ugx', sa.Integer(), nullable=False),
    sa.Column('momo_reference', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('paid_claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by', sa.Text(), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("plan IN ('daily_bronze', 'daily_silver', 'daily_gold', 'weekly_bronze', 'weekly_silver', 'weekly_gold', 'monthly')", name=op.f('ck_plan_requests_plan')),
    sa.CheckConstraint("status IN ('AWAITING_PAYMENT', 'PAYMENT_CLAIMED', 'APPROVED', 'REJECTED')", name=op.f('ck_plan_requests_status')),
    sa.CheckConstraint('amount_ugx > 0', name=op.f('ck_plan_requests_amount_positive')),
    sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id'], name=op.f('fk_plan_requests_merchant_id_merchants')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plan_requests')),
    sa.UniqueConstraint('momo_reference', name=op.f('uq_plan_requests_momo_reference'))
    )
    op.create_index(op.f('ix_plan_requests_merchant_id'), 'plan_requests', ['merchant_id'], unique=False)
    op.create_table('admin_alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('rider_id', sa.Integer(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_admin_alerts_order_id_orders')),
    sa.ForeignKeyConstraint(['rider_id'], ['riders.id'], name=op.f('fk_admin_alerts_rider_id_riders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_alerts'))
    )
    op.create_index(op.f('ix_admin_alerts_kind'), 'admin_alerts', ['kind'], unique=False)
    op.create_table('media',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('pickup', 'delivery')", name=op.f('ck_media_kind')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_media_order_id_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_media'))
    )
    op.create_index(op.f('ix_media_order_id'), 'media', ['order_id'], unique=False)
    op.create_table('order_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=24), server_default='transition', nullable=False),
    sa.Column('from_status', sa.String(length=16), nullable=True),
    sa.Column('to_status', sa.String(length=16), nullable=False),
    sa.Column('actor', sa.String(length=16), nullable=False),
    sa.Column('actor_id', sa.Text(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.CheckConstraint("actor IN ('merchant', 'rider', 'customer', 'admin', 'system')", name=op.f('ck_order_events_actor')),
    sa.CheckConstraint("kind IN ('transition', 'drop_code_wrong', 'drop_code_reset')", name=op.f('ck_order_events_kind')),
    sa.CheckConstraint("to_status IN ('BOOKED', 'READY', 'ASSIGNED', 'PICKED_UP', 'DELIVERED', 'CANCELLED', 'FAILED')", name=op.f('ck_order_events_to_status')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_events_order_id_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_events'))
    )
    op.create_index(op.f('ix_order_events_order_id'), 'order_events', ['order_id'], unique=False)
    op.create_table('receipts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('number', sa.String(length=16), nullable=False),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('recipient_role', sa.String(length=16), nullable=False),
    sa.Column('recipient_phone', sa.String(length=16), nullable=True),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('plan_request_id', sa.Integer(), nullable=True),
    sa.Column('handover_id', sa.Integer(), nullable=True),
    sa.Column('payout_id', sa.Integer(), nullable=True),
    sa.Column('amount_ugx', sa.Integer(), nullable=True),
    sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('efris_fdn', sa.Text(), nullable=True),
    sa.Column('efris_verification_code', sa.Text(), nullable=True),
    sa.Column('voids_receipt_id', sa.Integer(), nullable=True),
    sa.Column('view_token', sa.String(length=64), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(efris_fdn IS NULL AND efris_verification_code IS NULL) OR type = 'plan_purchase'", name=op.f('ck_receipts_efris_only_on_plan_purchase')),
    sa.CheckConstraint("(type = 'void') = (voids_receipt_id IS NOT NULL)", name=op.f('ck_receipts_void_points_at_a_receipt')),
    sa.CheckConstraint("num_nonnulls(order_id, plan_request_id, handover_id, payout_id) = 1 OR (num_nonnulls(order_id, plan_request_id, handover_id, payout_id) = 0 AND type IN ('rider_pay', 'void'))", name=op.f('ck_receipts_exactly_one_link')),
    sa.CheckConstraint("recipient_role IN ('merchant', 'customer', 'rider', 'dispatcher')", name=op.f('ck_receipts_recipient_role')),
    sa.CheckConstraint("type <> 'plan_purchase' OR (efris_fdn IS NOT NULL AND efris_verification_code IS NOT NULL)", name=op.f('ck_receipts_plan_purchase_has_efris')),
    sa.CheckConstraint("type IN ('plan_purchase', 'booking_confirmation', 'delivery_merchant', 'delivery_customer', 'delivery_rider', 'failed_delivery_merchant', 'cash_handover', 'cod_payout', 'rider_pay', 'void')", name=op.f('ck_receipts_type')),
    sa.ForeignKeyConstraint(['handover_id'], ['cash_handovers.id'], name=op.f('fk_receipts_handover_id_cash_handovers')),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_receipts_order_id_orders')),
    sa.ForeignKeyConstraint(['payout_id'], ['cod_payouts.id'], name=op.f('fk_receipts_payout_id_cod_payouts')),
    sa.ForeignKeyConstraint(['plan_request_id'], ['plan_requests.id'], name=op.f('fk_receipts_plan_request_id_plan_requests')),
    sa.ForeignKeyConstraint(['voids_receipt_id'], ['receipts.id'], name=op.f('fk_receipts_voids_receipt_id_receipts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_receipts')),
    sa.UniqueConstraint('number', name=op.f('uq_receipts_number')),
    sa.UniqueConstraint('voids_receipt_id', name=op.f('uq_receipts_voids_receipt_id'))
    )
    op.create_index('ix_receipts_order_id', 'receipts', ['order_id'], unique=False)
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(RECEIPTS_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER receipts_append_only BEFORE UPDATE OR DELETE ON receipts "
        "FOR EACH ROW EXECUTE FUNCTION receipts_guard()"
    )
    op.execute(
        "CREATE TRIGGER receipts_no_truncate BEFORE TRUNCATE ON receipts "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_change()"
    )
    op.execute(
        "CREATE TRIGGER order_events_append_only BEFORE UPDATE OR DELETE ON order_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_change()"
    )
    op.execute(
        "CREATE TRIGGER order_events_no_truncate BEFORE TRUNCATE ON order_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_change()"
    )


def downgrade() -> None:
    op.drop_index('ix_receipts_order_id', table_name='receipts')
    op.drop_table('receipts')
    op.drop_index(op.f('ix_order_events_order_id'), table_name='order_events')
    op.drop_table('order_events')
    op.drop_index(op.f('ix_media_order_id'), table_name='media')
    op.drop_table('media')
    op.drop_index(op.f('ix_admin_alerts_kind'), table_name='admin_alerts')
    op.drop_table('admin_alerts')
    op.drop_index(op.f('ix_plan_requests_merchant_id'), table_name='plan_requests')
    op.drop_table('plan_requests')
    op.drop_index(op.f('ix_orders_status'), table_name='orders')
    op.drop_index(op.f('ix_orders_rider_id'), table_name='orders')
    op.drop_index(op.f('ix_orders_merchant_id'), table_name='orders')
    op.drop_index(op.f('ix_orders_customer_phone'), table_name='orders')
    op.drop_table('orders')
    op.drop_index(op.f('ix_cod_payouts_merchant_id'), table_name='cod_payouts')
    op.drop_table('cod_payouts')
    op.drop_table('cash_handovers')
    op.drop_table('webhook_events')
    op.drop_table('riders')
    op.drop_table('merchants')
    op.drop_table('conversations')
    op.execute("DROP FUNCTION receipts_guard()")
    op.execute("DROP FUNCTION reject_change()")
    op.execute("DROP SEQUENCE plan_request_ref_seq")
    op.execute("DROP SEQUENCE order_code_seq")
