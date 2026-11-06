from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from ..models import Change, ChangeType, Product
from .base import BaseProvider, FetchError, ParseError, is_challenge_page


LOG = logging.getLogger(__name__)

BASE_URL = "https://billing.highendnetwork.com/"
FEED_PATH = "feeds/productsinfo.php"
ORDER_URL = "cart.php?a=add&pid=%s"
NOT_FOUND = "Product ID Not Found"
CYCLES = ("monthly", "quarterly", "semiannually", "annually")
FEED_BODY = re.compile(r"^\s*document\.write\('(?P<value>.*)'\);?\s*$", re.S)
# Cloudflare challenges the /store/... paths only; WHMCS serves the same pages
# at index.php?rp=/store/... and that route is not challenged.
STORE_ROUTE = "index.php"
PRODUCT_HEADER = re.compile(
    r'<header>\s*<span id="product(?P<pid>\d+)-name">(?P<name>.*?)</span>(?P<rest>.*?)</header>',
    re.S,
)
QTY = re.compile(r"(\d+)\s+Available", re.I)
OUT_OF_STOCK = "Out of Stock"
# Name prefix -> region. Names look like HKG.P0.Micro, SIN.Standard.Mini, Seednet.Matcha.
REGIONS = {
    "hkg": "HK",
    "sin": "SG",
    "hinet": "TW",
    "seednet": "TW",
    "so-net": "TW",
    "sonet": "TW",
    "tpe": "TW",
    "twn": "TW",
    "taiwan": "TW",
}


def region_of(name: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (category, region) from a name such as ``Seednet.Matcha``."""
    prefix = name.split(".", 1)[0].strip()
    return prefix or None, REGIONS.get(prefix.casefold())


def parse_feed(body: str) -> str:
    """Unwrap a WHMCS ``productsinfo.php`` ``document.write('...')`` reply."""
    match = FEED_BODY.match(body)
    if not match:
        raise ParseError("HighEnd feed reply is not document.write(): %r" % body[:120])
    value = match.group("value")
    return value.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


SPEC_TERMS = (
    ("cpu", ("vcore", "vcpu", "core")),
    ("ram", ("ram", "memory")),
    ("disk", ("ssd", "nvme", "hdd", "disk")),
    ("traffic", ("traffic",)),
    ("bandwidth", ("mbps", "gbps", "port")),
    ("ipv4", ("ipv4",)),
    ("ipv6", ("ipv6",)),
)


def parse_specs(description: str) -> Dict[str, str]:
    """Pick spec lines out of a feed description such as ``1 vCore<br />1 G RAM``."""
    specs: Dict[str, str] = {}
    for line in re.split(r"<br\s*/?>", description, flags=re.I):
        line = " ".join(re.sub(r"<[^>]+>", " ", line).split())
        lowered = line.casefold()
        for key, terms in SPEC_TERMS:
            if line and key not in specs and any(term in lowered for term in terms):
                specs[key] = line
                break
    return specs


def parse_store_page(body: str) -> Dict[str, Dict[str, Any]]:
    """Products on a store category page: {pid: {name, stock, link}}.

    ``stock`` is the ``N Available`` count, or None when the product does not
    track stock.
    """
    if 'id="products"' not in body:
        raise ParseError("HighEnd store page has no product list")
    products: Dict[str, Dict[str, Any]] = {}
    for match in PRODUCT_HEADER.finditer(body):
        pid = match.group("pid")
        qty = QTY.search(re.sub(r"<[^>]+>", " ", match.group("rest")))
        link = re.search(
            r'<a href="(?P<href>[^"]+)"[^>]*id="product%s-order-button"' % pid, body
        )
        products[pid] = {
            "name": " ".join(re.sub(r"<[^>]+>", " ", match.group("name")).split()),
            "stock": int(qty.group(1)) if qty else None,
            "link": link.group("href") if link else None,
        }
    return products


def parse_price(value: str) -> Optional[str]:
    """Return a display price, or None for a cycle the product does not offer (-1.00)."""
    value = " ".join(value.split())
    if not value or re.search(r"-\s*1\.00\b", value):
        return None
    return value


class HighendProvider(BaseProvider):
    """HighEndNetwork (HEN) WHMCS store.

    Cloudflare puts an interactive challenge on ``/store/...`` and
    ``cart.php`` (for server and browser alike), but WHMCS also serves the
    store at ``index.php?rp=/store/...``, which is not challenged. Every
    round reads the ``store_pages`` categories there (``N Available`` per
    product) and the ``direct_links`` of hidden sale items (a redirect to
    ``cart.php?a=confproduct`` means orderable, an "Out of Stock" page means
    sold out).

    The catalog itself comes from the open ``feeds/productsinfo.php``, which
    answers name / price for any PID, including direct-link sale products, so
    new PIDs are found before anyone posts the link. Every
    ``feed_every_rounds`` rounds the feed re-reads a few known PIDs in
    rotation, probes just above the highest PID seen (WHMCS IDs only grow)
    and refreshes one watched product's prices (plus its description-derived
    specs, once). All requests go one at a time ``request_spacing_seconds``
    apart. The catalog survives restarts through ``restore_state``; only the
    very first run walks every PID.

    New and removed PIDs, and products put on or taken off the watched store
    pages, alert for every product. Price, stock and name changes alert only
    for names matching ``watch_keywords``. With ``watched_only`` every alert,
    the name sweep and new-PID pricing are limited to watched names (the
    sweep still re-reads unclassified names such as ``test``, which a sale
    item may be renamed from).
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_url = str(config.get("base_url", BASE_URL))
        self._catalog: Dict[str, Product] = {}
        self._missing: Dict[str, int] = {}
        self._high_water = 0
        self._sweep_cursor = 0
        self._far_cursor = 0
        self._rounds = 0
        self._feed_rounds = 0
        self._last_request: Optional[float] = None
        self._requests = 0
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
        self.last_scan_stats: Dict[str, Any] = {}

    # -- state -----------------------------------------------------------------

    def restore_state(self, provider_state: Dict[str, Any]) -> None:
        for item in (provider_state.get("products") or {}).values():
            product = Product.from_dict(item)
            self._catalog[product.product_id] = product
        pids = [int(pid) for pid in self._catalog]
        self._high_water = max([int(provider_state.get("high_water_pid") or 0)] + pids)

    def update_state_after_success(
        self, provider_state: Dict[str, Any], products: List[Product]
    ) -> None:
        provider_state["high_water_pid"] = self._high_water

    def _watched(self, product: Product) -> bool:
        keywords = [str(item).casefold() for item in self.config.get("watch_keywords") or []]
        if not keywords:
            return True
        name = product.name.casefold()
        return any(keyword in name for keyword in keywords)

    def _tracked(self, product: Product) -> bool:
        """Whether a product is re-read and priced by the feed at all."""
        if not self.config.get("watched_only"):
            return True
        return self._watched(product) or product.region is None

    def additional_changes(
        self, products: List[Product], previous_state: Dict[str, Any]
    ) -> List[Change]:
        """A known PID put on, or taken off, a watched store page (``["listing"]``)."""
        old_products = {}
        for item in (previous_state.get("products") or {}).values():
            product = Product.from_dict(item)
            old_products[product.product_id] = product
        changes: List[Change] = []
        for product in products:
            old = old_products.get(product.product_id)
            if old is None or not old.metadata.get("store_seen"):
                continue
            was = old.metadata.get("listed_in")
            now = product.metadata.get("listed_in")
            if was != now:
                kind = ChangeType.NEW if now else ChangeType.REMOVED
                changes.append(Change(kind, product, old, ["listing"]))
        return changes

    def should_notify(self, change: Change) -> bool:
        # New / removed PIDs and store-page moves alert for every product
        # (unless watched_only); price, stock and name changes only for
        # watched ones.
        if change.type in (ChangeType.NEW, ChangeType.REMOVED) and not self.config.get(
            "watched_only"
        ):
            return super().should_notify(change)
        products = [change.product] + ([change.old] if change.old else [])
        if not any(self._watched(product) for product in products):
            return False
        # Prices are filled in lazily after the baseline walk; the first fill
        # is not a price change. A product already read with no price at all
        # (a sale item not open yet) does alert when it gets one.
        if (
            change.type == ChangeType.PRICE
            and change.old is not None
            and not change.old.pricing
            and not change.old.metadata.get("priced_at")
        ):
            return False
        return super().should_notify(change)

    # -- scan ------------------------------------------------------------------

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._requests = 0
        listed, direct = self._read_store()
        every = max(1, int(self.config.get("feed_every_rounds", 1)))
        if not self._catalog:
            found = self._discover_all()
        elif self._rounds % every == 0:
            found = self._incremental()
        else:
            found = []
        found += self._apply_store(listed, direct)
        self._rounds += 1
        self.last_scan_stats = {
            "products": len(self._catalog),
            "high_water_pid": self._high_water,
            "new_pids": found,
            "listed": len(listed),
            "requests": self._requests,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return [self._catalog[pid] for pid in sorted(self._catalog, key=int)]

    def _discover_all(self) -> List[str]:
        """First run: walk PIDs from 1 until a long run of empty IDs."""
        limit = max(1, int(self.config.get("discovery_misses", 40)))
        maximum = max(1, int(self.config.get("max_pid", 2000)))
        found: List[str] = []
        misses = 0
        pid = 1
        while misses < limit and pid <= maximum:
            name = self._name(pid)
            if name is None:
                misses += 1
            else:
                misses = 0
                self._catalog[str(pid)] = self._product(str(pid), name)
                self._high_water = max(self._high_water, pid)
                found.append(str(pid))
            pid += 1
        minimum = max(1, int(self.config.get("minimum_products", 1)))
        if len(self._catalog) < minimum:
            count = len(self._catalog)
            self._catalog.clear()
            raise ParseError(
                "HighEnd feed found only %d products; expected at least %d" % (count, minimum)
            )
        return found

    def _incremental(self) -> List[str]:
        catalog = dict(self._catalog)
        missing = dict(self._missing)
        high_water = self._high_water

        # 1. Rotate through known PIDs, confirming suspected removals first.
        batch = max(0, int(self.config.get("sweep_batch", 5)))
        known = [pid for pid in sorted(catalog, key=int) if self._tracked(catalog[pid])]
        suspects = [pid for pid in known if pid in missing]
        rotation = [pid for pid in known if pid not in missing]
        picked = suspects[:batch]
        cursor = self._sweep_cursor
        while rotation and len(picked) < batch and len(picked) < len(known):
            pid = rotation[cursor % len(rotation)]
            cursor += 1
            if pid not in picked:
                picked.append(pid)
        for pid in picked:
            name = self._name(int(pid))
            if name is None:
                missing[pid] = missing.get(pid, 0) + 1
                # Two misses in a row before a PID counts as removed.
                if missing[pid] >= 2:
                    del catalog[pid]
                    del missing[pid]
                continue
            missing.pop(pid, None)
            if name != catalog[pid].name:
                category, region = region_of(name)
                catalog[pid] = self._copy(catalog[pid], name=name, category=category, region=region)

        # 2. Probe above the highest PID ever seen, plus one rotating far PID
        #    in case deleted test products left a gap.
        ahead = max(1, int(self.config.get("probe_ahead", 3)))
        span = max(0, int(self.config.get("far_probe_span", 10)))
        probes = list(range(high_water + 1, high_water + ahead + 1))
        if span:
            probes.append(high_water + ahead + 1 + self._far_cursor % span)
        found: List[str] = []
        for pid in probes:
            if str(pid) in catalog:
                continue
            name = self._name(pid)
            if name is None:
                continue
            product = self._product(str(pid), name)
            if self._tracked(product):
                self._fill_prices(product)
            catalog[str(pid)] = product
            found.append(str(pid))
            high_water = max(high_water, pid)

        # 3. Refresh one product's prices, oldest first. Every other round
        #    goes to a not-yet-priced product first (a sale item about to
        #    open), so one of those cannot starve the rest.
        unpriced_first = self._feed_rounds % 2 == 0
        for _ in range(max(0, int(self.config.get("price_batch", 1)))):
            candidates = [
                pid
                for pid in sorted(catalog, key=int)
                if pid not in found and self._watched(catalog[pid])
            ]
            if not candidates:
                break
            pid = min(
                candidates,
                key=lambda value: (
                    unpriced_first and bool(catalog[value].pricing),
                    catalog[value].metadata.get("priced_at", 0),
                    int(value),
                ),
            )
            product = self._copy(catalog[pid])
            self._fill_prices(product)
            catalog[pid] = product

        # Commit only after every request in the round succeeded.
        self._catalog = catalog
        self._missing = missing
        self._high_water = high_water
        self._sweep_cursor = cursor
        self._far_cursor += 1
        self._feed_rounds += 1
        return found

    # -- store pages ---------------------------------------------------------------

    def _read_store(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Tuple[bool, str]]]:
        """Read the watched category pages and the hidden direct links."""
        listed: Dict[str, Dict[str, Any]] = {}
        for path in self.config.get("store_pages") or []:
            body = self._fetch(self.base_url + STORE_ROUTE, {"rp": str(path)})
            for pid, info in parse_store_page(body).items():
                info["page"] = str(path)
                listed.setdefault(pid, info)
        direct: Dict[str, Tuple[bool, str]] = {}
        for pid, path in (self.config.get("direct_links") or {}).items():
            pid, path = str(pid), str(path)
            if pid in listed:
                continue
            response = self._request(
                self.base_url + STORE_ROUTE, {"rp": path}, allow_redirects=False
            )
            location = str((getattr(response, "headers", None) or {}).get("Location", ""))
            if 300 <= response.status_code < 400 and "confproduct" in location:
                direct[pid] = (True, path)
            elif response.status_code == 200 and OUT_OF_STOCK in response.text:
                direct[pid] = (False, path)
            else:
                raise ParseError(
                    "HighEnd direct link %s: unexpected reply HTTP %s %s"
                    % (path, response.status_code, location)
                )
            # An orderable product was just put in this session's cart; drop it.
            cookies = getattr(self.session, "cookies", None)
            if cookies is not None:
                cookies.clear()
        return listed, direct

    def _apply_store(
        self, listed: Dict[str, Dict[str, Any]], direct: Dict[str, Tuple[bool, str]]
    ) -> List[str]:
        """Put store stock on the catalog; returns PIDs first seen on a store page."""
        found: List[str] = []
        for pid, info in listed.items():
            if pid not in self._catalog and info["name"]:
                product = self._product(pid, info["name"])
                if self._tracked(product):
                    self._fill_prices(product)
                self._catalog[pid] = product
                self._high_water = max(self._high_water, int(pid))
                found.append(pid)
        root = self.base_url.rstrip("/")
        for pid, current in list(self._catalog.items()):
            product = self._copy(current)
            product.metadata["store_seen"] = True
            info = listed.get(pid)
            if info is not None:
                product.stock = info["stock"]
                product.available = None if info["stock"] is None else info["stock"] > 0
                product.metadata["listed_in"] = info["page"]
                product.metadata["stock_source"] = "store page"
                if info["link"]:
                    product.url = root + info["link"]
            elif pid in direct:
                product.available, path = direct[pid]
                product.stock = None
                product.metadata["listed_in"] = None
                product.metadata["stock_source"] = "direct link"
                product.url = root + path
            else:
                product.available = None
                product.stock = None
                product.metadata["listed_in"] = None
                product.metadata["stock_source"] = "none (not on a watched store page)"
            self._catalog[pid] = product
        return found

    # -- products ----------------------------------------------------------------

    def _product(self, pid: str, name: str) -> Product:
        category, region = region_of(name)
        return Product(
            provider=self.name,
            product_id=pid,
            name=name,
            category=category,
            region=region,
            url=self.base_url + ORDER_URL % pid,
        )

    @staticmethod
    def _copy(product: Product, **changes: Any) -> Product:
        value = product.to_dict()
        value["metadata"] = dict(value.get("metadata") or {})
        value.update(changes)
        return Product.from_dict(value)

    def _fill_prices(self, product: Product) -> None:
        if not product.metadata.get("specs_read"):
            product.specs = parse_specs(self._feed(int(product.product_id), "description"))
            product.metadata["specs_read"] = True
        pricing: Dict[str, str] = {}
        for cycle in CYCLES:
            price = parse_price(self._feed(int(product.product_id), "price", billingcycle=cycle))
            if price is not None:
                pricing[cycle] = price
        product.pricing = pricing
        first = next(iter(pricing.items()), None)
        product.price = first[1] if first else None
        product.billing_cycle = first[0] if first else None
        product.metadata["orderable_cycles"] = len(pricing)
        product.metadata["priced_at"] = int(time.time())

    # -- HTTP ----------------------------------------------------------------------

    def _name(self, pid: int) -> Optional[str]:
        value = " ".join(self._feed(pid, "name").split())
        return None if value == NOT_FOUND else value

    def _feed(self, pid: int, get: str, **extra: str) -> str:
        params = {"pid": str(pid), "get": get}
        if get == "price":
            params["currency"] = str(self.config.get("currency_id", 1))
        params.update(extra)
        return parse_feed(self._fetch(self.base_url + FEED_PATH, params))

    def _fetch(self, url: str, params: Dict[str, str]) -> str:
        return self._request(url, params).text

    def _request(self, url: str, params: Dict[str, str], allow_redirects: bool = True) -> Any:
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
                response = self.session.get(
                    url, params=params, timeout=self.timeout, allow_redirects=allow_redirects
                )
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
            return response
        raise FetchError("request failed for %s" % url)
