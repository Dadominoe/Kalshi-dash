"""Upcoming scheduled events that can move crypto (and every risk asset) - for the call banner's 'COMING UP' card. Information only; never used for calls or trades.
Dates are the published schedules (Fed meeting calendar, BLS / BEA release calendars, the US election date). The CPI dates come from cpi_info.RELEASES. Add / adjust EVENTS when new schedules are published.
'expected' means the date follows the agency's usual pattern and should be double-checked."""
import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# (when, short name, why it can move crypto)
EVENTS = [
    (dt.datetime(2026, 10, 28, 14, 0, tzinfo=ET), "Fed rate decision", "Rate decisions move every risk asset, crypto included (press conference 2:30 PM ET)"),
    (dt.datetime(2026, 10, 29, 8, 30, tzinfo=ET), "GDP + PCE inflation", "PCE is the Fed's favourite inflation gauge; GDP shows how strong the economy is"),
    (dt.datetime(2026, 11, 3, 0, 0, tzinfo=ET), "US midterm elections", "Congress decides what happens to crypto laws and policy"),
    (dt.datetime(2026, 11, 6, 8, 30, tzinfo=ET), "US jobs report (expected)", "Strong or weak jobs data shifts interest-rate expectations"),
    (dt.datetime(2026, 12, 9, 14, 0, tzinfo=ET), "Fed rate decision", "The last Fed meeting of the year"),
]


def _cpi():
    try:
        from src import cpi_info
        return [(r[0], "US CPI inflation", "A hot or cool reading swings rate expectations and risk assets") for r in cpi_info.RELEASES]
    except Exception:
        return []


def _left(d):
    s = int(d.total_seconds())
    if s <= 0:
        return "now"
    dd, rem = divmod(s, 86400)
    hh, rem = divmod(rem, 3600)
    mm = rem // 60
    if dd >= 2:
        return f"in {dd}d {hh}h"
    if dd == 1:
        return f"in 1d {hh}h"
    if hh >= 1:
        return f"in {hh}h {mm:02d}m"
    return f"in {mm}m"


def upcoming(n=4, now=None):
    """[{name, when (aware datetime), left ('in 18h 22m'), why, hours}] for the next n events."""
    now = now or dt.datetime.now(ET)
    ev = sorted([e for e in EVENTS + _cpi() if e[0] > now - dt.timedelta(minutes=30)], key=lambda e: e[0])[:n]
    return [{"name": e[1], "when": e[0], "left": _left(e[0] - now), "why": e[2], "hours": (e[0] - now).total_seconds() / 3600} for e in ev]
