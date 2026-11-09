from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..models import Change, ChangeType, Product
from .base import BaseProvider, ParseError
from .dmit import keyword_hit


LOG = logging.getLogger(__name__)


def keyword_labels(text: str, links: List[str], keywords: Dict[str, List[str]]) -> List[str]:
    """Labels whose aliases appear in a post's text or links."""
    haystack = (text or "") + " " + " ".join(links)
    return [label for label, aliases in keywords.items() if keyword_hit(haystack, aliases)]


def pid_of(url: str) -> Optional[str]:
    values = parse_qs(urlparse(url).query).get("pid")
    return values[0] if values and re.fullmatch(r"\d+", values[0]) else None


def resolve_links(
    session: requests.Session,
    links: List[str],
    hosts: List[str],
    watched_pids: Set[str],
    spacing: float = 3.0,
    timeout: float = 20.0,
) -> List[Dict[str, Any]]:
    """{link, pid, watched} for each cart link or short link (on ``hosts``) in
    a post; a short link costs one request (its redirect names the PID)."""
    hosts_set = {str(host).lower() for host in hosts}
    result: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        pid = pid_of(link)
        host = (urlparse(link).hostname or "").lower()
        if pid is None and host in hosts_set:
            if result:
                time.sleep(spacing)
            try:
                response = session.get(link, timeout=timeout, allow_redirects=False)
                pid = pid_of(urljoin(link, response.headers.get("Location") or ""))
            except requests.RequestException as exc:
                LOG.warning("cannot resolve %s (%s)", link, type(exc).__name__)
        if pid is None and host not in hosts_set:
            continue
        result.append({"link": link, "pid": pid, "watched": pid in watched_pids if pid else None})
    return result


class TgChannelProvider(BaseProvider):
    """Keyword posts of a public Telegram channel, read from its web preview
    (https://t.me/s/<channel>; no login, nothing sent to the vendor's site).

    Added 2026-11-10 for @dmitnews, which posts every DMIT special-plan
    restock with an ``act.dmithost.net`` short link. Each matching post is a
    product; a new one is notified once. Short links in a new post are
    resolved (one request each, spaced) to the PID they point at, so a plan
    moved to a new PID still shows up.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.channel = str(config["channel"]).lstrip("@")
        # {label: [aliases]}; "tyo+tiny" needs both words.
        self.keywords: Dict[str, List[str]] = {
            str(label): [str(alias) for alias in aliases]
            for label, aliases in (config.get("keywords") or {}).items()
        }
        self.watched_pids = {str(pid) for pid in config.get("watched_pids") or []}
        self._links: Dict[str, List[Dict[str, Any]]] = {}

    def restore_state(self, provider_state: Dict[str, Any]) -> None:
        for item in (provider_state.get("products") or {}).values():
            if isinstance(item, dict):
                links = (item.get("metadata") or {}).get("links")
                if isinstance(links, list):
                    self._links[str(item.get("product_id"))] = links

    def fetch_products(self) -> List[Product]:
        url = "https://t.me/s/%s" % self.channel
        posts = self.parse_posts(self.get_text(url))
        products = []
        for post in posts:
            labels = keyword_labels(post["text"], post["links"], self.keywords)
            if not labels:
                continue
            if post["id"] not in self._links:
                self._links[post["id"]] = self._resolve(post["links"])
            products.append(
                Product(
                    provider=self.name,
                    product_id=post["id"],
                    name=post["title"],
                    category="TG 频道 @%s" % self.channel,
                    url="https://t.me/%s/%s" % (self.channel, post["id"]),
                    metadata={
                        "catalog_event": "channel_post",
                        "channel": self.channel,
                        "labels": labels,
                        "posted_at": post["date"],
                        "text": post["text"][:1500],
                        "links": self._links[post["id"]],
                    },
                )
            )
        return products

    @staticmethod
    def parse_posts(page_html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(page_html, "lxml")
        nodes = soup.select(".tgme_widget_message[data-post]")
        if not nodes:
            raise ParseError("Telegram channel page had no posts")
        posts = []
        for node in nodes:
            post_id = str(node["data-post"]).rsplit("/", 1)[-1]
            body = node.select_one(".tgme_widget_message_text")
            if body is None:
                continue
            for br in body.find_all("br"):
                br.replace_with("\n")
            text = body.get_text().strip()
            links = [str(a["href"]) for a in body.select("a[href]")]
            stamp = node.select_one("time[datetime]")
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            posts.append(
                {
                    "id": post_id,
                    "title": first[:100] or "post %s" % post_id,
                    "text": text,
                    "links": links,
                    "date": str(stamp["datetime"]) if stamp else None,
                }
            )
        return posts

    def _resolve(self, links: List[str]) -> List[Dict[str, Any]]:
        return resolve_links(
            self.session,
            links,
            list(self.config.get("resolve_hosts") or []),
            self.watched_pids,
            max(0.0, float(self.config.get("resolve_spacing_seconds", 3))),
            self.timeout,
        )

    def should_notify(self, change: Change) -> bool:
        return change.type == ChangeType.NEW


__all__ = ["TgChannelProvider", "keyword_labels", "pid_of", "resolve_links"]
