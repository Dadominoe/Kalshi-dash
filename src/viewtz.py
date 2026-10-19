"""The time zone of whoever is looking at the dashboard.

The page tells the server its UTC offset in a cookie (`tz`, minutes east of UTC); the request handler stores it here for that request's thread,
and every on-screen clock time (completed-call times, chart axis, history hours, streak table) is formatted with get().
No cookie (a script, curl, an old tab) -> Eastern time, as before. Display only - nothing here touches trading or logging (logs stay UTC).
"""
import datetime as dt
import threading
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_local = threading.local()


def set_offset(minutes):
    """Remember the viewer's offset (minutes east of UTC) for this thread; anything invalid clears it."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = None
    _local.tz = dt.timezone(dt.timedelta(minutes=m)) if m is not None and -840 <= m <= 840 else None


def set_view(name=None, minutes=None):
    """Prefer the viewer's real zone NAME (handles daylight-saving changes for future dates); fall back to the plain offset."""
    if name:
        try:
            _local.tz = ZoneInfo(name)
            return
        except Exception:
            pass
    set_offset(minutes)


def get():
    return getattr(_local, "tz", None) or _ET


def t12(d, seconds=False):
    """'9:16 PM' / '9:16:12 PM' - regular 12-hour clock, no leading zero."""
    return d.strftime("%I:%M:%S %p" if seconds else "%I:%M %p").lstrip("0")
