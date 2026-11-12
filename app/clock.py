"""Time helpers. Timestamps are stored in UTC; business rules use Kampala time."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KAMPALA = ZoneInfo("Africa/Kampala")


def utcnow() -> datetime:
    return datetime.now(UTC)


def kampala_date(at: datetime) -> date:
    return at.astimezone(KAMPALA).date()


def kampala_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC start (inclusive) and end (exclusive) of a Kampala calendar day."""
    start = datetime.combine(day, time.min, tzinfo=KAMPALA)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def format_kampala(at: datetime) -> str:
    return at.astimezone(KAMPALA).strftime("%d %b %Y %H:%M") + " EAT"
