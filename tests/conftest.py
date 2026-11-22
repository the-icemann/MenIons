"""Test setup. Tests run against a real Postgres: a separate <database>_test, rebuilt from the
migration at the start of every run. Each test runs inside a transaction that is rolled back."""

import itertools
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
_base = make_url(os.environ.get("DATABASE_URL", "postgresql+psycopg://menions:menions@localhost:5432/menions"))
TEST_URL = _base.set(database=f"{_base.database}_test")

# Set before anything imports app.config. No test can reach a real WhatsApp provider.
os.environ.update(
    DATABASE_URL=TEST_URL.render_as_string(hide_password=False),
    ENV="test",
    WHATSAPP_PROVIDER="fake",
    WHATSAPP_WEBHOOK_SIGNATURE="none",
    WHATSAPP_VERIFY_TOKEN="verify-me",
    COMPANY_NAME="Men-Ions",
    ADMIN_PHONES="+256799999001",
    WHATSAPP_DISPLAY_NUMBER="256700000099",
    MOMO_PAY_NUMBER="0700000099",
    MOMO_PAY_NAME="Men-Ions Ltd",
    WHATSAPP_UPDATE_TEMPLATE="",
    PUBLIC_BASE_URL="https://menions.test",
    MEDIA_DIR="/tmp/menions-test-media",
    PAYMENTS_PROVIDER="manual",
    EFRIS_PROVIDER="manual",
    PAWAPAY_API_TOKEN="",
    PAWAPAY_CALLBACK_TOKEN="",
    DATA_RETENTION_DAYS="90",
    PRIVACY_CONTACT="",
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app import orders, plans  # noqa: E402
from app.models import Media, Merchant, MerchantStatus, Order, OrderStatus, Rider  # noqa: E402

# Tuesday 10 Nov 2026, 12:00 in Kampala.
T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
_counter = itertools.count(1)


@pytest.fixture(scope="session")
def engine():
    admin = create_engine(_base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_URL.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_URL.database}"'))
    admin.dispose()

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(TEST_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    yield session
    session.close()
    outer.rollback()
    connection.close()


@pytest.fixture
def client(session):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def fixture_payload(name: str) -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / f"{name}.json").read_text())


def phone() -> str:
    return f"+2567{next(_counter):08d}"


def make_merchant(session, *, plan="weekly_gold", status=MerchantStatus.ACTIVE, at=T0, **kwargs) -> Merchant:
    merchant = Merchant(
        shop_name=kwargs.pop("shop_name", "Kisementi Threads"),
        owner_name="Akello Grace",
        sells="Clothes",
        phone=kwargs.pop("phone", None) or phone(),
        status=status,
        **kwargs,
    )
    if plan:
        plans.activate_plan(merchant, plan, at)
    session.add(merchant)
    session.flush()
    return merchant


def make_rider(session, **kwargs) -> Rider:
    kwargs.setdefault("privacy_accepted_at", T0)  # an existing rider who has accepted the notice
    rider = Rider(name=kwargs.pop("name", "Musa K"), phone=phone(), on_shift=True, **kwargs)
    session.add(rider)
    session.flush()
    return rider


def book(session, merchant, *, at=T0, **kwargs) -> Order:
    order, _ = orders.create_order(
        session,
        merchant,
        customer_name=kwargs.pop("customer_name", "Sarah N"),
        customer_phone=kwargs.pop("customer_phone", None) or phone(),
        package=kwargs.pop("package", "2 dresses"),
        at=at,
        **kwargs,
    )
    return order


def add_photo(session, order, kind) -> None:
    session.add(Media(order_id=order.id, kind=kind, url=f"https://example.test/{order.code}-{kind}.jpg"))
    session.flush()


def advance(session, order, rider, to: str, *, at=T0) -> Order:
    """Walk an order forward along the happy path until it reaches `to`."""
    S = OrderStatus
    if order.status == S.BOOKED and to != S.BOOKED:
        order.drop_lat, order.drop_lng = 0.3476, 32.5825
        orders.transition(session, order, S.READY, "customer", at=at)
    if order.status == S.READY and to != S.READY:
        orders.transition(session, order, S.ASSIGNED, "rider", rider.id, at=at)
    if order.status == S.ASSIGNED and to != S.ASSIGNED:
        add_photo(session, order, "pickup")
        orders.transition(session, order, S.PICKED_UP, "rider", rider.id, at=at)
    if order.status == S.PICKED_UP and to == S.DELIVERED:
        add_photo(session, order, "delivery")
        orders.transition(session, order, S.DELIVERED, "rider", rider.id, {"drop_code": order.drop_code}, at=at)
    if order.status == S.PICKED_UP and to == S.FAILED:
        orders.transition(session, order, S.FAILED, "rider", rider.id, {"reason": "unreachable"}, at=at)
    assert order.status == to
    return order
