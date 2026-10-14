"""Heartbeat and control files shared by the auto-buyers and the Telegram bot.

Each auto-buyer runs as its own process and writes ``<dir>/<name>.json``
while it runs; a crashed or stopped buyer shows up as a stale heartbeat.
The bot pauses or resumes a buyer by writing ``<dir>/<name>.control``,
which the buyer re-reads on every loop. A paused buyer keeps running but
makes no requests, so resuming needs no process management.
"""
from __future__ import annotations

import html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


DEFAULT_DIR = "data/autobuy"
HEARTBEAT_SECONDS = 30.0
PAUSE_POLL_SECONDS = 2.0
MIN_INTERVAL = 2
BOUGHT_REASON = "已抢到，自动停止"
MAX_INTERVAL = 3600
LIVE_STATES = ("starting", "watching", "ordering", "paused")

STATE_LABELS = {
    "starting": "⏳ 启动中",
    "watching": "🟢 正在抢购",
    "ordering": "🛒 有货，正在下单",
    "paused": "⏹ 已停止",
    "done": "✅ 已抢到",
    "stopped": "⚠️ 进程未运行",
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


class Control:
    """The pause switch for one buyer; the bot writes it, the buyer reads it."""

    def __init__(self, directory: str, name: str):
        self.path = Path(directory) / ("%s.control" % name)

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def exists(self) -> bool:
        return self.path.exists()

    def paused(self) -> bool:
        return bool(self.read().get("paused"))

    def interval(self, default: float) -> float:
        """The poll interval set from Telegram, or ``default`` if unset/invalid."""
        value = self.read().get("interval")
        if isinstance(value, (int, float)) and MIN_INTERVAL <= value <= MAX_INTERVAL:
            return float(value)
        return default

    def set(self, paused: bool, reason: str = "") -> None:
        self.update(paused=paused, reason=reason, paused_at=_now())

    def set_interval(self, seconds: int) -> None:
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            raise ValueError("interval out of range: %s" % seconds)
        self.update(interval=seconds)

    def update(self, **fields: Any) -> None:
        data = self.read()
        data.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".control.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(self.path))


class Switch:
    """Pause handling shared by the buyers.

    A purchase pauses the buyer instead of ending the process; resuming from
    Telegram archives the purchase marker so the buyer can buy again.
    """

    def __init__(self, control: Control, marker_path: Path, beat: Callable[..., None]):
        self.control = control
        self.marker_path = marker_path
        self.beat = beat
        self.sleep = time.sleep

    def startup(self, result: str) -> None:
        """Stay paused after a past purchase unless a resume came after it."""
        if not self.marker_path.exists():
            return
        resumed_at = _parse(self.control.read().get("paused_at"))
        bought_at = datetime.fromtimestamp(self.marker_path.stat().st_mtime, timezone.utc)
        if self.control.paused() or resumed_at is None or resumed_at < bought_at:
            self.control.set(True, BOUGHT_REASON)
        self.beat(result=result)

    def hold(self) -> bool:
        """Beat and sleep while paused; True means skip this poll."""
        if not self.control.paused():
            return False
        reason = self.control.read().get("reason") or "在 Telegram 手动停止"
        self.beat(state="paused", detail=str(reason))
        self.sleep(PAUSE_POLL_SECONDS)
        return True

    def rearm(self) -> bool:
        """After a resume, move the old purchase marker aside. True if one was moved."""
        if not self.marker_path.exists():
            return False
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.marker_path.with_name(
            "%s-%s%s" % (self.marker_path.stem, stamp, self.marker_path.suffix)
        )
        os.replace(str(self.marker_path), str(target))
        self.beat(result="")
        return True

    def interval(self, default: float) -> float:
        value = self.control.interval(default)
        self.beat(interval=value)
        return value

    def bought(self, result: str) -> None:
        self.control.set(True, BOUGHT_REASON)
        self.beat(state="paused", detail=BOUGHT_REASON, result=result)


def load_statuses(directory: str) -> List[Dict[str, Any]]:
    """Heartbeats, each with the current control file attached as ``control``."""
    result = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name"):
            data["control"] = Control(directory, str(data["name"])).read()
            result.append(data)
    return result


def pending_command(status: Dict[str, Any], state: str) -> Optional[str]:
    """``pause``/``resume`` when the control file asks for a change not yet applied."""
    control = status.get("control") or {}
    if state not in LIVE_STATES or "paused" not in control:
        return None
    if control["paused"] and state != "paused":
        return "pause"
    if not control["paused"] and state == "paused":
        return "resume"
    return None


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
    rows = sort_statuses(statuses, now)
    active = sum(1 for s in rows if effective_state(s, now) in ("starting", "watching", "ordering"))
    blocks = ["🛒 <b>抢购程序</b>（运行中 %d / 共 %d）" % (active, len(rows))]
    for status in rows:
        state = effective_state(status, now)
        label = STATE_LABELS[state]
        pending = pending_command(status, state)
        if pending == "pause":
            label += "（停止指令已下达，几秒内生效）"
        elif pending == "resume":
            label += "（启动指令已下达，几秒内生效）"
        lines = [
            "<b>%s</b>  %s" % (html.escape(str(status.get("title") or status["name"])), label),
            "目标：%s" % html.escape(str(status.get("target") or "-")),
        ]
        if state in LIVE_STATES:
            lines.append("轮询：每 %g 秒" % configured_interval(status))
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


def configured_interval(status: Dict[str, Any]) -> float:
    """The interval the buyer will use next: the Telegram setting, else its own."""
    try:
        own = float(status.get("interval") or 0)
    except (TypeError, ValueError):
        own = 0.0
    value = (status.get("control") or {}).get("interval")
    if isinstance(value, (int, float)) and MIN_INTERVAL <= value <= MAX_INTERVAL:
        return float(value)
    return own


def sort_statuses(
    statuses: List[Dict[str, Any]], now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    order = {"ordering": 0, "watching": 1, "starting": 2, "paused": 3, "done": 4, "stopped": 5}
    return sorted(statuses, key=lambda s: (order.get(effective_state(s, now), 9), s.get("name")))


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
