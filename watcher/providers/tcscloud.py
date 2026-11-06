from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup, Tag

from ..models import Change, ChangeType, Product
from .base import BaseProvider, FetchError, ParseError, is_challenge_page
from .highend import parse_feed, parse_price


LOG = logging.getLogger(__name__)

BASE_URL = "https://billing.tcscloud.net/"
START_CATEGORY = "taiwan-vps-dynamic"
CATEGORY_PATH = "index.php?rp=/store/%s"
FEED_PATH = "feeds/productsinfo.php"
ORDER_PATH = "cart.php?a=add&pid=%s"
NOT_FOUND = "Product ID Not Found"
STORE_ROUTE = re.compile(r"rp=/store/([^/?&#\"'<>\s]+)/?(?:[?&#]|$)")
STORE_PATH = re.compile(r"rp=/store/([^/?&#]+)(?:/([^/?&#]+))?")
QTY = re.compile(r"(?<![0-9])([0-9]+)\s*(?:可用|available)", re.I)
EMPTY_GROUP_MARKERS = (
    "產品群組不包含任何可見的產品",
    "产品组中不包含任何可见的产品",
    "product group does not contain any visible products",
)
ERROR_MARKERS = ("糟糕，發生問題了", "oops, there's a problem", "頁面未找到", "page not found")
SOLD_OUT_MARKERS = ("缺貨中", "缺货中", "out of stock")
CYCLE_TEXT = (
    ("半年繳", "半年付"),
    ("月繳", "月付"),
    ("季繳", "季付"),
    ("兩年繳", "两年付"),
    ("三年繳", "三年付"),
    ("年繳", "年付"),
    ("一次性", "一次性"),
    ("免費", "免费"),
)
CYCLE_KEYS = {
    "monthly": "月付",
    "quarterly": "季付",
    "semiannually": "半年付",
    "annually": "年付",
    "biennially": "两年付",
    "triennially": "三年付",
    "onetime": "一次性",
    "free": "免费",
}
SPEC_LABELS = {
    "cpu": "cpu",
    "ram": "ram",
    "memory": "ram",
    "disk": "disk",
    "storage": "disk",
    "network": "bandwidth",
    "bandwidth": "bandwidth",
    "traffic": "traffic",
    "ip": "ipv4",
    "ipv4": "ipv4",
    "ipv6": "ipv6",
}
# Every TCSCloud product today is in Taipei. A name token naming another
# market moves it out of the default region.
OTHER_REGIONS = (
    (re.compile(r"(?<![A-Z])(HK|HKG)(?![A-Z])"), "HK"),
    (re.compile(r"(?<![A-Z])(JP|TYO|OSA)(?![A-Z])"), "JP"),
    (re.compile(r"(?<![A-Z])(SG|SIN)(?![A-Z])"), "SG"),
    (re.compile(r"(?<![A-Z])(US|LAX|SJC)(?![A-Z])"), "US"),
)


def region_of(name: str, default: str = "TW") -> str:
    upper = name.upper()
    for pattern, region in OTHER_REGIONS:
        if pattern.search(upper):
            return region
    return default


def lines_of(node: Optional[Tag]) -> List[str]:
    if node is None:
        return []
    for br in node.find_all("br"):
        br.replace_with("\n")
    return [" ".join(line.split()) for line in node.get_text().split("\n") if line.strip()]


def parse_specs(lines: List[str]) -> Dict[str, str]:
    """``CPU | 2 vCore`` style description lines -> spec fields."""
    specs: Dict[str, str] = {}
    for line in lines:
        label, sep, value = line.partition("|")
        key = SPEC_LABELS.get(label.strip().casefold())
        if sep and key and value.strip() and key not in specs:
            specs[key] = value.strip()
    return specs


def store_routes(soup: BeautifulSoup) -> Dict[str, str]:
    """Category slug -> label, from every ``rp=/store/<slug>`` link on a page."""
    routes: Dict[str, str] = {}
    for link in soup.select('a[href*="rp=/store/"]'):
        match = STORE_ROUTE.search(unquote(str(link.get("href"))))
        if not match:
            continue
        slug = match.group(1)
        label = " ".join(link.get_text(" ", strip=True).split())
        if slug not in routes or (label and not routes[slug]):
            routes[slug] = label
    return routes


def parse_category(html: str, slug: str) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Return (cards, store routes) for one WHMCS standard_cart category page."""
    soup = BeautifulSoup(html, "lxml")
    text = " ".join(soup.get_text(" ", strip=True).split()).casefold()
    routes = store_routes(soup)
    cards = soup.select('div.product[id^="product"]')
    if not cards:
        if any(marker.casefold() in text for marker in EMPTY_GROUP_MARKERS):
            return [], routes
        if any(marker.casefold() in text for marker in ERROR_MARKERS):
            raise ParseError("TCSCloud category %s returned an error page" % slug)
        raise ParseError("TCSCloud category %s has no product cards" % slug)
    items: List[Dict[str, Any]] = []
    for card in cards:
        match = re.fullmatch(r"product([0-9]+)", str(card.get("id") or ""))
        if not match:
            continue
        pid = match.group(1)
        name_node = card.select_one("#product%s-name" % pid)
        name = " ".join(name_node.get_text(" ", strip=True).split()) if name_node else ""
        if not name:
            raise ParseError("TCSCloud PID %s has no name" % pid)
        stock: Optional[int] = None
        qty = card.select_one(".qty")
        if qty is not None:
            found = QTY.search(qty.get_text(" ", strip=True))
            if not found:
                raise ParseError(
                    "TCSCloud PID %s has an unrecognized stock marker %r"
                    % (pid, qty.get_text(" ", strip=True))
                )
            stock = int(found.group(1))
        pricing = card.select_one("#product%s-price" % pid)
        price_node = pricing.select_one(".price") if pricing else None
        if price_node is None:
            raise ParseError("TCSCloud PID %s has no price" % pid)
        price = " ".join(price_node.get_text(" ", strip=True).split())
        rest = " ".join(pricing.get_text(" ", strip=True).split())
        cycle = next((label for marker, label in CYCLE_TEXT if marker in rest), None)
        button = card.select_one("#product%s-order-button[href]" % pid)
        lines = lines_of(card.select_one("#product%s-description" % pid))
        items.append(
            {
                "pid": pid,
                "name": name,
                "stock": stock,
                "price": price,
                "price_from": "從開始" in rest or "starting from" in rest.casefold(),
                "billing_cycle": cycle,
                "card_url": str(button.get("href")) if button else None,
                "description": lines,
            }
        )
    return items, routes


def parse_order_page(html: str, final_url: str) -> Dict[str, Any]:
    """Classify a ``cart.php?a=add&pid=N`` result.

    - ``available``: lands on ``a=confproduct`` (billing-cycle selector), or
      straight in the cart (``a=view``) for a product with nothing to configure.
    - ``sold_out``: the WHMCS "缺貨中" page, served at the product's own store
      URL ``/store/<group>/<product>``; ``group`` names its (maybe hidden) group.
    - ``retired``: bounced back to a bare store category; WHMCS does this for a
      retired product. 2026-11-06: pids 24, 81-87, 95-107.
    - ``unknown``: anything else.
    """
    parsed = urlparse(final_url)
    query = parse_qs(parsed.query)
    path = STORE_PATH.search(unquote(final_url))
    soup = BeautifulSoup(html, "lxml")
    text = " ".join(soup.get_text(" ", strip=True).split())
    lowered = text.casefold()
    if query.get("a") == ["view"] and parsed.path.endswith("cart.php"):
        return {"status": "available", "pricing": {}}
    if query.get("a") == ["confproduct"]:
        pricing: Dict[str, str] = {}
        for option in soup.select('select[name="billingcycle"] option'):
            label = " ".join(option.get_text(" ", strip=True).split())
            key = CYCLE_KEYS.get(str(option.get("value") or ""), str(option.get("value") or ""))
            if label and key:
                pricing[key] = label
        single = soup.select_one('input[name="billingcycle"]')
        if not pricing and single is not None:
            key = CYCLE_KEYS.get(str(single.get("value") or ""), str(single.get("value") or ""))
            pricing[key] = ""
        return {"status": "available", "pricing": pricing}
    if any(marker.casefold() in lowered for marker in SOLD_OUT_MARKERS):
        return {"status": "sold_out", "group": path.group(1) if path else None}
    if path and not path.group(2) and soup.select('div.product[id^="product"]'):
        return {"status": "retired"}
    return {"status": "unknown", "url": final_url, "excerpt": text[:160]}


class TcscloudProvider(BaseProvider):
    """TCSCloud (billing.tcscloud.net), a WHMCS store selling Taipei VPS.

    Each round reads every store category (the sidebar lists them; a new one is
    picked up automatically). Cards carry the real stock count ``N 可用``, the
    starting price and the spec lines. Categories with stock tracking off (Taipei
    Premium) have no count; those, and products that exist but are not listed in
    any category (direct-link sale items), get their stock from the order page:
    ``cart.php?a=add&pid=N`` lands on ``confproduct`` when orderable and on the
    WHMCS "缺貨中" page when sold out.

    Unlisted products are found through the public ``feeds/productsinfo.php``
    feed, which answers a name for any PID, hidden or not: the first run walks
    from ``feed_start_pid`` until a long run of empty IDs, later rounds probe just
    above the highest PID ever seen (WHMCS IDs only grow) and re-read one
    unlisted name. Feed work runs every ``feed_every_rounds`` rounds to keep the
    request budget. All requests go one at a time ``request_spacing_seconds``
    apart, and a round only commits after every request in it succeeded.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = str(config.get("base_url", BASE_URL))
        self._products: Dict[str, Product] = {}
        self._unlisted: Dict[str, str] = {}
        self._hidden_groups: Set[str] = set()
        self._missing: Dict[str, int] = {}
        self._high_water = 0
        self._far_cursor = 0
        self._rounds = 0
        self._restored = False
        self._last_request: Optional[float] = None
        self._requests = 0
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
        self._now: Callable[[], float] = time.time
        self.last_scan_stats: Dict[str, Any] = {}

    # -- config ------------------------------------------------------------------

    def _focus(self) -> Set[str]:
        return {str(item).upper() for item in self.config.get("focus_regions") or []}

    def _in_focus(self, region: Optional[str]) -> bool:
        focus = self._focus()
        return not focus or (region or "").upper() in focus

    def _ignored(self) -> Set[str]:
        return {str(pid) for pid in self.config.get("ignore_pids") or []}

    def _watched(self, *texts: Optional[str]) -> bool:
        """Whether a product gets stock work and stock/price alerts.

        ``stock_watch_keywords`` (2026-11-06: SeedNet only) are matched in the
        name, description and store link. Without the setting every product is
        watched.
        """
        keywords = [str(item).casefold() for item in self.config.get("stock_watch_keywords") or []]
        if not keywords:
            return True
        haystack = " ".join(text for text in texts if text).casefold()
        return any(keyword in haystack for keyword in keywords)

    # -- state -----------------------------------------------------------------

    def restore_state(self, provider_state: Dict[str, Any]) -> None:
        for item in (provider_state.get("products") or {}).values():
            product = Product.from_dict(item)
            self._products[product.product_id] = product
        saved = provider_state.get("tcscloud") or {}
        self._unlisted = {str(k): str(v) for k, v in (saved.get("unlisted") or {}).items()}
        self._hidden_groups = {str(slug) for slug in saved.get("hidden_groups") or []}
        pids = [int(pid) for pid in list(self._products) + list(self._unlisted)]
        self._high_water = max([int(saved.get("high_water_pid") or 0)] + pids)
        self._restored = bool(saved)

    def update_state_after_success(
        self, provider_state: Dict[str, Any], products: List[Product]
    ) -> None:
        provider_state["tcscloud"] = {
            "high_water_pid": self._high_water,
            "unlisted": dict(sorted(self._unlisted.items(), key=lambda item: int(item[0]))),
            "hidden_groups": sorted(self._hidden_groups),
        }

    def additional_changes(
        self, products: List[Product], previous_state: Dict[str, Any]
    ) -> List[Change]:
        """Store-front moves: a known PID shown in, or dropped from, the store menu.

        A PID that leaves the menu but is still orderable by direct link is not
        a removal in the diff, and a hidden PID put on the store is not new, so
        both are reported here (``fields == ["listing"]``).
        """
        old_products = {}
        for item in (previous_state.get("products") or {}).values():
            product = Product.from_dict(item)
            old_products[product.product_id] = product
        changes: List[Change] = []
        for product in products:
            old = old_products.get(product.product_id)
            if old is None:
                continue
            was = bool(old.metadata.get("category_visible"))
            now = bool(product.metadata.get("category_visible"))
            if was != now:
                kind = ChangeType.NEW if now else ChangeType.REMOVED
                changes.append(Change(kind, product, old, ["listing"]))
        return changes

    def should_notify(self, change: Change) -> bool:
        # New PIDs, removals and store-front moves are always reported; stock,
        # price and name changes only for watched products (SeedNet).
        if change.type in (ChangeType.NEW, ChangeType.REMOVED):
            return super().should_notify(change)
        if not (
            change.product.metadata.get("stock_watched", True)
            or (change.old is not None and change.old.metadata.get("stock_watched") is True)
        ):
            return False
        # Order-page prices arrive after the product is first seen; the first
        # fill is not a price change.
        if (
            change.type == ChangeType.PRICE
            and change.old is not None
            and not change.old.pricing
            and (change.fields == ["pricing"] or change.old.price is None)
        ):
            return False
        if change.type == ChangeType.STOCK and not self.config.get("stock_count_alerts", True):
            return False
        return super().should_notify(change)

    # -- scan ------------------------------------------------------------------

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._requests = 0
        ignored = self._ignored()
        hidden_groups = set(self._hidden_groups)
        listed, labels, visible = self._read_categories(hidden_groups)
        for pid in ignored:
            listed.pop(pid, None)

        unlisted = dict(self._unlisted)
        missing = dict(self._missing)
        high_water = max([self._high_water] + [int(pid) for pid in listed])
        new_pids: List[str] = []

        # Listed products that left the store: still in the feed means they
        # became direct-link only; otherwise they are gone.
        for pid, old in sorted(self._products.items(), key=lambda item: int(item[0])):
            if pid in listed or pid in unlisted or old.metadata.get("listed") is not True:
                continue
            name = self._name(int(pid))
            if name is not None:
                unlisted[pid] = name

        feed_round = not self._restored and not self._unlisted and not self._products
        if not feed_round:
            # Straight onto the store, never seen in the feed before.
            new_pids.extend(
                sorted(
                    (pid for pid in listed if pid not in self._products and pid not in self._unlisted),
                    key=int,
                )
            )
        if feed_round:
            high_water = self._discover(listed, unlisted, high_water, new_pids)
        elif self._rounds % max(1, int(self.config.get("feed_every_rounds", 1))) == 0:
            high_water = self._probe(listed, unlisted, missing, high_water, new_pids)
        for pid in list(unlisted):
            if pid in listed or pid in ignored:
                unlisted.pop(pid)
                missing.pop(pid, None)

        products: Dict[str, Product] = {}
        for pid, item in listed.items():
            products[pid] = self._listed_product(item, labels, visible)
        for pid, name in unlisted.items():
            products[pid] = self._unlisted_product(pid, name)
        products = {pid: p for pid, p in products.items() if self._in_focus(p.region)}
        if self.config.get("watch_new_pids", True):
            # A PID created after the baseline may be a sale item set up ahead
            # of time: watch its stock whatever the ISP, so the opening alerts.
            for pid in new_pids:
                if pid in products:
                    products[pid].metadata["found_after_baseline"] = int(self._now())
                    products[pid].metadata["stock_watched"] = True

        checked = self._order_checks(products, new_pids, visible, hidden_groups, feed_round)
        self._feed_details(products, new_pids)

        result = [products[pid] for pid in sorted(products, key=int)]
        minimum = max(1, int(self.config.get("minimum_products", 1)))
        if sum(1 for p in result if p.metadata.get("category_visible")) < minimum:
            raise ParseError(
                "TCSCloud store listed fewer than %d products; refusing the snapshot" % minimum
            )

        # Commit only after every request in the round succeeded.
        self._products = products
        self._hidden_groups = hidden_groups
        self._unlisted = unlisted
        self._missing = missing
        self._high_water = high_water
        self._far_cursor += 1
        self._rounds += 1
        self._restored = True
        self.last_scan_stats = {
            "categories": len(labels),
            "hidden_groups": sorted(hidden_groups),
            "products": len(result),
            "listed": sum(1 for p in result if p.metadata.get("listed")),
            "unlisted": sum(1 for p in result if not p.metadata.get("listed")),
            "available": sum(p.available is True for p in result),
            "high_water_pid": high_water,
            "new_pids": new_pids,
            "order_checks": checked,
            "requests": self._requests,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return result

    def _read_categories(
        self, hidden_groups: Set[str]
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Set[str]]:
        """Read every category in the store menu, then the hidden groups.

        Returns (cards by PID, slug -> label, slugs the menu shows). A hidden
        group (configured ``extra_categories`` or learned from an order-page
        redirect, e.g. ``legacy``) that stops parsing is dropped, not fatal.
        """
        start = str(self.config.get("start_category", START_CATEGORY))
        labels: Dict[str, str] = {}
        visible: Set[str] = set()
        pending = [start]
        done: Set[str] = set()
        listed: Dict[str, Dict[str, Any]] = {}
        maximum = max(1, int(self.config.get("max_categories", 30)))
        while pending:
            slug = pending.pop(0)
            if slug in done:
                continue
            if len(done) >= maximum:
                raise ParseError("TCSCloud store exceeded max_categories=%d" % maximum)
            done.add(slug)
            cards, routes = parse_category(self._get(self.base_url + CATEGORY_PATH % slug), slug)
            for route, label in routes.items():
                visible.add(route)
                if label and not labels.get(route):
                    labels[route] = label
                if route not in done and route not in pending:
                    pending.append(route)
            self._add_cards(listed, cards, slug)
        minimum = max(1, int(self.config.get("minimum_categories", 1)))
        if len(done) < minimum:
            raise ParseError(
                "TCSCloud store showed only %d categories; expected at least %d"
                % (len(done), minimum)
            )
        hidden_groups.update(str(slug) for slug in self.config.get("extra_categories") or [])
        hidden_groups.difference_update(visible)
        # Groups are still learned (and kept in state) when not read.
        reading = sorted(hidden_groups) if self.config.get("read_hidden_groups", True) else []
        for slug in reading:
            try:
                html, final_url = self._get(self.base_url + CATEGORY_PATH % slug, final_url=True)
                path = STORE_PATH.search(unquote(final_url))
                if not path or path.group(1) != slug:
                    raise ParseError("redirected to %s" % final_url)
                cards, _ = parse_category(html, slug)
            except (FetchError, ParseError) as exc:
                if isinstance(exc, FetchError) and exc.blocked:
                    raise
                LOG.warning("TCSCloud hidden group %s dropped: %s", slug, exc)
                hidden_groups.discard(slug)
                continue
            labels[slug] = slug
            self._add_cards(listed, cards, slug)
        return listed, {slug: labels.get(slug) or slug for slug in done | set(reading)}, visible

    @staticmethod
    def _add_cards(listed: Dict[str, Dict[str, Any]], cards: List[Dict[str, Any]], slug: str) -> None:
        for card in cards:
            if card["pid"] not in listed:
                card["category_slug"] = slug
                listed[card["pid"]] = card

    def _discover(
        self,
        listed: Dict[str, Dict[str, Any]],
        unlisted: Dict[str, str],
        high_water: int,
        new_pids: List[str],
    ) -> int:
        """First run: walk the feed from feed_start_pid until a long empty run."""
        limit = max(1, int(self.config.get("discovery_misses", 20)))
        maximum = max(1, int(self.config.get("max_pid", 2000)))
        top = max([0] + [int(pid) for pid in listed])
        pid = max(1, int(self.config.get("feed_start_pid", 1)))
        misses = 0
        ignored = self._ignored()
        while pid <= maximum and (pid <= top or misses < limit):
            key = str(pid)
            if key in listed:
                misses = 0
            elif key in ignored:
                misses = 0
            else:
                name = self._name(pid)
                if name is None:
                    misses += 1
                else:
                    misses = 0
                    unlisted[key] = name
                    high_water = max(high_water, pid)
            pid += 1
        return high_water

    def _probe(
        self,
        listed: Dict[str, Dict[str, Any]],
        unlisted: Dict[str, str],
        missing: Dict[str, int],
        high_water: int,
        new_pids: List[str],
    ) -> int:
        # Re-read one unlisted name (suspected removals first).
        batch = max(0, int(self.config.get("name_batch", 1)))
        known = sorted((pid for pid in unlisted if pid not in listed), key=int)
        suspects = [pid for pid in known if pid in missing]
        rest = [pid for pid in known if pid not in missing]
        picked = suspects[:batch]
        if rest and len(picked) < batch:
            start = self._far_cursor % len(rest)
            picked += (rest[start:] + rest[:start])[: batch - len(picked)]
        for pid in picked:
            name = self._name(int(pid))
            if name is None:
                missing[pid] = missing.get(pid, 0) + 1
                # Two misses in a row before an unlisted PID counts as removed.
                if missing[pid] >= 2:
                    unlisted.pop(pid, None)
                    missing.pop(pid, None)
                continue
            missing.pop(pid, None)
            unlisted[pid] = name

        # Probe above the highest PID ever seen, plus one rotating farther PID
        # in case a deleted product left a gap.
        ahead = max(1, int(self.config.get("probe_ahead", 3)))
        span = max(0, int(self.config.get("far_probe_span", 10)))
        probes = list(range(high_water + 1, high_water + ahead + 1))
        if span:
            probes.append(high_water + ahead + 1 + self._far_cursor % span)
        ignored = self._ignored()
        for pid in probes:
            key = str(pid)
            if key in listed or key in unlisted or key in ignored:
                continue
            name = self._name(pid)
            if name is None:
                continue
            unlisted[key] = name
            new_pids.append(key)
            high_water = max(high_water, pid)
        return high_water

    def _feed_details(self, products: Dict[str, Product], new_pids: List[str]) -> None:
        """Read price and spec lines once for unlisted products from the feed.

        The order page only shows prices while a product is in stock, so a
        sold-out direct-link item would otherwise alert with no price. New PIDs
        are read in the round they appear; older ones one per round. Retired
        PIDs are skipped.
        """
        pending = sorted(
            (
                pid
                for pid, product in products.items()
                if not product.metadata.get("listed")
                and not product.metadata.get("feed_read")
                and product.metadata.get("order_page") in ("available", "sold_out")
                and (product.metadata["stock_watched"] or pid in new_pids)
            ),
            key=lambda pid: (pid not in new_pids, int(pid)),
        )
        picked = [pid for pid in pending if pid in new_pids] or pending[:1]
        for pid in picked:
            product = products[pid]
            lines = [
                " ".join(line.split())
                for line in re.split(r"<br\s*/?>", self._feed(int(pid), "description"), flags=re.I)
            ]
            lines = [re.sub(r"<[^>]+>", "", line).strip() for line in lines]
            lines = [line for line in lines if line]
            product.specs = parse_specs(lines)
            product.metadata["description"] = "\n".join(lines)
            for cycle in ("monthly", "quarterly", "semiannually", "annually"):
                price = parse_price(self._feed(int(pid), "price", billingcycle=cycle))
                if price is not None:
                    product.price = price
                    product.billing_cycle = CYCLE_KEYS[cycle]
                    break
            product.metadata["feed_read"] = True

    def _order_checks(
        self,
        products: Dict[str, Product],
        new_pids: List[str],
        visible: Set[str],
        hidden_groups: Set[str],
        everything: bool = False,
    ) -> List[str]:
        """Read order pages: stock for products without a card count, prices for all.

        Only watched products are re-read (new PIDs get one look either way).
        Retired products are re-read only every ``retired_check_seconds``; one
        that comes back shows up as a restock. A sold-out page names the
        product's group; a group missing from the menu is read as a hidden
        category from the next round on.
        """
        now = self._now()
        stock_every = max(60, int(self.config.get("order_check_seconds", 1800)))
        retired_every = max(stock_every, int(self.config.get("retired_check_seconds", 21600)))
        price_every = max(stock_every, int(self.config.get("price_refresh_seconds", 21600)))
        due: List[Tuple[int, float, int, str]] = []
        for pid, product in products.items():
            last = float(product.metadata.get("order_checked_at") or 0)
            if not product.metadata["stock_watched"]:
                # Unwatched: one look when the PID first appears, for the alert.
                if pid in new_pids:
                    due.append((0, last, int(pid), pid))
            elif product.metadata.get("stock_source") == "order_page":
                every = retired_every if product.metadata.get("order_page") == "retired" else stock_every
                if pid in new_pids or now - last >= every:
                    due.append((0 if pid in new_pids else 1, last, int(pid), pid))
            elif (product.stock or 0) > 0 and now - last >= price_every:
                # Card stock is fresh every round; the order page only adds the
                # per-cycle prices, and a sold-out one shows none.
                due.append((2, last, int(pid), pid))
        due.sort()
        batch = len(due) if everything else max(0, int(self.config.get("order_check_batch", 2)))
        picked = [item[3] for item in due if item[0] == 0]
        picked += [item[3] for item in due if item[0] != 0][: max(0, batch - len(picked))]
        for pid in picked:
            product = products[pid]
            result = self._order_page(int(pid))
            product.metadata["order_checked_at"] = int(now)
            product.metadata["order_page"] = result["status"]
            if result["status"] == "unknown":
                LOG.warning(
                    "TCSCloud pid %s order page not recognized (%s): %s",
                    pid,
                    result.get("url"),
                    result.get("excerpt"),
                )
                continue
            group = result.get("group")
            if group:
                product.metadata["order_group"] = group
                if group not in visible:
                    hidden_groups.add(group)
            if product.metadata.get("stock_source") == "order_page":
                product.available = result["status"] == "available"
            if result.get("pricing"):
                product.pricing = result["pricing"]
                # The feed price (a real total) wins; the order page shows
                # month-equivalent figures.
                if not product.metadata.get("listed") and product.price is None:
                    cycle, label = next(iter(result["pricing"].items()))
                    product.billing_cycle = cycle
                    product.price = label.rsplit(" - ", 1)[-1] if label else product.price
        return picked

    # -- products ----------------------------------------------------------------

    def _carry(self, pid: str) -> Optional[Product]:
        return self._products.get(pid)

    def _listed_product(
        self, item: Dict[str, Any], labels: Dict[str, str], visible: Set[str]
    ) -> Product:
        pid = item["pid"]
        old = self._carry(pid)
        stock = item["stock"]
        by_card = stock is not None
        shown = item["category_slug"] in visible
        metadata: Dict[str, Any] = {
            "listed": True,
            "category_visible": shown,
            "discovery": {
                "type": "catalog",
                "hidden": not shown,
                "source": "store_category" if shown else "hidden_group",
            },
            "category_slug": item["category_slug"],
            "card_url": urljoin(self.base_url, item["card_url"]) if item["card_url"] else None,
            "description": "\n".join(item["description"]),
            "price_from": item["price_from"],
            "stock_source": "category_card" if by_card else "order_page",
            "stock_watched": self._watched(item["name"], item["card_url"], "\n".join(item["description"])),
        }
        available: Optional[bool] = stock > 0 if by_card else None
        pricing: Dict[str, str] = {}
        if old is not None:
            for key in ("order_checked_at", "order_page", "found_after_baseline"):
                if key in old.metadata:
                    metadata[key] = old.metadata[key]
            if metadata.get("found_after_baseline") and self.config.get("watch_new_pids", True):
                metadata["stock_watched"] = True
            pricing = dict(old.pricing)
            if (
                not by_card
                and metadata["stock_watched"]
                and old.metadata.get("stock_source") == "order_page"
            ):
                available = old.available
        name = item["name"]
        label = labels.get(item["category_slug"], item["category_slug"])
        if not shown:
            label = "%s（隐藏分组）" % label
        return Product(
            provider=self.name,
            product_id=pid,
            name=name,
            category=label,
            region=region_of("%s %s" % (name, label), str(self.config.get("default_region", "TW"))),
            price=item["price"],
            billing_cycle=item["billing_cycle"],
            stock=stock,
            available=available,
            url=self.base_url + ORDER_PATH % pid,
            specs=parse_specs(item["description"]),
            metadata=metadata,
            pricing=pricing,
        )

    def _unlisted_product(self, pid: str, name: str) -> Product:
        old = self._carry(pid)
        metadata: Dict[str, Any] = {
            "listed": False,
            "discovery": {"type": "feed", "hidden": True, "source": "productsinfo_feed"},
            # Telegram titles a new one as a hidden, possibly pre-sale, link.
            "hidden_direct_link": True,
            "stock_source": "order_page",
            "stock_watched": self._watched(name),
        }
        product = Product(
            provider=self.name,
            product_id=pid,
            name=name,
            category="未上架（仅直链）",
            region=region_of(name, str(self.config.get("default_region", "TW"))),
            url=self.base_url + ORDER_PATH % pid,
            metadata=metadata,
        )
        if old is not None:
            for key in ("order_checked_at", "order_page", "order_group", "found_after_baseline"):
                if key in old.metadata:
                    metadata[key] = old.metadata[key]
            product.pricing = dict(old.pricing)
            product.price = old.price
            product.billing_cycle = old.billing_cycle
            product.specs = dict(old.specs)
            if old.metadata.get("listed"):
                # It was listed until now: keep what the card said.
                metadata["description"] = old.metadata.get("description")
            else:
                for key in ("feed_read", "description"):
                    if key in old.metadata:
                        metadata[key] = old.metadata[key]
            metadata["stock_watched"] = self._watched(name, metadata.get("description")) or bool(
                metadata.get("found_after_baseline") and self.config.get("watch_new_pids", True)
            )
            if metadata["stock_watched"] and old.metadata.get("stock_source") == "order_page":
                product.available = old.available
            # An unwatched PID is never re-read, so an old reading would go stale.
        return product

    # -- HTTP ----------------------------------------------------------------------

    def _name(self, pid: int) -> Optional[str]:
        value = " ".join(self._feed(pid, "name").split())
        return None if value == NOT_FOUND else value

    def _feed(self, pid: int, get: str, **extra: str) -> str:
        params = {"pid": str(pid), "get": get}
        if get == "price":
            params["currency"] = str(self.config.get("currency_id", 1))
        params.update(extra)
        return parse_feed(self._get(self.base_url + FEED_PATH, params))

    def _order_page(self, pid: int) -> Dict[str, Any]:
        # A fresh WHMCS session each time so the probe never piles items into
        # one cart.
        self.session.cookies.clear()
        text, final_url = self._get(self.base_url + ORDER_PATH % pid, final_url=True)
        return parse_order_page(text, final_url)

    def _get(self, url: str, params: Optional[Dict[str, str]] = None, final_url: bool = False) -> Any:
        attempts = max(1, int(self.config.get("retry_attempts", 2)))
        spacing = max(3.0, float(self.config.get("request_spacing_seconds", 5)))
        for attempt in range(attempts):
            if self._last_request is not None:
                wait = spacing - (self._clock() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._clock()
            self._requests += 1
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    continue
                raise FetchError("request failed for %s (%s)" % (url, type(exc).__name__)) from None
            text = response.text
            if response.status_code in (403, 429) or is_challenge_page(text):
                # Never retry a block: back off through the scheduler instead.
                raise FetchError(
                    "HTTP %s for %s (challenge/block page)" % (response.status_code, url),
                    response.status_code,
                    True,
                )
            if response.status_code >= 500 and attempt + 1 < attempts:
                continue
            if response.status_code >= 400:
                raise FetchError("HTTP %s for %s" % (response.status_code, url), response.status_code)
            if final_url:
                return text, str(getattr(response, "url", url) or url)
            return text
        raise FetchError("request failed for %s" % url)
