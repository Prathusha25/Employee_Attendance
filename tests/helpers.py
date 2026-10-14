"""Small helpers shared by the tests. They use only the standard library, so expected values in the tests do
not depend on the code under test."""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h=0, mi=0, s=0, ms=0) -> int:
    """Epoch milliseconds of an IST wall-clock time."""
    dt = datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + ms


def ist_dt(y, mo, d, h=0, mi=0, s=0) -> datetime:
    """Aware UTC datetime of an IST wall-clock time (what MongoDB stores)."""
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)
