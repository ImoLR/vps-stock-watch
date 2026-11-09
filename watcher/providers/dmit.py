from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

from ..models import Change, ChangeType, Product
from .base import BaseProvider, FetchError, ParseError, is_challenge_page


LOG = logging.getLogger(__name__)


def keyword_hit(text: str, keywords: List[str]) -> bool:
    """Any keyword in ``text`` (case-insensitive); "tyo+tiny" needs both parts."""
    lowered = (text or "").lower()
    return any(
        all(part.strip().lower() in lowered for part in str(keyword).split("+") if part.strip())
        for keyword in keywords
        if str(keyword).strip()
    )


class DmitProvider(BaseProvider):
    """DMIT stock.

    With ``watch_pids`` (2026-11-10, user): only the listed special plans are
    checked, one order page each in a single browser session; the public
    catalog (regular plans) is not loaded at all. Only a restock of a listed
    plan, or a listed PID now showing a different product, is notified.
    """

    CATALOG_URL = "https://www.dmit.io/cart.php"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # Sold-out order pages show no prices: keep the last ones read.
        self._plans: Dict[str, Dict[str, str]] = {}

    def restore_state(self, provider_state: Dict[str, Any]) -> None:
        for item in (provider_state.get("products") or {}).values():
            if isinstance(item, dict) and item.get("pricing"):
                self._plans[str(item.get("product_id"))] = dict(item["pricing"])

    def fetch_products(self) -> List[Product]:
        if self.config.get("watch_pids"):
            return self._watched_products()
        html = self._fetch_catalog_html()
        products = self.parse_catalog(html)
        return self._merge_extra_pid_probes(products)

    # -- watched special plans ----------------------------------------------
    def _watched_products(self) -> List[Product]:
        specs = [dict(spec) for spec in self.config["watch_pids"]]
        urls = ["https://www.dmit.io/cart.php?a=add&pid=%s" % spec["pid"] for spec in specs]
        pages = self._browser_pages(urls)
        products = []
        for spec, url, (final_url, page_html) in zip(specs, urls, pages):
            products.append(self._watched_product(spec, url, final_url, page_html))
        return products

    def _watched_product(self, spec: Dict[str, Any], url: str, final_url: str, page_html: str) -> Product:
        pid = str(spec["pid"])
        in_stock, page_name, plans = self.parse_order_page(page_html, final_url)
        if plans:
            self._plans[pid] = plans
        keywords = [str(value) for value in spec.get("keywords") or [spec.get("name") or ""]]
        mismatch = bool(in_stock and page_name and not keyword_hit(page_name, keywords))
        pricing = dict(self._plans.get(pid, {}))
        return Product(
            provider=self.name,
            product_id=pid,
            name=str(spec.get("name") or page_name or "DMIT PID %s" % pid),
            category="特价",
            region=spec.get("region"),
            price=pricing.get("annually"),
            billing_cycle="yearly" if pricing.get("annually") else None,
            available=in_stock and not mismatch,
            url=url,
            pricing=pricing,
            metadata={
                "discovery": {"type": "watch_pid", "hidden": False, "source": "watch_pids"},
                "stock_source": "order_page",
                "page_name": page_name,
                "pid_reused": mismatch,
            },
        )

    @staticmethod
    def parse_order_page(page_html: str, final_url: str = "") -> Tuple[bool, Optional[str], Dict[str, str]]:
        """(in stock, product name shown, {cycle: price}) of an add-to-cart page."""
        soup = BeautifulSoup(page_html, "lxml")
        title = soup.select_one(".product-title") or soup.select_one(".cart-products-title")
        name = " ".join(title.get_text(" ", strip=True).split()) if title else None
        plans: Dict[str, str] = {}
        for option in soup.select("select#inputBillingcycle option"):
            value = str(option.get("value") or "").strip().lower()
            text = " ".join(option.get_text(" ", strip=True).split())
            match = re.search(r"\$[\d,.]+\s*USD", text)
            if value and match:
                plans[value] = match.group(0)
        lowered = soup.get_text(" ", strip=True).lower()
        if "out of stock" in lowered or "orders for it have been suspended" in lowered:
            return False, name, plans
        if name and ("confproduct" in final_url or plans):
            return True, name, plans
        raise ParseError("DMIT order page %s had no recognized stock signal" % (final_url or "?"))

    def _browser_pages(self, urls: List[str]) -> List[Tuple[str, str]]:
        """Load ``urls`` one by one in one browser (one Cloudflare pass)."""
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        spacing = max(0.0, float(self.config.get("pid_spacing_seconds", 5)))
        wait_ms = self.timeout * 1000
        results: List[Tuple[str, str]] = []
        started = time.monotonic()
        try:
            with sync_playwright() as playwright:
                options: Dict[str, Any] = {
                    "headless": bool(self.config.get("browser_headless", True)),
                    "args": list(self.config.get("browser_args", [])),
                }
                if self.proxy:
                    options["proxy"] = {"server": self.proxy}
                browser = playwright.chromium.launch(**options)
                try:
                    context = browser.new_context(
                        locale=self.config.get("browser_locale", "en-US"), service_workers="block"
                    )
                    page = context.new_page()
                    blocked_types = set(
                        self.config.get("browser_block_resource_types", ["image", "media", "font"])
                    )
                    page.route(
                        "**/*",
                        lambda route: (
                            route.abort()
                            if route.request.resource_type in blocked_types
                            else route.continue_()
                        ),
                    )
                    LOG.debug("dmit: browser up in %.1f s", time.monotonic() - started)
                    for index, url in enumerate(urls):
                        if index:
                            time.sleep(spacing)
                        loaded_at = time.monotonic()
                        page.goto(url, wait_until="domcontentloaded", timeout=wait_ms)
                        # The Cloudflare check page swaps itself for the real one.
                        page.wait_for_function(
                            "() => !/just a moment/i.test(document.title)", timeout=wait_ms
                        )
                        page.wait_for_timeout(int(self.config.get("browser_settle_ms", 1500)))
                        text = page.content()
                        if is_challenge_page(text) and "product-title" not in text and "out of stock" not in text.lower():
                            raise FetchError("browser received a challenge page for %s" % url, blocked=True)
                        results.append((page.url, text))
                        LOG.debug("dmit: %s in %.1f s", url, time.monotonic() - loaded_at)
                finally:
                    browser.close()
        except PlaywrightError as exc:
            raise FetchError(
                "browser failure after %d/%d DMIT pages: %s" % (len(results), len(urls), str(exc).splitlines()[0]),
                blocked=True,
            ) from exc
        return results

    def additional_changes(self, products: List[Product], previous_state: Dict[str, Any]) -> List[Change]:
        """A watched PID that now sells something else (DMIT reuses PIDs)."""
        changes = []
        old_products = previous_state.get("products") or {}
        for product in products:
            if not product.metadata.get("pid_reused"):
                continue
            old = old_products.get(product.key) or {}
            if (old.get("metadata") or {}).get("pid_reused"):
                continue
            shown = Product.from_dict(product.to_dict())
            shown.name = str(product.metadata.get("page_name"))
            changes.append(Change(ChangeType.NAME, shown, product, ["name", "pid_reused"]))
        return changes

    def should_notify(self, change: Change) -> bool:
        if not self.config.get("watch_pids"):
            return super().should_notify(change)
        if change.type == ChangeType.RESTOCK:
            return True
        if change.type == ChangeType.NEW:
            return bool(change.product.available)
        if change.type == ChangeType.SOLD_OUT:
            return bool(self.config.get("notify_sold_out", False))
        return change.type == ChangeType.NAME and "pid_reused" in change.fields

    def _fetch_catalog_html(self) -> str:
        fetch_mode = str(self.config.get("fetch_mode", "auto")).lower()
        url = str(self.config.get("catalog_url", self.CATALOG_URL))
        retries = max(0, int(self.config.get("selector_retry_attempts", 0)))
        delay = max(
            0.0,
            float(self.config.get("selector_retry_backoff_seconds", 1)),
        )
        for attempt in range(retries + 1):
            try:
                return self.get_text(
                    url,
                    allow_browser=fetch_mode in ("auto", "browser"),
                )
            except FetchError as exc:
                retryable = "selector timeout" in str(exc).lower()
                if not retryable or attempt >= retries:
                    raise
                LOG.warning(
                    "dmit selector timeout; bounded retry %d/%d",
                    attempt + 1,
                    retries,
                )
                if delay:
                    time.sleep(delay)
        raise FetchError("DMIT catalog retry loop exhausted")

    def parse_catalog(self, html: str) -> List[Product]:
        soup = BeautifulSoup(html, "lxml")
        region_by_gid = self._attributes_by_gid(soup, ".server-region-box", "city")
        type_by_gid = self._attributes_by_gid(soup, ".server-region-box", "type")
        category_by_gid = self._attributes_by_gid(soup, ".product-category-box", "category")
        products: List[Product] = []
        for card in soup.select(".cart-products-box[pid]"):
            pid = str(card.get("pid") or "").strip()
            name = self._text(card, ".cart-products-title")
            if not pid or not name:
                continue
            wrapper = card.find_parent(class_="cart-products-item")
            gid = str(wrapper.get("gid") or "") if isinstance(wrapper, Tag) else ""
            classes = {str(value).lower() for value in card.get("class", [])}
            stock_text = self._text(card, ".cart-products-qty").lower()
            available = "none-stock" not in classes and "out of stock" not in stock_text
            price = self._price(card)
            specs = self._specs(card)
            products.append(
                Product(
                    provider=self.name,
                    product_id=pid,
                    name=name,
                    category=category_by_gid.get(gid) or type_by_gid.get(gid) or "VPS",
                    region=region_by_gid.get(gid) or self._region_from_name(name),
                    price=price,
                    billing_cycle=self._billing_cycle(card),
                    stock=None,
                    available=available,
                    url="https://www.dmit.io/cart.php?a=add&pid=%s" % pid,
                    specs=specs,
                    metadata={
                        "discovery": {
                            "type": "catalog",
                            "hidden": False,
                            "source": "catalog",
                        },
                        "gid": gid or None,
                        "stock_source": "catalog_card",
                    },
                )
            )
        if not products:
            raise ParseError("DMIT catalog contained no .cart-products-box[pid] cards")
        return products

    def _merge_extra_pid_probes(self, products: List[Product]) -> List[Product]:
        by_id = {product.product_id: product for product in products}
        for raw in self.config.get("extra_pids", []):
            spec = raw if isinstance(raw, dict) else {"pid": raw}
            pid = str(spec.get("pid", "")).strip()
            if not pid or pid in by_id:
                continue
            available = self.probe_pid(pid)
            by_id[pid] = Product(
                provider=self.name,
                product_id=pid,
                name=str(spec.get("name") or "DMIT PID %s" % pid),
                category=str(spec.get("category") or "VPS"),
                region=spec.get("region"),
                available=available,
                url="https://www.dmit.io/cart.php?a=add&pid=%s" % pid,
                metadata={
                    "discovery": {
                        "type": "extra_pid",
                        "hidden": True,
                        "source": "extra_pids",
                    },
                    "stock_source": "pid_probe",
                    "configured_extra_pid": True,
                },
            )
        return list(by_id.values())

    def probe_pid(self, pid: str) -> bool:
        url = "https://www.dmit.io/cart.php?a=add&pid=%s" % pid
        mode = str(self.config.get("fetch_mode", "auto")).lower()
        html = self.get_text(url, allow_browser=mode in ("auto", "browser"))
        lowered = BeautifulSoup(html, "lxml").get_text(" ", strip=True).lower()
        if "out of stock" in lowered or "orders for it have been suspended" in lowered:
            return False
        if "product configuration" in lowered or "order summary" in lowered:
            return True
        raise ParseError("DMIT PID %s page had no recognized stock signal" % pid)

    @staticmethod
    def _attributes_by_gid(soup: BeautifulSoup, selector: str, attribute: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for node in soup.select(selector + "[gid]"):
            gid, value = str(node.get("gid") or ""), str(node.get(attribute) or "")
            if gid and value:
                result[gid] = value
        return result

    @staticmethod
    def _text(root: Tag, selector: str) -> str:
        node = root.select_one(selector)
        return " ".join(node.get_text(" ", strip=True).split()) if node else ""

    @classmethod
    def _price(cls, card: Tag) -> Optional[str]:
        number = cls._text(card, ".price-num")
        if not number:
            return None
        prefix = cls._text(card, ".price-prefix")
        suffix = cls._text(card, ".price-suffix")
        return "%s%s%s" % (prefix, number, (" " + suffix) if suffix else "")

    @classmethod
    def _billing_cycle(cls, card: Tag) -> Optional[str]:
        value = cls._text(card, ".billing-cycle-text").lstrip("/ ").lower()
        aliases = {"monthly": "monthly", "annually": "yearly", "yearly": "yearly"}
        return aliases.get(value, value or None)

    @classmethod
    def _specs(cls, card: Tag) -> Dict[str, str]:
        labels = {
            "vcpu": "cpu",
            "ram": "ram",
            "storage": "disk",
            "ipv4 address": "ipv4",
            "ipv6 address": "ipv6",
        }
        result: Dict[str, str] = {}
        for item in card.select(".products-desc-item"):
            title = cls._text(item, ".desc-item-title").lower()
            value = cls._text(item, ".desc-item-value")
            if title in labels and value:
                result[labels[title]] = value
        quota = cls._text(card, ".highspeed-quota")
        speed = cls._text(card, ".highspeed-text").lstrip("@ ")
        low_quota = cls._text(card, ".lowspeed-quota")
        low_speed = cls._text(card, ".lowspeed-text").lstrip("@ ")
        if quota:
            result["traffic"] = quota
        bandwidth = "; ".join(
            value for value in (speed, ("%s %s" % (low_quota, low_speed)).strip()) if value
        )
        if bandwidth:
            result["bandwidth"] = bandwidth
        return result

    @staticmethod
    def _region_from_name(name: str) -> Optional[str]:
        code = name.split(".", 1)[0].upper()
        return {"LAX": "Los Angeles", "HKG": "Hong Kong", "TYO": "Tokyo"}.get(code)
