from datetime import datetime
from datetime import time as dtime


def in_quiet_hours(quiet_hours: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    start = dtime.fromisoformat(quiet_hours["start"])
    end = dtime.fromisoformat(quiet_hours["end"])
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # window wraps past midnight
