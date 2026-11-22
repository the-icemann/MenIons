"""The background worker: python -m app.worker (the "worker" service in docker-compose.yml).

Every 30 seconds:
  - check payments and refunds pawaPay hasn't called back about (backstop for lost callbacks)
  - issue pending EFRIS documents, when the EFRIS provider is automatic
  - alert admins about FDNs waiting longer than EFRIS_ALERT_HOURS
Once a day, after 07:00 Kampala time:
  - send the admins the daily check (sales, EFRIS, anything that doesn't add up)
  - delete personal data past DATA_RETENTION_DAYS

Each job runs in its own transaction, so one failing job doesn't stop the others. Running
two workers is safe (rows are locked while settled) but pointless.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import billing, payments, privacy
from app.bots.effects import notify_admins
from app.clock import KAMPALA, kampala_date, utcnow
from app.db import get_sessionmaker
from app.messaging import deliver, discard_outbox, take_outbox
from app.models import JobRun

log = logging.getLogger("app.worker")
TICK_SECONDS = 30
DAILY_HOUR = 7  # Kampala time


def _due_today(session: Session, name: str, now: datetime) -> bool:
    if now.astimezone(KAMPALA).hour < DAILY_HOUR:
        return False
    last = session.execute(select(JobRun.last_run_at).where(JobRun.name == name)).scalar()
    return last is None or kampala_date(last) < kampala_date(now)


def _mark_ran(session: Session, name: str, now: datetime) -> None:
    session.execute(
        insert(JobRun).values(name=name, last_run_at=now)
        .on_conflict_do_update(index_elements=[JobRun.name], set_={"last_run_at": now})
    )  # fmt: skip


def daily_check(session: Session, now: datetime) -> None:
    notify_admins(session, billing.daily_summary(session, now))


def daily_cleanup(session: Session, now: datetime) -> None:
    privacy.purge_old_data(session, now)


def commit_and_send(session: Session) -> None:
    """Between the items of a batch: keep what is done, send its messages, release locks."""
    session.commit()
    deliver(session, take_outbox(session))


FREQUENT: list[tuple[str, Callable[[Session, datetime], object]]] = [
    ("poll_deposits", lambda s, now: billing.poll_deposits(s, now, commit_and_send)),
    ("poll_refunds", lambda s, now: billing.poll_refunds(s, now, commit_and_send)),
    ("fiscalise", lambda s, now: billing.fiscalise_pending(s, now, commit_and_send)),
    ("efris_alerts", billing.alert_stale_fiscal),
]
DAILY: list[tuple[str, Callable[[Session, datetime], object]]] = [
    ("daily_check", daily_check),
    ("data_cleanup", daily_cleanup),
]


def run_job(session: Session, name: str, job: Callable[[Session, datetime], object], now: datetime) -> bool:
    """Run one job in its own transaction, then send what it queued. False if it failed."""
    discard_outbox(session)
    try:
        job(session, now)
        session.commit()
    except Exception:  # noqa: BLE001 - one job must not stop the others
        session.rollback()
        discard_outbox(session)
        log.exception("job %s failed", name)
        return False
    deliver(session, take_outbox(session), now)
    return True


def tick(session: Session, now: datetime | None = None) -> None:
    now = now or utcnow()
    for name, job in FREQUENT:
        if name.startswith("poll_") and not payments.automatic():
            continue
        run_job(session, name, job, now)
    for name, job in DAILY:
        if _due_today(session, name, now) and run_job(session, name, job, now):
            _mark_ran(session, name, now)
            session.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker started")
    while not stop.is_set():
        with get_sessionmaker()() as session:
            try:
                tick(session)
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
        stop.wait(TICK_SECONDS)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
