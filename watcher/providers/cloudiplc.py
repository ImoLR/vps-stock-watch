from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag

from ..models import Change, ChangeType, Product
from .base import BaseProvider, FetchError, ParseError, is_challenge_page


LOG = logging.getLogger(__name__)

BASE_URL = "https://www.cloudiplc.com/"
ORDER_URL = BASE_URL + "cart.php?a=add&pid=%s"
FEED_URL = "feeds/productsinfo.php?pid=%s&get=name"
FEED_BODY = re.compile(r"^\s*document\.write\('(?P<value>.*)'\);?\s*$", re.S)
FEED_NOT_FOUND = "Product ID Not Found"
HIDDEN_CATEGORY = "隐藏链接"
GROUP_LINK = re.compile(r"""href=["']/?cart\.php\?gid=([0-9]+)["']""")
EMPTY_GROUP_MARKER = "产品组不包含任何可见的产品"
SOLD_OUT_MARKERS = ("缺货", "缺貨", "out of stock", "库存不足", "售罄")
EDGEONE_CHALLENGE = ("EO_Bot_Ssid", "__tst_status")
SPEC_COLUMNS = (
    (("cpu", "处理器"), "cpu"),
    (("内存",), "ram"),
    (("硬盘",), "disk"),
    (("地址", "ip"), "ipv4"),
    (("流量",), "traffic"),
    (("带宽",), "bandwidth"),
)


def _text(node: Optional[Tag]) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


class CloudiplcProvider(BaseProvider):
    """CloudIPLC (云际互联) WHMCS store with the NeWorld-New order form.

    Category pages are plain HTTP and list every product row, but carry no
    stock signal. The only place WHMCS reveals stock is ``cart.php?a=add``,
    which Tencent EdgeOne guards with a JavaScript cookie check, so those
    order pages are opened in an isolated Chromium and cached for
    ``order_page_refresh_seconds``. New PIDs are checked on the scan that
    first sees them.

    Hidden sale links (no group lists them) are found through the open
    ``feeds/productsinfo.php``: every ``hidden_probe_every_rounds`` rounds it
    asks for the ``hidden_probe_ahead`` PIDs just above the highest PID ever
    seen (WHMCS IDs only grow). A PID the feed names but no group lists joins
    the snapshot as a hidden direct link and stays there across restarts.
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.session.headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        self._order_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._order_failed_at: Optional[float] = None
        self._hidden: Dict[str, Product] = {}
        self._high_water = 0
        self._rounds = 0
        self._last_request: Optional[float] = None
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
        self.last_scan_stats: Dict[str, Any] = {}

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        requests_made = 0
        base_url = str(self.config.get("base_url", BASE_URL))
        store_html = self._fetch(base_url + "cart.php")
        requests_made += 1
        pages: Dict[str, str] = {}
        active = self._active_group(store_html)
        if active:
            pages[active] = store_html
        pending = self._group_ids(store_html)
        if not pending:
            raise ParseError("CloudIPLC store page lists no product groups")
        maximum = max(1, int(self.config.get("max_categories", 40)))
        while pending - set(pages):
            gid = min(pending - set(pages), key=int)
            if len(pages) >= maximum:
                raise ParseError("CloudIPLC discovery exceeded max_categories=%d" % maximum)
            pages[gid] = self._fetch(base_url + "cart.php?gid=%s" % gid)
            requests_made += 1
            pending |= self._group_ids(pages[gid])

        labels = self._group_labels(pages)
        products: Dict[str, Product] = {}
        for gid in sorted(pages, key=int):
            for product in self.parse_group(pages[gid], gid, labels.get(gid)):
                if product.product_id in products:
                    raise ParseError(
                        "CloudIPLC PID %s appears in more than one group" % product.product_id
                    )
                products[product.product_id] = product

        minimum = max(1, int(self.config.get("minimum_products", 1)))
        if len(products) < minimum:
            raise ParseError(
                "CloudIPLC catalog found only %d products; expected at least %d"
                % (len(products), minimum)
            )
        for pid, product in self._direct_products().items():
            products.setdefault(pid, product)
        probed, found = self._probe_hidden(products)
        requests_made += probed
        for pid in list(self._hidden):
            if pid in products:
                # Listed in a group (or configured) now; no longer hidden.
                self._hidden.pop(pid)
            else:
                products[pid] = self._hidden[pid]
        self._rounds += 1
        result = [products[pid] for pid in sorted(products, key=int)]
        order_pages = self._apply_order_pages(result)
        self.last_scan_stats = {
            "groups": sorted(pages, key=int),
            "products": len(result),
            "requests": requests_made,
            "high_water_pid": self._high_water,
            "hidden_found": found,
            "order_pages_checked": order_pages,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return result

    def parse_group(self, html: str, gid: str, label: Optional[str] = None) -> List[Product]:
        soup = BeautifulSoup(html, "lxml")
        container = soup.select_one("#order-standard_cart #products .pt")
        if container is None:
            raise ParseError("CloudIPLC group %s has no product table" % gid)
        heading = _text(soup.select_one("#order-standard_cart h1"))
        rows = container.select("a.pt__row[href]")
        if not rows:
            if EMPTY_GROUP_MARKER in _text(soup.select_one("#order-standard_cart")):
                return []
            raise ParseError("CloudIPLC group %s contained no product rows" % gid)
        columns = [_text(cell).casefold() for cell in container.select(".pt__header .pt__cell")]
        products = []
        for row in rows:
            match = re.search(r"[?&]pid=([0-9]+)", str(row.get("href")))
            if not match:
                raise ParseError("CloudIPLC group %s has a row without a PID" % gid)
            products.append(self._parse_row(row, match.group(1), gid, label or heading, heading, columns))
        return products

    def _parse_row(
        self,
        row: Tag,
        pid: str,
        gid: str,
        label: str,
        heading: str,
        columns: List[str],
    ) -> Product:
        cells = row.select(".pt__row-content > .pt__cell")
        name = _text(cells[0].select_one("strong")) if cells else ""
        if not name:
            raise ParseError("CloudIPLC PID %s has no product name" % pid)
        price_node = row.select_one(".pt__cell-price .js-price")
        price = _text(price_node).replace(" /", "/").replace("/ ", "/")
        if not price:
            raise ParseError("CloudIPLC PID %s has no price" % pid)
        specs: Dict[str, str] = {}
        for header, cell in zip(columns[1:], cells[1:]):
            if "pt__cell-price" in cell.get("class", []):
                continue
            value = _text(cell)
            for terms, key in SPEC_COLUMNS:
                if value and any(term in header for term in terms):
                    specs.setdefault(key, value)
                    break
        remarks = {}
        for span in row.select(".pt__remark > span"):
            title = _text(span.select_one("strong")).rstrip("：:")
            value = _text(span.select_one("span"))
            if title and value:
                remarks[title] = value
        if remarks.get("线路"):
            specs["route"] = remarks["线路"]
        return Product(
            provider=self.name,
            product_id=pid,
            name=name,
            category=label,
            region=heading or None,
            price=price,
            billing_cycle="annually" if "按年" in price else "monthly",
            stock=None,
            available=None,
            url=ORDER_URL % pid,
            specs=specs,
            metadata={
                "group_id": gid,
                "group_heading": heading,
                "remarks": remarks,
                "price_source": "category_row",
            },
        )

    def _direct_products(self) -> Dict[str, Product]:
        """Sale items reachable only by a direct order link (no group lists them).

        Name and category come from config so they stay stable while the order
        page is sold out or unchecked; price and stock come from the order page.
        """
        products = {}
        for entry in self.config.get("direct_products") or []:
            pid = str(entry["pid"])
            products[pid] = Product(
                provider=self.name,
                product_id=pid,
                name=str(entry.get("name") or "PID %s" % pid),
                category=str(entry.get("category") or "活动直链"),
                url=ORDER_URL % pid,
                metadata={"direct_link": True},
            )
        return products

    def restore_state(self, provider_state: Dict[str, Any]) -> None:
        pids = [int(provider_state.get("high_water_pid") or 0)]
        for item in (provider_state.get("products") or {}).values():
            product = Product.from_dict(item)
            pids.append(int(product.product_id))
            if product.metadata.get("hidden_direct_link"):
                self._hidden[product.product_id] = product
        self._high_water = max(pids)

    def update_state_after_success(
        self, provider_state: Dict[str, Any], products: List[Product]
    ) -> None:
        provider_state["high_water_pid"] = self._high_water

    def _probe_hidden(self, products: Dict[str, Product]) -> Tuple[int, List[str]]:
        """Ask the feed for PIDs above the highest seen; return (requests, hidden PIDs)."""
        # The baseline covers PIDs that existed before probing began, so old
        # hidden PIDs above the listed ones never alert as new.
        baseline = int(self.config.get("hidden_probe_baseline_pid") or 0)
        self._high_water = max([self._high_water, baseline] + [int(pid) for pid in products])
        every = max(1, int(self.config.get("hidden_probe_every_rounds", 3)))
        if not self.config.get("hidden_probe", False) or self._rounds % every:
            return 0, []
        base = self._high_water
        requests_made = 0
        found: List[str] = []
        for pid in range(base + 1, base + 1 + max(1, int(self.config.get("hidden_probe_ahead", 3)))):
            try:
                name = self._feed_name(str(pid))
            except FetchError as exc:
                if exc.blocked:
                    raise
                LOG.warning("%s: hidden-link probe stopped: %s", self.name, exc)
                break
            except ParseError as exc:
                LOG.warning("%s: hidden-link probe stopped: %s", self.name, exc)
                break
            finally:
                requests_made += 1
            if name is None:
                continue
            self._high_water = max(self._high_water, pid)
            key = str(pid)
            if key in products or key in self._hidden:
                continue
            LOG.info("%s: hidden PID %s found through the feed: %s", self.name, key, name)
            self._hidden[key] = Product(
                provider=self.name,
                product_id=key,
                name=name,
                category=HIDDEN_CATEGORY,
                url=ORDER_URL % key,
                metadata={"hidden_direct_link": True, "found_at": int(time.time())},
            )
            found.append(key)
        return requests_made, found

    def _feed_name(self, pid: str) -> Optional[str]:
        """A PID's name from the open WHMCS feed, or None if it does not exist."""
        url = str(self.config.get("base_url", BASE_URL)) + FEED_URL % pid
        self._space_requests()
        self._last_request = self._clock()
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FetchError("request failed for %s (%s)" % (url, type(exc).__name__)) from None
        text = response.text
        if (
            response.status_code in (403, 429)
            or is_challenge_page(text)
            or all(marker in text for marker in EDGEONE_CHALLENGE)
        ):
            raise FetchError(
                "HTTP %s for %s (challenge/block page)" % (response.status_code, url),
                response.status_code,
                True,
            )
        if response.status_code >= 400:
            raise FetchError("HTTP %s for %s" % (response.status_code, url), response.status_code)
        match = FEED_BODY.match(text)
        if not match:
            raise ParseError("CloudIPLC feed reply is not document.write(): %r" % text[:120])
        value = match.group("value").replace("\\'", "'").replace('\\"', '"')
        value = " ".join(html.unescape(value).split())
        if not value or value == FEED_NOT_FOUND:
            return None
        return value

    def should_notify(self, change: Change) -> bool:
        # 2026-11-06 user: only new products and hidden links alert.
        if self.config.get("notify_only_new") and change.type != ChangeType.NEW:
            return False
        # A direct-link product's price exists only while its order page is
        # open; it vanishes when sold out or unchecked, so only compare two
        # buyable snapshots.
        if (
            change.type == ChangeType.PRICE
            and change.product.metadata.get("direct_link")
            and not (change.old is not None and change.old.available and change.product.available)
        ):
            return False
        # Order-page pricing first appears once a PID's order page has been read.
        if (
            change.type == ChangeType.PRICE
            and change.fields == ["pricing"]
            and change.old is not None
            and not change.old.pricing
        ):
            return False
        return super().should_notify(change)

    def _apply_order_pages(self, products: List[Product]) -> int:
        if not self.config.get("order_page_check", True):
            return 0
        refresh = max(0, int(self.config.get("order_page_refresh_seconds", 7200)))
        retry = max(0, int(self.config.get("order_page_retry_seconds", 1800)))
        now = self._clock()
        due = [
            product.product_id
            for product in products
            if product.product_id not in self._order_cache
            or now - self._order_cache[product.product_id][0] >= refresh
        ]
        if self._order_failed_at is not None and now - self._order_failed_at < retry:
            due = []
        checked = 0
        if due:
            try:
                results = self._check_order_pages(due)
                self._order_failed_at = None
            except FetchError as exc:
                # Keep the last known state; a browser hiccup must not fake sold-outs.
                LOG.warning("%s: order-page check failed: %s", self.name, exc)
                self._order_failed_at = now
                results = {}
            for pid, order in results.items():
                previous = self._order_cache.get(pid)
                if order["status"] == "unknown":
                    LOG.warning("%s: PID %s order page unrecognized: %s", self.name, pid, order.get("url"))
                    if previous is not None:
                        order = previous[1]
                self._order_cache[pid] = (now, order)
                checked += 1
        for product in products:
            cached = self._order_cache.get(product.product_id)
            if cached is None or cached[1]["status"] == "unknown":
                continue
            order = cached[1]
            product.available = order["status"] == "available"
            product.pricing = dict(order.get("pricing") or {})
            product.metadata["order_page"] = {
                key: order[key] for key in ("status", "total", "name") if order.get(key)
            }
            product.metadata["stock_source"] = "order_page"
            if product.metadata.get("direct_link") and product.available:
                # The default first payment, including required traffic tiers.
                product.price = order.get("total")
                product.billing_cycle = next(iter(product.pricing), None)
        return checked

    def _check_order_pages(self, pids: List[str]) -> Dict[str, Dict[str, Any]]:
        from ..isolated_scan import fetch_products_isolated

        config = {
            key: value
            for key, value in self.config.items()
            if key.startswith("browser_") or key in ("base_url", "timeout_seconds")
        }
        config.update(
            {
                "name": self.name,
                "type": "cloudiplc_orders",
                "pids": list(pids),
                "request_spacing_seconds": self.config.get("request_spacing_seconds", 5),
                "scan_timeout_seconds": self.config.get(
                    "order_scan_timeout_seconds", 60 + 25 * len(pids)
                ),
                "scan_memory_high_mb": self.config.get("scan_memory_high_mb"),
                "scan_memory_max_mb": self.config.get("scan_memory_max_mb"),
            }
        )
        return {
            product.product_id: product.metadata["order_result"]
            for product in fetch_products_isolated(config)
        }

    def _fetch(self, url: str) -> str:
        self._space_requests()
        attempts = max(1, int(self.config.get("retry_attempts", 2)))
        for attempt in range(attempts):
            if attempt:
                self._sleep(max(5.0, float(self.config.get("request_spacing_seconds", 5))))
            self._last_request = self._clock()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    continue
                raise FetchError("request failed for %s (%s)" % (url, type(exc).__name__)) from None
            text = response.text
            blocked = (
                response.status_code in (403, 429)
                or is_challenge_page(text)
                or all(marker in text for marker in EDGEONE_CHALLENGE)
            )
            if blocked:
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
            if "order-standard_cart" not in text:
                raise ParseError("CloudIPLC page %s is not a store page" % url)
            return text
        raise FetchError("request failed for %s" % url)

    def _space_requests(self) -> None:
        spacing = max(0.0, float(self.config.get("request_spacing_seconds", 5)))
        if self._last_request is not None:
            wait = spacing - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)

    @staticmethod
    def _group_ids(html: str) -> set:
        # Includes links inside HTML comments: the hidden VDS group is commented out.
        return set(GROUP_LINK.findall(html))

    @staticmethod
    def _active_group(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one(".productgroups a.active[href*='gid=']")
        match = re.search(r"gid=([0-9]+)", str(node.get("href"))) if node else None
        return match.group(1) if match else None

    @staticmethod
    def _group_labels(pages: Dict[str, str]) -> Dict[str, str]:
        top: Dict[str, str] = {}
        sub: Dict[str, str] = {}
        for html in pages.values():
            soup = BeautifulSoup(html, "lxml")
            for node in soup.select(".navbar-menu a[href*='gid=']"):
                top.setdefault(re.search(r"gid=([0-9]+)", node["href"]).group(1), _text(node))
            for node in soup.select(".productgroups a[href*='gid=']"):
                sub.setdefault(re.search(r"gid=([0-9]+)", node["href"]).group(1), _text(node))
        # The menu item and the first subgroup share a gid; the subgroup is more specific.
        return {gid: sub.get(gid) or top.get(gid) for gid in set(top) | set(sub)}


class CloudiplcOrderCheck(BaseProvider):
    """Open CloudIPLC order pages in Chromium; runs inside an isolated scan.

    Returns one placeholder Product per PID whose ``metadata.order_result``
    holds ``status`` (available / sold_out / unknown), the per-cycle base
    prices and the default first-payment total.
    """

    def fetch_products(self) -> List[Product]:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        base_url = str(self.config.get("base_url", BASE_URL))
        spacing = max(3.0, float(self.config.get("request_spacing_seconds", 5)))
        blocked_types = set(
            self.config.get(
                "browser_block_resource_types", ["image", "media", "font", "stylesheet"]
            )
        )
        products: List[Product] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=bool(self.config.get("browser_headless", True)),
                args=list(self.config.get("browser_args", [])),
            )
            try:
                context = browser.new_context(locale="zh-CN", service_workers="block")
                page = context.new_page()
                page.route(
                    "**/*",
                    lambda route: (
                        route.abort()
                        if route.request.resource_type in blocked_types
                        else route.continue_()
                    ),
                )
                for index, pid in enumerate(self.config.get("pids", [])):
                    if index:
                        time.sleep(spacing)
                    url = (base_url + "cart.php?a=add&pid=%s") % pid
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                        try:
                            page.wait_for_url("**a=confproduct**", timeout=15000)
                            page.wait_for_selector("#producttotal .total", timeout=self.timeout * 1000)
                        except PlaywrightError:
                            pass
                        result = parse_order_page(page.content(), page.url)
                    except PlaywrightError as exc:
                        raise FetchError("browser failed for %s: %s" % (url, exc), blocked=True) from None
                    if result.get("blocked"):
                        raise FetchError("EdgeOne challenge did not clear for %s" % url, blocked=True)
                    products.append(
                        Product(self.name, str(pid), result.get("name") or str(pid),
                                metadata={"order_result": result})
                    )
            finally:
                browser.close()
        return products


def parse_order_page(html: str, url: str) -> Dict[str, Any]:
    """Classify a CloudIPLC ``cart.php?a=add`` result page."""
    if "order-standard_cart" not in html:
        blocked = all(marker in html for marker in EDGEONE_CHALLENGE) or is_challenge_page(html)
        return {"status": "unknown", "blocked": blocked, "url": url}
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one("#order-standard_cart")
    text = _text(main).casefold()
    if "a=confproduct" in url and soup.select_one("#producttotal"):
        pricing = {}
        for node in soup.select("[data-cycle][data-billing]"):
            pricing[str(node["data-cycle"])] = " ".join(str(node["data-billing"]).split())
        total = _text(soup.select_one("#producttotal .total"))
        total = re.sub(r"^当前总计\s*", "", total).rstrip("| ").strip()
        heading = _text(soup.select_one(".header-title h6"))
        return {
            "status": "available",
            "name": re.sub(r"^配置\s*", "", heading) or None,
            "pricing": pricing,
            "total": total or None,
            "url": url,
        }
    if any(marker in text for marker in SOLD_OUT_MARKERS):
        return {"status": "sold_out", "url": url}
    return {"status": "unknown", "url": url}
