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

Only a FAC link posted on the channel or by a group admin is also queued for
the FAC auto-buyer (``autobuy_trigger_dir``; see watcher.fac_autobuy).
Keyword-only messages are relayed but arm nothing (user, 2026-11-11: group
chat hits sale words all the time; 20 s polling is too much for that).

``relays`` (2026-11-11, user): other vendors relayed the same way as FAC
(BOIL): every channel post, and group messages with the vendor's shop link
or a staff sale post. ``staff_usernames`` narrows "admin" to the vendor's own
accounts when the group has many community admins.

``watch_channels`` (2026-11-10, user): other vendors' channels the account
follows (DMIT). Only posts naming a watched plan are relayed, with their
short links resolved to PIDs; pushed like the FAC channel, never polled.
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

import requests

from .config import load_config, telegram_credentials
from .fac_trigger import post_trigger
from .providers.tgchannel import keyword_labels, resolve_links
from .telegram import TelegramClient as BotClient
from .telegram import TelegramError, format_channel_post


LOG = logging.getLogger("fac_tg_watch")
CST = timezone(timedelta(hours=8))
AUTH_FAILED_EXIT = 3
DEFAULT_KEYWORDS = (
    "黑五", "black friday", "雙11", "双11", "雙十一", "双十一", "年付", "年繳",
    "年缴", "special-regulations", "優惠碼", "优惠码", "折扣碼", "折扣码",
    "優惠券", "优惠券", "上架", "上新", "補貨", "补货", "預售", "预售", "限時",
    "限时", "限量", "活動", "活动", "週年", "周年", "anniversary",
)
FAC_HOSTS = ("fachost.cloud", "fachost.com")


def link_pattern(hosts: Iterable[str]) -> "re.Pattern[str]":
    return re.compile(
        r"https?://(?:www\.)?(?:%s)(?![\w.-])[^\s<>\"')\]]*" % "|".join(re.escape(h) for h in hosts),
        re.I,
    )


LINK = link_pattern(FAC_HOSTS)


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


def fac_links(urls: Iterable[str], pattern: "re.Pattern[str]" = LINK) -> List[str]:
    return [url for url in urls if pattern.match(url)]


def matches_keyword(text: str, keywords: Iterable[str]) -> bool:
    lowered = (text or "").casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


def group_relay_reason(
    text: str,
    urls: List[str],
    sender_is_admin: bool,
    keywords: Iterable[str],
    pattern: "re.Pattern[str]" = LINK,
    vendor: str = "FAC",
) -> Optional[str]:
    """Why a group message is relayed, or None to skip it."""
    if fac_links(urls, pattern):
        return "含 %s 链接" % vendor
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
    vendor: str = "FAC",
    pattern: "re.Pattern[str]" = LINK,
) -> str:
    heading = {
        "channel": "📣 %s 公告频道%s" % (vendor, "（已编辑）" if edited else ""),
        "group": "💬 %s 群消息" % vendor,
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
    links = fac_links(urls, pattern)
    if links:
        lines.append("")
        lines.append("<b>%s 链接：</b>" % html.escape(vendor))
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

    def relay(self, name: Optional[str]) -> Dict[str, Any]:
        """FAC keeps the top-level keys (older files); other vendors nest."""
        if name is None:
            return self.data
        entry = self.data.setdefault("relays", {}).setdefault(name, {})
        entry.setdefault("channel_last_id", None)
        entry.setdefault("digests", {})
        return entry

    def save(self) -> None:
        for entry in [self.data] + list((self.data.get("relays") or {}).values()):
            digests = entry["digests"]
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


def vendor_channel(item: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """One ``watch_channels`` entry; ``watched_pids_from`` copies a provider's
    ``watch_pids`` so the two lists cannot drift apart."""
    watched = {str(pid) for pid in item.get("watched_pids") or []}
    source = item.get("watched_pids_from")
    if source:
        for provider in config.get("providers") or []:
            if provider.get("name") == source:
                watched.update(str(spec["pid"]) for spec in provider.get("watch_pids") or [])
    return {
        "name": str(item.get("name") or "频道"),
        "ids": [int(value) for value in item["ids"]],
        # Trading channels: a post must also say "restock" and not "buy/sell".
        "require_words": [str(word) for word in item.get("require_words") or []],
        "skip_words": [str(word) for word in item.get("skip_words") or []],
        "keywords": {
            str(label): [str(alias) for alias in aliases]
            for label, aliases in (item.get("keywords") or {}).items()
        },
        "hosts": [str(host) for host in item.get("resolve_hosts") or []],
        "watched": watched,
    }


class Relay:
    """One vendor's announcement channel and chat group."""

    def __init__(self, name: str, item: Dict[str, Any], base: Path, primary: bool = False):
        self.name = name
        # FAC's state stays at the top level of relay_state.json.
        self.state_key = None if primary else name
        self.channel_id = int(item["channel_id"])
        self.channel_username = item.get("channel_username")
        self.group_id = int(item["group_id"]) if item.get("group_id") else None
        self.link = link_pattern(item.get("link_hosts") or (FAC_HOSTS if primary else ()))
        self.keywords = list(item.get("keywords") or DEFAULT_KEYWORDS)
        # Admin bots count only with this username prefix (FAC's own fac_*).
        self.bot_prefix = str(item.get("admin_bot_prefix") or ("fac_" if primary else "")).lower()
        self.staff = {
            str(value).lower().lstrip("@") for value in item.get("staff_usernames") or []
        }
        trigger_dir = item.get("autobuy_trigger_dir")
        self.trigger_dir = str(base / str(trigger_dir)) if trigger_dir else None
        self.admins: Set[int] = set()
        self.admins_at = 0.0

    def is_admin(self, user: Any) -> bool:
        username = (getattr(user, "username", None) or "").lower()
        if self.staff:
            return username in self.staff
        # Group-management bots (lottery, cleanup) are noise.
        if getattr(user, "bot", False):
            return bool(self.bot_prefix) and username.startswith(self.bot_prefix)
        return True


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
        self.relays = [Relay("FAC", self.cfg, base, primary=True)] + [
            Relay(str(item["name"]), item, base) for item in self.cfg.get("relays") or []
        ]
        self.by_channel = {relay.channel_id: relay for relay in self.relays}
        self.by_group = {relay.group_id: relay for relay in self.relays if relay.group_id is not None}
        self.trigger_dir = self.relays[0].trigger_dir
        self.keywords = self.relays[0].keywords
        self.catch_up_seconds = max(60, int(self.cfg.get("catch_up_seconds", 600)))
        self.admin_refresh_seconds = max(300, int(self.cfg.get("admin_refresh_seconds", 3600)))
        self.state = RelayState(session_dir / "relay_state.json")
        self._lock = asyncio.Lock()
        self.vendor_channels = [
            vendor_channel(item, config) for item in self.cfg.get("watch_channels") or []
        ]
        self.vendor_by_id = {
            chat_id: vendor for vendor in self.vendor_channels for chat_id in vendor["ids"]
        }
        self.state.data.setdefault("vendor_last_ids", {})
        self._empty_logged: Set[int] = set()
        self.http = requests.Session()

    async def notify(self, message: str) -> None:
        if not self.bot:
            LOG.info("notification disabled: %s", message.replace("\n", " | "))
            return
        try:
            await asyncio.to_thread(self.bot.send, message)
        except TelegramError:
            LOG.exception("relay notification failed")

    def trigger(
        self, source: str, text: str, urls: List[str], trusted: bool, relay: Optional[Relay] = None
    ) -> None:
        """Queue a sighting for the auto-buyer: a FAC link from the channel or
        a group admin. Keywords alone, or links from other members, don't."""
        links = fac_links(urls, relay.link if relay else LINK)
        directory = relay.trigger_dir if relay else self.trigger_dir
        if trusted and links:
            post_trigger(directory, source, links, text)

    @staticmethod
    def _channel_link(relay: Relay, message_id: int) -> str:
        if relay.channel_username:
            return "https://t.me/%s/%s" % (relay.channel_username, message_id)
        return "https://t.me/c/%s/%s" % (str(relay.channel_id)[4:], message_id)

    async def refresh_admins(self, relay: Relay, force: bool = False) -> None:
        if relay.group_id is None or (
            not force and time.monotonic() - relay.admins_at < self.admin_refresh_seconds
        ):
            return
        from telethon.tl.types import ChannelParticipantsAdmins

        admins: Set[int] = set()
        try:
            async for user in self.client.iter_participants(relay.group_id, filter=ChannelParticipantsAdmins):
                if relay.is_admin(user):
                    admins.add(int(user.id))
        except Exception:
            LOG.exception("cannot list %s group admins; keeping %d known", relay.name, len(relay.admins))
            return
        relay.admins = admins
        relay.admins_at = time.monotonic()
        LOG.info("%s group admins refreshed: %d", relay.name, len(admins))

    async def handle_channel(self, message: Any, edited: bool = False) -> None:
        relay = self.by_channel.get(int(getattr(message, "chat_id", 0) or 0), self.relays[0])
        async with self._lock:
            state = self.state.relay(relay.state_key)
            key = str(message.id)
            digest = text_digest(message.raw_text or "")
            previous = state["digests"].get(key)
            if previous == digest:
                return
            if edited and previous is None:
                # Edit of a post from before this watcher started: skip old history.
                last = state["channel_last_id"]
                if last is None or message.id <= last:
                    state["digests"][key] = digest
                    self.state.save()
                    return
            urls = message_urls(message.raw_text, message.entities, message.buttons)
            self.trigger("tg-channel", message.raw_text, urls, True, relay)
            await self.notify(
                format_relay(
                    "channel",
                    message.date,
                    message.raw_text,
                    urls,
                    self._channel_link(relay, message.id),
                    edited=edited and previous is not None,
                    vendor=relay.name,
                    pattern=relay.link,
                )
            )
            state["digests"][key] = digest
            last = state["channel_last_id"]
            if last is None or message.id > last:
                state["channel_last_id"] = message.id
            self.state.save()

    async def handle_group(self, message: Any) -> None:
        relay = self.by_group.get(int(getattr(message, "chat_id", 0) or 0))
        if relay is None:
            return
        # Channel posts auto-forwarded into the group are relayed already.
        fwd = getattr(message, "fwd_from", None)
        if int(message.sender_id or 0) == relay.channel_id or (fwd and getattr(fwd, "channel_post", None)):
            return
        await self.refresh_admins(relay)
        sender_id = int(message.sender_id or 0)
        is_admin = sender_id in relay.admins
        urls = message_urls(message.raw_text, message.entities, message.buttons)
        reason = group_relay_reason(
            message.raw_text, urls, is_admin, relay.keywords, relay.link, relay.name
        )
        if not reason:
            return
        self.trigger("tg-group", message.raw_text, urls, is_admin, relay)
        sender = await message.get_sender()
        link = "https://t.me/c/%s/%s" % (str(relay.group_id)[4:], message.id)
        await self.notify(
            format_relay(
                "group", message.date, message.raw_text, urls, link, _display(sender), reason,
                vendor=relay.name, pattern=relay.link,
            )
        )

    async def handle_vendor(self, message: Any) -> None:
        chat_id = int(message.chat_id)
        vendor = self.vendor_by_id.get(chat_id)
        if vendor is None:
            return
        async with self._lock:
            last = self.state.data["vendor_last_ids"].get(str(chat_id))
            if last is not None and message.id <= last:
                return
            self.state.data["vendor_last_ids"][str(chat_id)] = message.id
            self.state.save()
        text = message.raw_text or ""
        urls = message_urls(text, message.entities, message.buttons)
        if not text and not urls:
            if chat_id not in self._empty_logged:
                # @DMITstock posts arrived empty on 11-10; keep one for a look.
                self._empty_logged.add(chat_id)
                LOG.info("empty post %s/%s: %s", chat_id, message.id, str(message.to_dict())[:1500])
            return
        labels = keyword_labels(text, urls, vendor["keywords"])
        if not labels:
            return
        if vendor["skip_words"] and matches_keyword(text, vendor["skip_words"]):
            LOG.info("vendor post %s/%s skipped (trade post): %s", chat_id, message.id, text[:80])
            return
        if vendor["require_words"] and not matches_keyword(text, vendor["require_words"]):
            return
        links = await asyncio.to_thread(
            resolve_links, self.http, urls, vendor["hosts"], vendor["watched"]
        )
        chat = await message.get_chat()
        username = getattr(chat, "username", None)
        link = (
            "https://t.me/%s/%s" % (username, message.id)
            if username
            else "https://t.me/c/%s/%s" % (str(chat_id)[4:], message.id)
        )
        LOG.info("vendor post %s/%s: %s", chat_id, message.id, labels)
        await self.notify(
            format_channel_post(
                "%s · %s" % (vendor["name"], getattr(chat, "title", None) or chat_id),
                labels,
                text,
                links,
                link,
                message.date.astimezone(CST).strftime("%m-%d %H:%M") if message.date else None,
            )
        )

    async def catch_up_vendors(self) -> None:
        for chat_id in self.vendor_by_id:
            last = self.state.data["vendor_last_ids"].get(str(chat_id))
            if last is None:
                latest = await self.client.get_messages(chat_id, limit=1)
                self.state.data["vendor_last_ids"][str(chat_id)] = latest[0].id if latest else 0
                self.state.save()
                continue
            missed = []
            async for message in self.client.iter_messages(chat_id, min_id=last, reverse=True, limit=50):
                missed.append(message)
            for message in missed:
                try:
                    await self.handle_vendor(message)
                except Exception:
                    LOG.exception("vendor post handling failed")

    async def catch_up(self, relay: Relay) -> None:
        """Relay channel posts missed while disconnected (cheap, one API call)."""
        state = self.state.relay(relay.state_key)
        last = state["channel_last_id"]
        if last is None:
            latest = await self.client.get_messages(relay.channel_id, limit=1)
            state["channel_last_id"] = latest[0].id if latest else 0
            self.state.save()
            return
        missed = []
        async for message in self.client.iter_messages(relay.channel_id, min_id=last, reverse=True, limit=50):
            missed.append(message)
        for message in missed:
            await self.handle_channel(message)

    async def _catch_up_loop(self) -> None:
        while True:
            await asyncio.sleep(self.catch_up_seconds)
            for relay in self.relays:
                try:
                    await self.catch_up(relay)
                except Exception:
                    LOG.exception("%s channel catch-up failed", relay.name)
            try:
                await self.catch_up_vendors()
            except Exception:
                LOG.exception("vendor channel catch-up failed")

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
            # Resolve every chat once so events can match them by ID. FAC must
            # open; a later vendor that fails is dropped with a log line.
            first_starts = []
            for relay in list(self.relays):
                try:
                    await self.client.get_entity(relay.channel_id)
                    if relay.group_id is not None:
                        await self.client.get_entity(relay.group_id)
                except Exception:
                    if relay is self.relays[0]:
                        raise
                    LOG.exception("cannot open %s chats; skipping that relay", relay.name)
                    self.relays.remove(relay)
                    continue
                await self.refresh_admins(relay, force=True)
                if self.state.relay(relay.state_key)["channel_last_id"] is None:
                    first_starts.append(relay)
                await self.catch_up(relay)
            self.by_channel = {relay.channel_id: relay for relay in self.relays}
            self.by_group = {
                relay.group_id: relay for relay in self.relays if relay.group_id is not None
            }
            for chat_id in list(self.vendor_by_id):
                try:
                    await self.client.get_entity(chat_id)
                except Exception:
                    LOG.exception("cannot open vendor channel %s; skipping it", chat_id)
                    self.vendor_by_id.pop(chat_id)
            await self.catch_up_vendors()
            if self.vendor_by_id:
                LOG.info("watching vendor channels: %s", sorted(self.vendor_by_id))

                @self.client.on(events.NewMessage(chats=list(self.vendor_by_id)))
                async def on_vendor(event):  # noqa: ANN001
                    try:
                        await self.handle_vendor(event.message)
                    except Exception:
                        LOG.exception("vendor post handling failed")

            @self.client.on(events.NewMessage(chats=list(self.by_channel)))
            async def on_channel(event):  # noqa: ANN001
                await self.handle_channel(event.message)

            @self.client.on(events.MessageEdited(chats=list(self.by_channel)))
            async def on_channel_edit(event):  # noqa: ANN001
                await self.handle_channel(event.message, edited=True)

            if self.by_group:
                @self.client.on(events.NewMessage(chats=list(self.by_group)))
                async def on_group(event):  # noqa: ANN001
                    try:
                        await self.handle_group(event.message)
                    except Exception:
                        LOG.exception("group message handling failed")

            for relay in first_starts:
                await self.notify(
                    "<b>✅ %s TG 监听已启动</b>\n\n公告频道每条都会转发；群里只转发含 %s 链接、"
                    "或%s发的活动消息。"
                    % (relay.name, relay.name, "官方账号" if relay.staff else "管理员")
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
