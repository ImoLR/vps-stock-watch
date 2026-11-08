"""Relay FACHOST's Telegram announcements to the watcher's bot chat.

Runs as its own service (deploy/fac-tg-watch.service) with a logged-in
Telegram *user* session, because a bot cannot join the vendor's group or
read its channel. Telegram pushes new messages over the open connection, so
nothing is polled except a 10-minute channel catch-up after reconnects.

Read-only: it never sends, reacts, joins or marks anything read in the
vendor's chats. Only the watcher bot (TELEGRAM_BOT_TOKEN) posts, to
TELEGRAM_CHAT_ID.

What is relayed:
- channel: every new post, plus edits that change the text;
- group: messages with a fachost.cloud link (anyone), and messages from
  group admins (people, or bots named fac_*) that hit a sale keyword.

Relayed messages with a FAC link or a sale keyword are also queued for the
FAC auto-buyer (``autobuy_trigger_dir``; see watcher.fac_autobuy).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from .config import load_config, telegram_credentials
from .fac_trigger import post_trigger
from .telegram import TelegramClient as BotClient
from .telegram import TelegramError


LOG = logging.getLogger("fac_tg_watch")
CST = timezone(timedelta(hours=8))
AUTH_FAILED_EXIT = 3
DEFAULT_KEYWORDS = (
    "黑五", "black friday", "雙11", "双11", "雙十一", "双十一", "年付", "年繳",
    "年缴", "special-regulations", "優惠碼", "优惠码", "折扣碼", "折扣码",
    "優惠券", "优惠券", "上架", "上新", "補貨", "补货", "預售", "预售", "限時",
    "限时", "限量", "活動", "活动", "週年", "周年", "anniversary",
)
LINK = re.compile(r"https?://(?:www\.)?fachost\.(?:cloud|com)[^\s<>\"')\]]*", re.I)


def message_urls(text: str, entities: Iterable[Any] = (), buttons: Any = None) -> List[str]:
    """Every URL in a message: plain text, hidden text links and buttons."""
    urls: List[str] = []
    for value in re.findall(r"https?://[^\s<>\"')\]]+", text or ""):
        urls.append(value)
    for entity in entities or ():
        url = getattr(entity, "url", None)
        if url:
            urls.append(url)
    for row in buttons or ():
        for button in row:
            url = getattr(button, "url", None)
            if url:
                urls.append(url)
    seen: Set[str] = set()
    result = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def fac_links(urls: Iterable[str]) -> List[str]:
    return [url for url in urls if LINK.match(url)]


def matches_keyword(text: str, keywords: Iterable[str]) -> bool:
    lowered = (text or "").casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


def group_relay_reason(
    text: str,
    urls: List[str],
    sender_is_admin: bool,
    keywords: Iterable[str],
) -> Optional[str]:
    """Why a group message is relayed, or None to skip it."""
    if fac_links(urls):
        return "含 FAC 链接"
    if sender_is_admin and matches_keyword(text, keywords):
        return "管理员 + 活动关键词"
    return None


def format_relay(
    source: str,
    when: Any,
    text: str,
    urls: List[str],
    link: Optional[str],
    sender: Optional[str] = None,
    reason: Optional[str] = None,
    edited: bool = False,
) -> str:
    heading = {
        "channel": "📣 FAC 公告频道%s" % ("（已编辑）" if edited else ""),
        "group": "💬 FAC 群消息",
    }[source]
    lines = ["<b>%s</b>" % heading]
    meta = []
    if when is not None:
        meta.append(when.astimezone(CST).strftime("%m-%d %H:%M"))
    if sender:
        meta.append(html.escape(sender))
    if reason:
        meta.append(html.escape(reason))
    if meta:
        lines.append(" · ".join(meta))
    lines.append("")
    body = (text or "").strip() or "（无文字，可能是图片或文件）"
    if len(body) > 3000:
        body = body[:3000] + "…"
    lines.append(html.escape(body))
    links = fac_links(urls)
    if links:
        lines.append("")
        lines.append("<b>FAC 链接：</b>")
        for url in links[:10]:
            lines.append('<a href="%s">%s</a>' % (html.escape(url, quote=True), html.escape(url)))
    if link:
        lines.extend(["", '<a href="%s">打开原消息</a>' % html.escape(link, quote=True)])
    return "\n".join(lines)


def text_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class RelayState:
    """Last relayed channel post and text digests, so restarts don't repeat."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("channel_last_id", None)
        self.data.setdefault("digests", {})

    def save(self) -> None:
        digests = self.data["digests"]
        if len(digests) > 500:
            for key in sorted(digests, key=lambda value: int(value))[:-500]:
                digests.pop(key, None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


def _env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.strip().split("=", 1)
            values[key] = value
    return values


def _display(entity: Any) -> str:
    if entity is None:
        return "?"
    name = " ".join(
        part for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if part
    ) or getattr(entity, "title", None) or ""
    username = getattr(entity, "username", None)
    return "%s%s" % (name, " @%s" % username if username else "")


class FacTelegramWatch:
    def __init__(self, config: Dict[str, Any], bot: Optional[BotClient]):
        from telethon import TelegramClient

        base = Path(config["_path"]).parent
        self.cfg = config["fac_tg_watch"]
        session_dir = (base / str(self.cfg.get("session_dir", "data/tg"))).resolve()
        creds = _env_file(session_dir / "user.env")
        self.client = TelegramClient(
            str(session_dir / "fac_user"), int(creds["TG_API_ID"]), creds["TG_API_HASH"]
        )
        self.bot = bot
        self.channel_id = int(self.cfg["channel_id"])
        self.channel_username = self.cfg.get("channel_username")
        self.group_id = int(self.cfg["group_id"]) if self.cfg.get("group_id") else None
        self.keywords = list(self.cfg.get("keywords") or DEFAULT_KEYWORDS)
        self.catch_up_seconds = max(60, int(self.cfg.get("catch_up_seconds", 600)))
        self.admin_refresh_seconds = max(300, int(self.cfg.get("admin_refresh_seconds", 3600)))
        self.state = RelayState(session_dir / "relay_state.json")
        trigger_dir = self.cfg.get("autobuy_trigger_dir")
        self.trigger_dir = str(base / str(trigger_dir)) if trigger_dir else None
        self.admins: Set[int] = set()
        self._admins_at = 0.0
        self._lock = asyncio.Lock()

    async def notify(self, message: str) -> None:
        if not self.bot:
            LOG.info("notification disabled: %s", message.replace("\n", " | "))
            return
        try:
            await asyncio.to_thread(self.bot.send, message)
        except TelegramError:
            LOG.exception("relay notification failed")

    def trigger(self, source: str, text: str, urls: List[str], trusted: bool) -> None:
        """Queue a sighting for the auto-buyer: FAC links, or a sale keyword
        from the channel / a group admin."""
        links = fac_links(urls)
        keyword = trusted and matches_keyword(text, self.keywords)
        if links or keyword:
            post_trigger(self.trigger_dir, source, links, text, keyword_hit=keyword)

    def _channel_link(self, message_id: int) -> str:
        if self.channel_username:
            return "https://t.me/%s/%s" % (self.channel_username, message_id)
        return "https://t.me/c/%s/%s" % (str(self.channel_id)[4:], message_id)

    async def refresh_admins(self, force: bool = False) -> None:
        if self.group_id is None or (not force and time.monotonic() - self._admins_at < self.admin_refresh_seconds):
            return
        from telethon.tl.types import ChannelParticipantsAdmins

        admins: Set[int] = set()
        try:
            async for user in self.client.iter_participants(self.group_id, filter=ChannelParticipantsAdmins):
                username = (getattr(user, "username", None) or "").lower()
                # Group-management bots (lottery, cleanup) are noise; FAC's own
                # bots are named fac_*.
                if getattr(user, "bot", False) and not username.startswith("fac_"):
                    continue
                admins.add(int(user.id))
        except Exception:
            LOG.exception("cannot list group admins; keeping %d known", len(self.admins))
            return
        self.admins = admins
        self._admins_at = time.monotonic()
        LOG.info("group admins refreshed: %d", len(admins))

    async def handle_channel(self, message: Any, edited: bool = False) -> None:
        async with self._lock:
            key = str(message.id)
            digest = text_digest(message.raw_text or "")
            previous = self.state.data["digests"].get(key)
            if previous == digest:
                return
            if edited and previous is None:
                # Edit of a post from before this watcher started: skip old history.
                last = self.state.data["channel_last_id"]
                if last is None or message.id <= last:
                    self.state.data["digests"][key] = digest
                    self.state.save()
                    return
            urls = message_urls(message.raw_text, message.entities, message.buttons)
            self.trigger("tg-channel", message.raw_text, urls, True)
            await self.notify(
                format_relay(
                    "channel",
                    message.date,
                    message.raw_text,
                    urls,
                    self._channel_link(message.id),
                    edited=edited and previous is not None,
                )
            )
            self.state.data["digests"][key] = digest
            last = self.state.data["channel_last_id"]
            if last is None or message.id > last:
                self.state.data["channel_last_id"] = message.id
            self.state.save()

    async def handle_group(self, message: Any) -> None:
        # Channel posts auto-forwarded into the group are relayed already.
        fwd = getattr(message, "fwd_from", None)
        if int(message.sender_id or 0) == self.channel_id or (fwd and getattr(fwd, "channel_post", None)):
            return
        await self.refresh_admins()
        sender_id = int(message.sender_id or 0)
        urls = message_urls(message.raw_text, message.entities, message.buttons)
        reason = group_relay_reason(message.raw_text, urls, sender_id in self.admins, self.keywords)
        if not reason:
            return
        self.trigger("tg-group", message.raw_text, urls, sender_id in self.admins)
        sender = await message.get_sender()
        link = "https://t.me/c/%s/%s" % (str(self.group_id)[4:], message.id)
        await self.notify(
            format_relay("group", message.date, message.raw_text, urls, link, _display(sender), reason)
        )

    async def catch_up(self) -> None:
        """Relay channel posts missed while disconnected (cheap, one API call)."""
        last = self.state.data["channel_last_id"]
        if last is None:
            latest = await self.client.get_messages(self.channel_id, limit=1)
            self.state.data["channel_last_id"] = latest[0].id if latest else 0
            self.state.save()
            return
        missed = []
        async for message in self.client.iter_messages(self.channel_id, min_id=last, reverse=True, limit=50):
            missed.append(message)
        for message in missed:
            await self.handle_channel(message)

    async def _catch_up_loop(self) -> None:
        while True:
            await asyncio.sleep(self.catch_up_seconds)
            try:
                await self.catch_up()
            except Exception:
                LOG.exception("channel catch-up failed")

    async def run(self) -> int:
        from telethon import events
        from telethon.errors import UnauthorizedError

        await self.client.connect()
        try:
            if not await self.client.is_user_authorized():
                await self.notify("<b>⚠️ FAC TG 监听：登录已失效</b>\n\n需要重新登录 TG 账号后才能继续转发。")
                return AUTH_FAILED_EXIT
            me = await self.client.get_me()
            LOG.info("logged in as id=%s", me.id)
            # Resolve both chats once so events can match them by ID.
            await self.client.get_entity(self.channel_id)
            if self.group_id is not None:
                await self.client.get_entity(self.group_id)
            await self.refresh_admins(force=True)
            first_start = self.state.data["channel_last_id"] is None
            await self.catch_up()

            @self.client.on(events.NewMessage(chats=[self.channel_id]))
            async def on_channel(event):  # noqa: ANN001
                await self.handle_channel(event.message)

            @self.client.on(events.MessageEdited(chats=[self.channel_id]))
            async def on_channel_edit(event):  # noqa: ANN001
                await self.handle_channel(event.message, edited=True)

            if self.group_id is not None:
                @self.client.on(events.NewMessage(chats=[self.group_id]))
                async def on_group(event):  # noqa: ANN001
                    try:
                        await self.handle_group(event.message)
                    except Exception:
                        LOG.exception("group message handling failed")

            if first_start:
                await self.notify(
                    "<b>✅ FAC TG 监听已启动</b>\n\n公告频道每条都会转发；群里只转发含 FAC 链接、"
                    "或管理员发的活动消息。"
                )
            task = asyncio.ensure_future(self._catch_up_loop())
            try:
                await self.client.run_until_disconnected()
            finally:
                task.cancel()
            return 1
        except UnauthorizedError:
            LOG.exception("Telegram session rejected")
            await self.notify("<b>⚠️ FAC TG 监听：登录已失效</b>\n\n需要重新登录 TG 账号后才能继续转发。")
            return AUTH_FAILED_EXIT
        finally:
            await self.client.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    config = load_config(args.config)
    if not (config.get("fac_tg_watch") or {}).get("enabled", False):
        LOG.info("fac_tg_watch is disabled in the config")
        return 0
    bot = None
    if not args.no_notify:
        credentials = telegram_credentials()
        if not credentials["token"] or not credentials["chat_id"]:
            LOG.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
            return 2
        bot = BotClient(credentials["token"], credentials["chat_id"])
    async def run() -> int:
        # Build the client inside the running loop: Telethon (and asyncio.Lock
        # on 3.9) bind to the loop current at construction time.
        return await FacTelegramWatch(config, bot).run()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
