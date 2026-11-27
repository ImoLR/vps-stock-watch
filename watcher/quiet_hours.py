"""Daily quiet hours, Taiwan time: a site gets no request at all meanwhile.

``quiet_hours: "00:05-09:05"`` (start-end, may wrap past midnight; empty =
off). BOIL, 2026-11-28 (user): nobody runs a sale at night, and the TCS
SeedNet exit changes its IP at 04:00 anyway, so the BOIL monitor and buyer
rest overnight while the Telegram relay keeps notifying as usual.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def parse_quiet_hours(spec: Any) -> Optional[Tuple[time, time]]:
    """``"00:05-09:05"`` -> (start, end); empty -> None; bad text raises ValueError."""
    text = str(spec or "").strip()
    if not text:
        return None
    start, sep, end = text.partition("-")
    if not sep:
        raise ValueError("quiet_hours must look like 00:05-09:05, not %r" % text)
    window = (time.fromisoformat(start.strip()), time.fromisoformat(end.strip()))
    if window[0] == window[1]:
        raise ValueError("quiet_hours %r is empty" % text)
    return window


def in_quiet_hours(spec: Any, now: Optional[datetime] = None) -> bool:
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    clock = (now or datetime.now(TAIPEI)).astimezone(TAIPEI).time()
    start, end = window
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end
