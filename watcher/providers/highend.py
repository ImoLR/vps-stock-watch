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


def parse_price(value: str) -> Optional[str]:
    """Return a display price, or None for a cycle the product does not offer (-1.00)."""
    value = " ".join(value.split())
    if not value or re.search(r"-\s*1\.00\b", value):
        return None
    return value


class HighendProvider(BaseProvider):
    """HighEndNetwork (HEN) WHMCS store, read through the public data feed.

    Cloudflare puts an interactive challenge on ``/store`` and ``cart.php``
    for every server IP we tried (plain HTTP and headed Chromium alike), but
    ``feeds/productsinfo.php`` is open. It answers name / price / order URL
    for any PID, including sale products that only exist as a direct link
    (``/store/promotion/...``), so new PIDs are found before anyone posts the
    link. It has no stock field: products carry ``available=None`` and the
    alerts are new / renamed / repriced / removed PIDs.

    Each round re-reads a few known PIDs in rotation, probes just above the
    highest PID seen (WHMCS IDs only grow) and refreshes one product's prices
    (plus its description-derived specs, once),
    all one request at a time ``request_spacing_seconds`` apart. The catalog
    survives restarts through ``restore_state``; only the very first run walks
    every PID.

    With ``focus_regions`` set, only those regions (plus unclassified names
    such as ``test``) are re-read, and only those regions get prices. Other
    regions stay in the catalog so the PID high-water mark holds, but they
    are never re-read and never notify.
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

    def _in_focus(self, region: Optional[str], unknown: bool = True) -> bool:
        focus = {str(item).upper() for item in self.config.get("focus_regions") or []}
        if not focus:
            return True
        if region is None:
            return unknown
        return region.upper() in focus

    def should_notify(self, change: Change) -> bool:
        products = [change.product] + ([change.old] if change.old else [])
        if not any(self._in_focus(product.region) for product in products):
            return False
        # Prices are filled in lazily after the baseline walk; the first fill
        # is not a price change.
        if (
            change.type == ChangeType.PRICE
            and change.old is not None
            and not change.old.pricing
        ):
            return False
        return super().should_notify(change)

    # -- scan ------------------------------------------------------------------

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._requests = 0
        if not self._catalog:
            found = self._discover_all()
        else:
            found = self._incremental()
        self.last_scan_stats = {
            "products": len(self._catalog),
            "high_water_pid": self._high_water,
            "new_pids": found,
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
        known = [pid for pid in sorted(catalog, key=int) if self._in_focus(catalog[pid].region)]
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
            if self._in_focus(product.region):
                self._fill_prices(product)
            catalog[str(pid)] = product
            found.append(str(pid))
            high_water = max(high_water, pid)

        # 3. Refresh one product's prices, oldest first. Every other round
        #    goes to a not-yet-priced product first (a sale item about to
        #    open), so one of those cannot starve the rest.
        unpriced_first = self._rounds % 2 == 0
        for _ in range(max(0, int(self.config.get("price_batch", 1)))):
            candidates = [
                pid
                for pid in sorted(catalog, key=int)
                if pid not in found and self._in_focus(catalog[pid].region, unknown=False)
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
        self._rounds += 1
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
            metadata={"stock_source": "none (store pages behind Cloudflare challenge)"},
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
            return text
        raise FetchError("request failed for %s" % url)
