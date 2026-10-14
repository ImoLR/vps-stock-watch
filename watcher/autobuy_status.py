"""Heartbeat files shared by the auto-buyers and the Telegram bot.

Each auto-buyer runs as its own process and writes ``<dir>/<name>.json``
while it runs. The main watcher only reads these files to answer the
Telegram "抢购程序" view, so a crashed or stopped buyer shows up as a stale
heartbeat instead of needing any IPC.
"""
from __future__ import annotations

import html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_DIR = "data/autobuy"
HEARTBEAT_SECONDS = 30.0

STATE_LABELS = {
    "starting": "⏳ 启动中",
    "watching": "🟢 正在抢购",
    "ordering": "🛒 有货，正在下单",
    "done": "✅ 已抢到",
    "stopped": "⏹ 已停止",
}


class Heartbeat:
    """Writes one buyer's status file; cheap enough to call on every poll."""

    def __init__(self, directory: str, name: str, title: str, target: str, interval: float):
        self.path = Path(directory) / ("%s.json" % name)
        self.data: Dict[str, Any] = {
            "name": name,
            "title": title,
            "target": target,
            "interval": interval,
            "pid": os.getpid(),
            "started_at": _now(),
            "state": "starting",
            "detail": "",
        }
        self.last_write = float("-inf")

    def update(self, state: Optional[str] = None, detail: Optional[str] = None, **extra: Any) -> None:
        changed = False
        for key, value in dict(extra, state=state, detail=detail).items():
            if value is not None and self.data.get(key) != value:
                self.data[key] = value
                changed = True
        if changed or time.monotonic() - self.last_write >= HEARTBEAT_SECONDS:
            self.write()

    def write(self) -> None:
        self.data["updated_at"] = _now()
        self.last_write = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(self.path))
        except OSError:
            # Status is informational; never let it stop a purchase.
            pass


def load_statuses(directory: str) -> List[Dict[str, Any]]:
    result = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name"):
            result.append(data)
    return result


def effective_state(status: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """The reported state, or ``stopped`` when a running buyer stopped heartbeating."""
    state = str(status.get("state") or "stopped")
    if state in ("done", "stopped"):
        return state
    updated = _parse(status.get("updated_at"))
    if updated is None:
        return "stopped"
    try:
        interval = float(status.get("interval") or 0)
    except (TypeError, ValueError):
        interval = 0.0
    limit = max(HEARTBEAT_SECONDS, interval) * 3 + 30
    age = ((now or datetime.now(timezone.utc)) - updated).total_seconds()
    return "stopped" if age > limit else state


def format_autobuy_status(statuses: List[Dict[str, Any]], now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not statuses:
        return "🛒 <b>抢购程序</b>\n\n当前没有抢购程序。"
    order = {"ordering": 0, "watching": 1, "starting": 2, "done": 3, "stopped": 4}
    rows = sorted(statuses, key=lambda s: (order.get(effective_state(s, now), 9), s.get("name")))
    active = sum(1 for s in rows if effective_state(s, now) in ("starting", "watching", "ordering"))
    blocks = ["🛒 <b>抢购程序</b>（运行中 %d / 共 %d）" % (active, len(rows))]
    for status in rows:
        state = effective_state(status, now)
        lines = [
            "<b>%s</b>  %s" % (html.escape(str(status.get("title") or status["name"])), STATE_LABELS[state]),
            "目标：%s" % html.escape(str(status.get("target") or "-")),
        ]
        if state in ("starting", "watching", "ordering"):
            lines.append("轮询：每 %g 秒" % float(status.get("interval") or 0))
        if status.get("detail"):
            lines.append("状态：%s" % html.escape(str(status["detail"])))
        if status.get("result"):
            lines.append("结果：%s" % html.escape(str(status["result"])))
        lines.append(
            "启动：%s · 更新：%s"
            % (_short_time(status.get("started_at")), _short_time(status.get("updated_at")))
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _short_time(value: Any) -> str:
    parsed = _parse(value)
    if parsed is None:
        return "-"
    # Host local time, like the rest of the bot's timestamps.
    return parsed.astimezone().strftime("%m-%d %H:%M:%S")
