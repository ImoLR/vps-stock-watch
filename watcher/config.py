from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv


def load_config(path: str) -> Dict[str, Any]:
    config_path = Path(path).resolve()
    load_dotenv(config_path.parent / ".env", override=False)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise RuntimeError("cannot read config %s: %s" % (config_path, exc))
    if not isinstance(config, dict):
        raise ValueError("config root must be a mapping")
    providers = config.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("config requires at least one provider")
    config["_path"] = str(config_path)
    config["state_path"] = str(
        (config_path.parent / str(config.get("state_path", "data/state.json"))).resolve()
    )
    config["poll_interval_seconds"] = max(
        30, int(os.environ.get("POLL_INTERVAL_SECONDS", config.get("poll_interval_seconds", 60)))
    )
    return config


def telegram_credentials() -> Dict[str, str]:
    return {
        "token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    }
