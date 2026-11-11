"""Hand-off from the FACHOST monitors to the FAC auto-buyer.

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
) -> Optional[Path]:
    """Queue one sighting. Never raises: the monitors must keep running."""
    if not directory:
        return None
    payload = {
        "source": source,
        "urls": [str(url) for url in urls],
        "text": (text or "")[:2000],
        "at": time.time(),
    }
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
