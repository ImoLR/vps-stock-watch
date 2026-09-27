from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .diff import compare_products, index_products
from .models import Change, ChangeType, Product


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "telegram_update_offset": 0,
        "counters": {"new_products": 0, "notifications": 0},
        "settings": {"provider_intervals": {}},
        "providers": {},
    }


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return empty_state()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RuntimeError("cannot load state %s: %s" % (self.path, exc))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise RuntimeError("unsupported state format in %s" % self.path)
        value.setdefault("counters", {}).setdefault("new_products", 0)
        value["counters"].setdefault("notifications", 0)
        value.setdefault("providers", {})
        value.setdefault("telegram_update_offset", 0)
        settings = value.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            value["settings"] = settings
        intervals = settings.setdefault("provider_intervals", {})
        if not isinstance(intervals, dict):
            settings["provider_intervals"] = {}
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = utc_now()
        fd, temporary = tempfile.mkstemp(
            prefix=".%s." % self.path.name, suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def provider(self, name: str) -> Dict[str, Any]:
        return self.data["providers"].setdefault(
            name,
            {
                "initialized": False,
                "last_check_at": None,
                "last_success_at": None,
                "last_error": None,
                "consecutive_failures": 0,
                "next_check_at": None,
                "products": {},
            },
        )

    def record_success(self, name: str, products: Iterable[Product]) -> Tuple[bool, List[Change]]:
        entry = self.provider(name)
        snapshot = list(products)
        current_map = index_products(snapshot)
        now = utc_now()
        first_run = not entry.get("initialized", False)
        changes: List[Change] = []
        if not first_run:
            old = [Product.from_dict(item) for item in entry.get("products", {}).values()]
            changes = compare_products(old, snapshot)
            self.data["counters"]["new_products"] += sum(
                1 for change in changes if change.type == ChangeType.NEW
            )
        entry.update(
            {
                "initialized": True,
                "last_check_at": now,
                "last_success_at": now,
                "last_error": None,
                "consecutive_failures": 0,
                "products": {key: product.to_dict() for key, product in current_map.items()},
            }
        )
        return first_run, changes

    def record_failure(self, name: str, error: str) -> None:
        entry = self.provider(name)
        entry["last_check_at"] = utc_now()
        entry["last_error"] = error[:1000]
        entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        # Deliberately do not touch products or last_success_at.

    def increment_notifications(self, amount: int = 1) -> None:
        self.data["counters"]["notifications"] += amount

    def provider_intervals(self) -> Dict[str, Any]:
        return self.data["settings"]["provider_intervals"]

    def set_provider_interval(self, name: str, seconds: int) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise ValueError("provider interval must be a positive integer")
        self.provider_intervals()[name] = seconds

    def snapshot(self) -> Dict[str, Any]:
        return deepcopy(self.data)
