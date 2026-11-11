from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..fac_trigger import post_trigger
from ..models import Change, Product
from .base import BaseProvider, FetchError, is_challenge_page
from .boilcloud import BoilcloudProvider
from .highend import CYCLES, NOT_FOUND, parse_feed, parse_price


LOG = logging.getLogger(__name__)
FEED_PATH = "feeds/productsinfo.php"


class BoilcloudSaleProvider(BoilcloudProvider):
    """Watch BOILCLOUD's hidden holiday group and new PIDs (user, 2026-11-11).

    BOIL sells holiday items from the hidden WHMCS group ``store/festive-offer``
    (618 / National Day blind boxes, Christmas 2025, the 2026 HiNet plan) or as
    new PIDs in hidden zone groups, and posts the links on its Telegram
    channel. Each round reads the festive group (one request): a pseudo
    product ``category:festive-offer`` is available while the group lists
    products, so empty -> listed alerts like a restock, and its cards are
    products with stock. Every ``feed_every_rounds`` the open productsinfo feed
    probes ``probe_ahead`` PIDs above the highest seen; a new PID is reported
    once, with its prices. Notify only: these are hidden links. Every
    ``watch_every_rounds`` the public ``watch_groups`` (Taiwan) are read too,
    keeping only SeedNet cards: a SeedNet plan listed there is public. An open group
    also wakes the BOIL buyer (``autobuy_trigger_dir``), marked public only
    when the shop menu lists it.
    """

    DEFAULT_CATEGORIES = ("festive-offer",)

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = str(config.get("base_url", self.BASE_URL))
        # PIDs found by the feed, kept across rounds (the feed is not re-read).
        self._new_pids: Dict[str, Product] = {}
        # Target cards (SeedNet) on public groups, kept between their reads.
        self._group_cards: Dict[str, Product] = {}
        self._high_water = int(config.get("feed_start_pid") or 0)
        self._rounds = 0
        self._last_request: Optional[float] = None
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep

    def restore_state(self, provider_state: Dict[str, Any]) -> None:
        for item in (provider_state.get("products") or {}).values():
            product = Product.from_dict(item)
            if product.metadata.get("catalog_event") == "new_pid":
                self._new_pids[product.product_id] = product
            elif (product.metadata.get("discovery") or {}).get("type") == "public_group":
                self._group_cards[product.product_id] = product
        pids = [int(item.metadata["pid"]) for item in self._new_pids.values()]
        self._high_water = max(
            [self._high_water, int(provider_state.get("high_water_pid") or 0)] + pids
        )

    def update_state_after_success(
        self, provider_state: Dict[str, Any], products: List[Product]
    ) -> None:
        provider_state["high_water_pid"] = self._high_water

    def should_notify(self, change: Change) -> bool:
        # Every sale-item change alerts; the catalog provider's SeedNet/annual
        # stock filter is for regular products.
        return BaseProvider.should_notify(self, change)

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._scan_request_count = 0
        slugs = [str(value) for value in self.config.get("categories") or self.DEFAULT_CATEGORIES]
        products: List[Product] = []
        cards: Dict[str, Product] = {}
        for slug in slugs:
            url = urljoin(self.base_url, "store/%s" % slug)
            status, final_url, text = self._get(url)
            if status == 404 or self._category_slug(final_url) != slug:
                # Gone, or redirected to the bare store: treat as closed.
                products.append(self._category_product(slug, url, False, closed="页面不存在或跳走"))
                continue
            if status != 200:
                raise FetchError("HTTP %s for %s" % (status, url), status)
            group_id = self._category_group_id(text, slug)
            items = self.parse_category(text, final_url, slug, self._heading(text) or slug, group_id)
            listed = self._listed_in_menu(text, slug)
            for item in items:
                item.metadata["discovery"] = {
                    "type": "sale_group",
                    "hidden": not listed,
                    "source": "store/%s" % slug,
                }
                cards.setdefault(item.product_id, item)
            products.append(
                self._category_product(slug, url, bool(items), len(items), self._heading(text), listed)
            )
        self._read_watch_groups()
        for key, item in self._group_cards.items():
            cards.setdefault(key, item)
        products.extend(cards[key] for key in sorted(cards, key=int))
        for item in products:
            if item.metadata.get("catalog_event") == "sale_category" and item.available:
                # Wake the BOIL buyer; it orders only once the group is public
                # (listed in the shop menu) or posted on Telegram.
                # The card texts let the buyer skip a group with no SeedNet.
                text = "\n".join(
                    "%s %s" % (card.name, card.metadata.get("description") or "")
                    for card in cards.values()
                )
                post_trigger(
                    self.config.get("autobuy_trigger_dir"), "boilcloud_sale", [item.url], text,
                    public=bool(item.metadata.get("listed_in_menu")),
                )

        found: List[str] = []
        every = max(1, int(self.config.get("feed_every_rounds", 3)))
        if self._high_water and self._rounds % every == 0:
            found = self._probe_new_pids()
        self._rounds += 1
        products.extend(self._new_pids[key] for key in sorted(self._new_pids))
        self.last_scan_stats = {
            "categories": len(slugs),
            "open_categories": sum(
                1
                for item in products
                if item.metadata.get("catalog_event") == "sale_category" and item.available
            ),
            "sale_products": len(cards),
            "watched_group_cards": len(self._group_cards),
            "high_water_pid": self._high_water,
            "new_pids": found,
            "requests": self._scan_request_count,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return products

    # -- public groups (target cards only) -----------------------------------

    def _read_watch_groups(self) -> None:
        """Every ``watch_every_rounds``, read the public ``watch_groups`` and
        keep only cards naming ``watch_keywords`` (SeedNet). These are listed
        in the shop, so an orderable one wakes the buyer as public."""
        groups = [str(value) for value in self.config.get("watch_groups") or []]
        every = max(1, int(self.config.get("watch_every_rounds", 2)))
        if not groups or self._rounds % every:
            return
        keywords = [str(word).casefold() for word in self.config.get("watch_keywords") or ["seednet"]]
        found: Dict[str, Product] = {}
        for slug in groups:
            url = urljoin(self.base_url, "store/%s" % slug)
            status, final_url, text = self._get(url)
            if status != 200 or self._category_slug(final_url) != slug:
                raise FetchError("HTTP %s for %s (%s)" % (status, url, final_url), status)
            label = self._category_label(text, slug, [])
            for item in self.parse_category(text, final_url, slug, label, self._category_group_id(text, slug)):
                blob = ("%s %s" % (item.name, item.metadata.get("description") or "")).casefold()
                if not any(word in blob for word in keywords):
                    continue
                item.metadata["discovery"] = {"type": "public_group", "hidden": False, "source": "store/%s" % slug}
                found.setdefault(item.product_id, item)
                if item.available:
                    post_trigger(
                        self.config.get("autobuy_trigger_dir"), "boilcloud_sale", [url],
                        "%s %s" % (item.name, item.metadata.get("description") or ""), public=True,
                    )
        # Commit only after every group was read.
        self._group_cards = found

    # -- festive group ---------------------------------------------------------

    def _category_product(
        self,
        slug: str,
        url: str,
        is_open: bool,
        product_count: int = 0,
        heading: Optional[str] = None,
        listed: Optional[bool] = None,
        closed: str = "分组为空",
    ) -> Product:
        return Product(
            provider=self.name,
            product_id="category:%s" % slug,
            name="活动分组 %s" % (heading or slug),
            category="活动分组",
            available=is_open,
            url=url,
            metadata={
                "catalog_event": "sale_category",
                "category_slug": slug,
                "product_count": product_count if is_open else None,
                "closed_label": closed,
                # Whether the shop's own category menu lists the group (public).
                "listed_in_menu": listed if is_open else None,
            },
        )

    def _listed_in_menu(self, page_html: str, slug: str) -> bool:
        soup = BeautifulSoup(page_html, "lxml")
        for node in soup.select('.cart-sidebar [menuitemname="Categories"] a[href]'):
            if self._category_slug(urljoin(self.base_url, str(node.get("href") or ""))) == slug:
                return True
        return False

    @staticmethod
    def _heading(page_html: str) -> Optional[str]:
        soup = BeautifulSoup(page_html, "lxml")
        node = soup.select_one("#order-standard_cart h2.font-size-22") or soup.select_one(
            "#order-standard_cart h1"
        )
        text = " ".join(node.get_text(" ", strip=True).split()) if node else ""
        return text or None

    # -- new PIDs ----------------------------------------------------------------

    def _probe_new_pids(self) -> List[str]:
        ahead = max(1, int(self.config.get("probe_ahead", 2)))
        found: List[str] = []
        high_water = self._high_water
        new_pids = dict(self._new_pids)
        for pid in range(self._high_water + 1, self._high_water + ahead + 1):
            name = " ".join(self._feed(pid, "name").split())
            if name == NOT_FOUND:
                continue
            product = Product(
                provider=self.name,
                product_id="pid:%d" % pid,
                name=name,
                category="新 PID",
                url=urljoin(self.base_url, "cart.php?a=add&pid=%d" % pid),
                metadata={
                    "catalog_event": "new_pid",
                    "pid": pid,
                    "hidden_direct_link": True,
                    "discovery": {"type": "feed_probe", "hidden": True, "source": FEED_PATH},
                },
            )
            pricing: Dict[str, str] = {}
            for cycle in CYCLES:
                price = parse_price(self._feed(pid, "price", billingcycle=cycle))
                if price is not None:
                    pricing[cycle] = price
            product.pricing = pricing
            first = next(iter(pricing.items()), None)
            product.price = first[1] if first else None
            product.billing_cycle = first[0] if first else None
            new_pids[product.product_id] = product
            found.append(str(pid))
            high_water = max(high_water, pid)
        # Commit only after every request in the probe succeeded.
        self._new_pids = new_pids
        self._high_water = high_water
        return found

    def _feed(self, pid: int, get: str, **extra: str) -> str:
        params = {"pid": str(pid), "get": get}
        if get == "price":
            params["currency"] = str(self.config.get("currency_id", 1))
        params.update(extra)
        status, _, text = self._get(urljoin(self.base_url, FEED_PATH), params)
        if status != 200:
            raise FetchError("HTTP %s for %s pid=%s" % (status, FEED_PATH, pid), status)
        return parse_feed(text)

    # -- HTTP ------------------------------------------------------------------

    def _get(self, url: str, params: Optional[Dict[str, str]] = None):
        """One GET, spaced from the last one; a block page is never retried."""
        spacing = max(3.0, float(self.config.get("request_spacing_seconds", 5)))
        if self._last_request is not None:
            wait = spacing - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()
        self._scan_request_count += 1
        try:
            response = self.session.get(url, params=params, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise FetchError("request failed for %s (%s)" % (url, type(exc).__name__)) from None
        text = response.text
        if response.status_code in (403, 429) or is_challenge_page(text):
            raise FetchError(
                "HTTP %s for %s (challenge/block page)" % (response.status_code, url),
                response.status_code,
                True,
            )
        return response.status_code, response.url, text


__all__ = ["BoilcloudSaleProvider"]
