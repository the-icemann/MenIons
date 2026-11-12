from sqlalchemy import text

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.db import Base


def test_the_migration_matches_the_models(engine):
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_sequences_and_triggers_exist(session):
    sequences = set(session.execute(text("SELECT sequencename FROM pg_sequences")).scalars())
    assert {"order_code_seq", "plan_request_ref_seq"} <= sequences
    triggers = set(session.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).scalars())
    assert triggers == {
        "receipts_append_only", "receipts_no_truncate", "order_events_append_only", "order_events_no_truncate",
    }  # fmt: skip
