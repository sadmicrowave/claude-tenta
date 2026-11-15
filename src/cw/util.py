"""Small helpers shared by every module: UTC time in one format, and plain-words durations."""
from datetime import datetime, timezone

SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"       # the only program search path a job gets (a throwaway tools folder may be put in front)


def now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    """2026-01-01T00:00:00Z, the one timestamp format used in job files, results and logs."""
    return (dt or now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text):
    """Inverse of iso(); returns None for anything that is not a UTC/offset ISO-8601 time."""
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def human_hours(h):
    return f"{h * 60:.0f} min" if h < 1 else (f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days")
