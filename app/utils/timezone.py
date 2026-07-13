"""
Central timezone utility for Bibek Enterprises backend.

All datetime operations MUST use IST (Asia/Kolkata, UTC+5:30).
- Store in DB as TIMESTAMPTZ (always UTC internally in Postgres)
- All "now" helpers return IST-aware datetimes
- All naive datetimes from DB are treated as UTC then converted to IST

Usage:
    from app.utils.timezone import now_ist, today_ist, to_ist, ist_midnight_utc

"""
import pytz
from datetime import datetime, date, timedelta, timezone

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.utc


def now_ist() -> datetime:
    """Return current datetime in IST (timezone-aware)."""
    return datetime.now(IST)


def now_utc() -> datetime:
    """Return current datetime in UTC (timezone-aware). Use for TIMESTAMPTZ DB writes."""
    return datetime.now(timezone.utc)


def now_naive() -> datetime:
    """Return current UTC datetime WITHOUT tzinfo. Use for TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_ist() -> date:
    """Return today's date in IST."""
    return now_ist().date()


def to_ist(dt: datetime | None) -> datetime | None:
    """Convert any datetime (naive=UTC assumed, or aware) to IST."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def to_utc(dt: datetime | None) -> datetime | None:
    """Convert any datetime to UTC. Naive datetimes treated as IST."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt.astimezone(timezone.utc)


def ist_midnight_utc(d: date | None = None) -> datetime:
    """Return midnight IST for given date (or today) as UTC-aware datetime."""
    d = d or today_ist()
    midnight_ist = IST.localize(datetime(d.year, d.month, d.day, 0, 0, 0))
    return midnight_ist.astimezone(timezone.utc)


def ist_end_of_day_utc(d: date | None = None) -> datetime:
    """Return 23:59:59 IST for given date (or today) as UTC-aware datetime."""
    d = d or today_ist()
    eod_ist = IST.localize(datetime(d.year, d.month, d.day, 23, 59, 59))
    return eod_ist.astimezone(timezone.utc)


def ist_date_str(dt: datetime | None = None) -> str:
    """Return YYYY-MM-DD string in IST for a given UTC datetime (or now)."""
    if dt is None:
        return today_ist().isoformat()
    return to_ist(dt).strftime("%Y-%m-%d")


def ist_invoice_suffix() -> str:
    """Return IST-based timestamp suffix for invoice/payment IDs."""
    return now_ist().strftime("%Y%m%d%H%M%S%f")
