"""Hand-off from the vendor monitors to the auto-buyers (FAC, BOIL).

The monitors (the Telegram relay and the ``fachost_sale`` provider) run in
other processes, so a sighting is dropped as one small JSON file in a queue
directory. The buyer takes the files in order and deletes them; a file
written while the buyer is down simply waits for it.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


LOG = logging.getLogger(__name__)
DEFAULT_DIR = "data/autobuy/fac_triggers"
# Older files are leftovers from a long buyer outage; a sale they point at
# is long over or will be seen again by the monitors.
MAX_AGE_SECONDS = 6 * 3600


def post_trigger(
    directory: Optional[str],
    source: str,
    urls: Iterable[str] = (),
    text: str = "",
    **extra: Any,
) -> Optional[Path]:
    """Queue one sighting. Never raises: the monitors must keep running.
    ``extra`` fields (e.g. ``public``) are passed through to the buyer."""
    if not directory:
        return None
    payload = dict(extra)
    payload.update({
        "source": source,
        "urls": [str(url) for url in urls],
        "text": (text or "")[:2000],
        "at": time.time(),
    })
    if not payload["urls"]:
        return None
    try:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        name = "%.6f-%s.json" % (payload["at"], uuid.uuid4().hex[:8])
        tmp = path / (name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(path / name))
        return path / name
    except OSError:
        LOG.exception("cannot queue FAC auto-buy trigger")
        return None


def take_triggers(directory: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Read and delete every queued trigger, oldest first; drop stale ones."""
    path = Path(directory)
    if not path.is_dir():
        return []
    now = time.time() if now is None else now
    result = []
    for item in sorted(path.glob("*.json")):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        try:
            item.unlink()
        except OSError:
            pass
        if not isinstance(data, dict):
            continue
        if now - float(data.get("at") or 0) > MAX_AGE_SECONDS:
            LOG.info("dropping stale trigger %s", item.name)
            continue
        result.append(data)
    return result


# -- alert windows ------------------------------------------------------------
# A sale may be near (the sale page opened or emptied, a target card waits
# for stock, an announcement without a link): for a while the Telegram relay
# also asks the channel for its newest post every few seconds, in case
# Telegram's push is slow (2026-11-26: 5 s), and the buyer looks for triggers
# more often. Outside these windows the relay only gets pushes (user,
# 2026-11-27: not all the time; the user's own Telegram account is used).

def open_alert(
    path: Optional[str],
    reason: str,
    minutes: float,
    every: float,
    start: Optional[float] = None,
    hourly: bool = False,
    cooldown_minutes: float = 60,
    now: Optional[float] = None,
) -> bool:
    """Add a window (``every`` seconds between pulls; ``hourly``: 2 s around
    each full hour). The same ``reason`` opens at most once per cooldown.
    Never raises."""
    if not path:
        return False
    now = time.time() if now is None else now
    try:
        target = Path(path)
        try:
            data = json.loads(target.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        opened = {str(key): float(value) for key, value in (data.get("opened") or {}).items()
                  if now - float(value) < 24 * 3600}
        if now - opened.get(reason, float("-inf")) < cooldown_minutes * 60:
            return False
        begin = now if start is None else float(start)
        windows = [item for item in data.get("windows") or [] if float(item.get("end", 0)) > now]
        windows.append({"reason": reason, "start": begin, "end": begin + minutes * 60,
                        "every": float(every), "hourly": bool(hourly)})
        opened[reason] = now
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"windows": windows, "opened": opened}, ensure_ascii=False), "utf-8")
        os.replace(str(tmp), str(target))
        LOG.info("alert window %s: %.0f min, every %gs%s", reason, minutes, every, " (hourly 2 s)" if hourly else "")
        return True
    except (OSError, ValueError, TypeError):
        LOG.exception("cannot open alert window %s", reason)
        return False


def alert_every(path: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Seconds between channel pulls right now, or None outside every window."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    best: Optional[float] = None
    for item in data.get("windows") or []:
        try:
            if not float(item["start"]) <= now < float(item["end"]):
                continue
            every = float(item["every"])
            if item.get("hourly"):
                local = time.localtime(now)
                second = local.tm_min * 60 + local.tm_sec
                if second >= 59 * 60 or second <= 90:  # :59:00 - :01:30
                    every = min(every, 2.0)
        except (KeyError, TypeError, ValueError):
            continue
        best = every if best is None else min(best, every)
    return best
