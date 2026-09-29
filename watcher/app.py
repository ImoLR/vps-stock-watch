from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import load_config, telegram_credentials
from .isolated_scan import fetch_products_isolated, terminate_active_scans
from .models import Product
from .providers import build_provider
from .providers.base import BaseProvider
from .route_watch import (
    MisakaRouteWatcher,
    RouteStateStore,
    RouteWatchScheduler,
)
from .state import StateStore
from .telegram import (
    BOT_COMMANDS,
    INTERVAL_BACK_CALLBACK,
    INTERVAL_MENU_CALLBACK,
    INTERVAL_PROVIDER_PREFIX,
    ROUTE_BACK_CALLBACK,
    ROUTE_STATUS_CALLBACK,
    STOCK_MENU_CALLBACK,
    STOCK_PROVIDER_PREFIX,
    TelegramClient,
    TelegramError,
    format_change,
    format_interval_invalid,
    format_interval_menu,
    format_interval_prompt,
    format_interval_success,
    format_status,
    format_route_status,
    format_stock_menu,
    format_stock_provider,
    interval_menu_markup,
    stock_menu_markup,
    stock_provider_markup,
    status_menu_markup,
    route_status_markup,
)


LOG = logging.getLogger(__name__)
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 86400


class WatcherApp:
    def __init__(self, config: Dict[str, Any], notifications: bool = True):
        self.config = config
        self.store = StateStore(config["state_path"])
        self.providers: List[BaseProvider] = [
            build_provider(item)
            for item in config["providers"]
            if item.get("enabled", True)
        ]
        if not self.providers:
            raise ValueError("no enabled providers")
        credentials = telegram_credentials()
        self.telegram: Optional[TelegramClient] = None
        if notifications:
            if not credentials["token"] or not credentials["chat_id"]:
                raise ValueError(
                    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required; use --no-notify for local checks"
                )
            self.telegram = TelegramClient(credentials["token"], credentials["chat_id"])
        self.default_interval = int(config["poll_interval_seconds"])
        self.suspicious_keywords = config.get(
            "suspicious_keywords", ["test", "beta", "internal", "do not buy", "测试"]
        )
        self.stopping = False
        self._lock = threading.RLock()
        self._telegram_lock = threading.Lock()
        self._clock = time.monotonic
        self._next_due = {provider.name: 0.0 for provider in self.providers}
        self._running = set()
        self._provider_threads: Dict[str, threading.Thread] = {}
        self._pending_interval_provider: Optional[str] = None
        route_config = config.get("route_watch")
        self.route_config: Optional[Dict[str, Any]] = (
            route_config
            if isinstance(route_config, dict) and route_config.get("enabled", False)
            else None
        )
        self.route_store: Optional[RouteStateStore] = None
        self.route_scheduler: Optional[RouteWatchScheduler] = None
        if self.route_config:
            self.route_store = RouteStateStore(str(self.route_config["state_path"]))
            route_telegram = None
            if notifications and credentials["token"] and credentials["chat_id"]:
                route_telegram = TelegramClient(
                    credentials["token"], credentials["chat_id"]
                )
            route_watcher = MisakaRouteWatcher(
                self.route_config,
                self.route_store,
                notify=(
                    (lambda message: route_telegram.send(message))
                    if route_telegram
                    else None
                ),
            )
            self.route_scheduler = RouteWatchScheduler(
                route_watcher, self.route_config
            )

    def run_once(self) -> bool:
        success = True
        for provider in self.providers:
            success = self.check(provider) and success
        self.poll_commands()
        return success

    def run_forever(self) -> None:
        self.configure_bot_commands()
        if self.route_scheduler:
            self.route_scheduler.start()
        try:
            while not self.stopping:
                self.run_scheduled_step()
                self.poll_commands()
                time.sleep(2)
        finally:
            if self.route_scheduler:
                self.route_scheduler.stop()
            terminate_active_scans()
            self.wait_for_provider_workers(timeout=5)

    def configure_bot_commands(self) -> None:
        if not self.telegram:
            return
        try:
            with self._telegram_lock:
                self.telegram.set_commands(BOT_COMMANDS)
            LOG.info("configured Telegram bot commands")
        except TelegramError as exc:
            LOG.warning("cannot configure Telegram bot commands: %s", exc)

    def run_scheduled_step(self, now: Optional[float] = None) -> None:
        current = self._clock() if now is None else now
        for provider in self.providers:
            with self._lock:
                if (
                    current < self._next_due[provider.name]
                    or provider.name in self._running
                ):
                    continue
                self._running.add(provider.name)
                thread = threading.Thread(
                    target=self._run_provider_worker,
                    args=(provider,),
                    name="provider-%s" % provider.name,
                    daemon=True,
                )
                self._provider_threads[provider.name] = thread
            try:
                thread.start()
            except Exception:
                with self._lock:
                    self._running.discard(provider.name)
                    self._provider_threads.pop(provider.name, None)
                raise

    def _run_provider_worker(self, provider: BaseProvider) -> None:
        try:
            ok = self.check(provider)
            with self._lock:
                failures = int(
                    self.store.provider(provider.name).get("consecutive_failures", 0)
                )
                normal = self.provider_interval(provider.name)
                if ok:
                    delay = normal
                else:
                    maximum = int(provider.config.get("max_backoff_seconds", 1800))
                    delay = min(maximum, normal * (2 ** min(failures - 1, 6)))
                self._schedule(provider.name, delay)
                self.store.save()
        except Exception:
            LOG.exception("provider=%s scheduler worker failed", provider.name)
        finally:
            with self._lock:
                self._running.discard(provider.name)
                self._provider_threads.pop(provider.name, None)

    def wait_for_provider_workers(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                threads = list(self._provider_threads.values())
            if not threads:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for thread in threads:
                thread.join(timeout=min(remaining, 0.1))

    def provider_interval(self, name: str) -> int:
        with self._lock:
            configured = next(
                (
                    item.config.get("interval_seconds", self.default_interval)
                    for item in self.providers
                    if item.name == name
                ),
                self.default_interval,
            )
            override = self.store.provider_intervals().get(name)
            if (
                isinstance(override, int)
                and not isinstance(override, bool)
                and MIN_SCAN_INTERVAL <= override <= MAX_SCAN_INTERVAL
            ):
                return override
            return max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(configured)))

    def provider_intervals(self) -> Dict[str, int]:
        return {provider.name: self.provider_interval(provider.name) for provider in self.providers}

    def set_provider_interval(self, name: str, seconds: int) -> None:
        if name not in {provider.name for provider in self.providers}:
            raise ValueError("unknown provider")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not MIN_SCAN_INTERVAL <= seconds <= MAX_SCAN_INTERVAL
        ):
            raise ValueError("interval is outside the allowed range")
        with self._lock:
            self.store.set_provider_interval(name, seconds)
            self._schedule(name, seconds)
            self.store.save()

    def _schedule(self, name: str, delay: int) -> None:
        with self._lock:
            self._next_due[name] = self._clock() + delay
            self.store.provider(name)["next_check_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat().replace("+00:00", "Z")

    def check(self, provider: BaseProvider) -> bool:
        LOG.info("checking provider=%s", provider.name)
        try:
            products = self._fetch_products(provider)
            with self._lock:
                previous_state = deepcopy(self.store.provider(provider.name))
            provider.validate_snapshot(products, previous_state)
            additional_changes = provider.additional_changes(products, previous_state)
            with self._lock:
                first_run, changes = self.store.record_success(provider.name, products)
                provider.update_state_after_success(
                    self.store.provider(provider.name), products
                )
                if not first_run:
                    changes.extend(additional_changes)
                self.store.save()
            LOG.info(
                "provider=%s success products=%d baseline=%s changes=%d",
                provider.name,
                len(products),
                first_run,
                len(changes),
            )
            if first_run:
                return True
            for change in changes:
                if not provider.should_notify(change):
                    LOG.info(
                        "notification suppressed provider=%s product=%s change=%s",
                        provider.name,
                        change.product.key,
                        change.type.value,
                    )
                    continue
                message = format_change(change, self.suspicious_keywords)
                if self.telegram:
                    try:
                        with self._telegram_lock:
                            self.telegram.send(message)
                    except TelegramError:
                        LOG.exception("notification failed provider=%s product=%s", provider.name, change.product.key)
                        continue
                    with self._lock:
                        self.store.increment_notifications()
                        self.store.save()
                else:
                    LOG.info("notification disabled: %s", message.replace("\n", " | "))
            return True
        except Exception as exc:
            LOG.exception("provider=%s check failed", provider.name)
            with self._lock:
                self.store.record_failure(
                    provider.name, "%s: %s" % (type(exc).__name__, exc)
                )
                self.store.save()
            return False

    @staticmethod
    def _fetch_products(provider: BaseProvider) -> List[Product]:
        if provider.config.get("isolate_process", False):
            return fetch_products_isolated(provider.config)
        return provider.fetch_products()

    def poll_commands(self) -> None:
        if not self.telegram:
            return
        with self._lock:
            offset = int(self.store.data.get("telegram_update_offset", 0))
        try:
            with self._telegram_lock:
                updates = self.telegram.commands(offset)
        except TelegramError as exc:
            LOG.warning("cannot poll Telegram commands: %s", exc)
            return
        for update in updates:
            update_id = int(update.get("update_id", 0))
            try:
                self.process_update(update)
            except TelegramError as exc:
                LOG.warning("cannot process Telegram update: %s", exc)
            finally:
                with self._lock:
                    self.store.data["telegram_update_offset"] = max(
                        int(self.store.data.get("telegram_update_offset", 0)),
                        update_id + 1,
                    )
                    self.store.save()

    def process_update(self, update: Dict[str, Any]) -> None:
        if not self.telegram:
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._process_callback(callback)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((message.get("from") or {}).get("id", ""))
        text = str(message.get("text", "")).strip()
        command = text.split("@", 1)[0].strip().lower()
        if self._authorized(chat_id, user_id) and command == "/status":
            self._pending_interval_provider = None
            self.send_status_menu()
            LOG.info("answered Telegram /status")
            return
        if self._authorized(chat_id, user_id) and command == "/stock":
            self._pending_interval_provider = None
            self.send_stock_menu(refresh_disk=True)
            LOG.info("answered Telegram /stock from state")
            return
        if not self._authorized(chat_id, user_id) or not self._pending_interval_provider:
            return
        if text.casefold() == "取消".casefold():
            self._pending_interval_provider = None
            self._send_interval_menu()
            return
        if not re.fullmatch(r"[0-9]+", text):
            self._send_telegram(format_interval_invalid(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL))
            return
        seconds = int(text)
        if not MIN_SCAN_INTERVAL <= seconds <= MAX_SCAN_INTERVAL:
            self._send_telegram(format_interval_invalid(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL))
            return
        name = self._pending_interval_provider
        old = self.provider_interval(name)
        self.set_provider_interval(name, seconds)
        self._pending_interval_provider = None
        LOG.info("provider interval updated provider=%s old=%d new=%d", name, old, seconds)
        self._send_telegram(format_interval_success(name, old, seconds))
        self._send_interval_menu()

    def _process_callback(self, callback: Dict[str, Any]) -> None:
        if not self.telegram:
            return
        callback_id = str(callback.get("id", ""))
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        user_id = str((callback.get("from") or {}).get("id", ""))
        if not self._authorized(chat_id, user_id):
            with self._telegram_lock:
                self.telegram.answer_callback(callback_id, "无权限", show_alert=True)
            return
        with self._telegram_lock:
            self.telegram.answer_callback(callback_id)
        data = str(callback.get("data", ""))
        if data == INTERVAL_MENU_CALLBACK:
            self._pending_interval_provider = None
            self._send_interval_menu()
        elif data == INTERVAL_BACK_CALLBACK:
            self._pending_interval_provider = None
            self.send_status_menu()
        elif data == ROUTE_BACK_CALLBACK:
            self._pending_interval_provider = None
            self.send_status_menu()
        elif data == ROUTE_STATUS_CALLBACK and self.route_store:
            self._pending_interval_provider = None
            self.send_route_status(refresh_disk=True)
            LOG.info("answered Telegram Misaka route status from state")
        elif data.startswith(INTERVAL_PROVIDER_PREFIX):
            name = data[len(INTERVAL_PROVIDER_PREFIX):]
            if name not in {provider.name for provider in self.providers}:
                return
            self._pending_interval_provider = name
            self._send_telegram(
                format_interval_prompt(
                    name,
                    self.provider_interval(name),
                    MIN_SCAN_INTERVAL,
                    MAX_SCAN_INTERVAL,
                )
            )
        elif data == STOCK_MENU_CALLBACK:
            self._pending_interval_provider = None
            self.send_stock_menu(refresh_disk=True)
            LOG.info("refreshed Telegram /stock menu from state")
        elif data.startswith(STOCK_PROVIDER_PREFIX):
            match = re.fullmatch(r"stock:provider:([a-z0-9_-]+):([0-9]+)", data)
            if not match:
                return
            name, page_text = match.groups()
            if name not in {provider.name for provider in self.providers}:
                return
            self._pending_interval_provider = None
            self.send_stock_provider(name, int(page_text), refresh_disk=True)
            LOG.info(
                "answered Telegram /stock provider=%s legacy_page=%s from state",
                name,
                page_text,
            )

    def _authorized(self, chat_id: str, user_id: str) -> bool:
        if not self.telegram:
            return False
        return bool(chat_id) and chat_id == self.telegram.chat_id and user_id == self.telegram.chat_id

    def send_status_menu(self) -> None:
        route_state = self._route_state(refresh_disk=True)
        with self._lock:
            state = self.store.snapshot()
            intervals = self.provider_intervals()
        self._send_telegram(
            format_status(
                state,
                intervals,
                route_state=route_state,
                route_interval=(
                    int(self.route_config.get("interval_seconds", 1800))
                    if self.route_config
                    else None
                ),
            ),
            status_menu_markup(route_enabled=bool(self.route_config)),
        )

    def send_route_status(self, refresh_disk: bool = False) -> None:
        route_state = self._route_state(refresh_disk)
        if route_state is None:
            return
        messages = format_route_status(route_state)
        for index, text in enumerate(messages):
            markup = route_status_markup() if index == len(messages) - 1 else None
            self._send_telegram(text, markup)

    def _route_state(self, refresh_disk: bool) -> Optional[Dict[str, Any]]:
        if not self.route_store or not self.route_config:
            return None
        if refresh_disk:
            return RouteStateStore(str(self.route_config["state_path"])).snapshot()
        return self.route_store.snapshot()

    def send_stock_menu(self, refresh_disk: bool = False) -> None:
        state = self._stock_state(refresh_disk)
        names = [provider.name for provider in self.providers]
        self._send_telegram(
            format_stock_menu(state, names),
            stock_menu_markup(state, names),
        )

    def send_stock_provider(
        self, name: str, page: int = 0, refresh_disk: bool = False
    ) -> None:
        state = self._stock_state(refresh_disk)
        messages = format_stock_provider(
            state,
            name,
            self.provider_interval(name),
        )
        for index, text in enumerate(messages):
            markup = stock_provider_markup() if index == len(messages) - 1 else None
            self._send_telegram(text, markup)

    def _stock_state(self, refresh_disk: bool) -> Dict[str, Any]:
        if refresh_disk:
            return StateStore(str(self.store.path)).snapshot()
        with self._lock:
            return self.store.snapshot()

    def _send_interval_menu(self) -> None:
        names = [provider.name for provider in self.providers]
        self._send_telegram(
            format_interval_menu(self.provider_intervals(), names),
            interval_menu_markup(names),
        )

    def _send_telegram(
        self, text: str, reply_markup: Optional[Dict[str, Any]] = None
    ) -> None:
        if not self.telegram:
            return
        with self._telegram_lock:
            self.telegram.send(text, reply_markup=reply_markup)
        with self._lock:
            self.store.increment_notifications()
            self.store.save()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VPS/IDC catalog and stock watcher")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="check each enabled provider once")
    parser.add_argument("--no-notify", action="store_true", help="do not call Telegram")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # urllib3's DEBUG request lines contain the Telegram token in the URL path.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        app = WatcherApp(load_config(args.config), notifications=not args.no_notify)
    except Exception as exc:
        LOG.error("startup failed: %s", exc)
        return 2

    def stop(_signum: int, _frame: Any) -> None:
        app.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.once:
        return 0 if app.run_once() else 1
    app.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
