from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..models import Product
from .base import BaseProvider, FetchError, ParseError, is_challenge_page


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    url: str
    text: str


class VmsiloProvider(BaseProvider):
    """Discover VMSILO's public WHMCS catalog from server-rendered HTML."""

    BASE_URL = "https://portal.vmsilo.com/"
    STORE_URL = "https://portal.vmsilo.com/store"
    CATEGORY_COUNT = re.compile(r"standard__cart__([0-9]+)products")
    SOLD_OUT_MARKERS = (
        "sold out",
        "out of stock",
        "售罄",
        "無庫存",
        "无库存",
        "缺貨",
        "缺货",
    )

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.last_scan_stats: Dict[str, Any] = {}
        self._scan_request_count = 0

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._scan_request_count = 0
        base_url = str(self.config.get("base_url", self.BASE_URL))
        store_url = str(self.config.get("store_url", self.STORE_URL))

        homepage = self._fetch_document(base_url)
        store = self._fetch_document(store_url)
        references: Dict[str, Dict[str, Any]] = {}
        self._collect_categories(homepage, references, "homepage_navigation")
        store_navigation = self._collect_categories(
            store, references, "store_navigation", sidebar_only=True
        )
        store_slug = self._category_slug(store.url)
        if store_slug:
            self._add_reference(references, store_slug, None, "store_redirect")
            store_navigation.add(store_slug)
        for value in self.config.get("extra_category_urls", []):
            slug = self._category_slug(str(value))
            if not slug:
                raise ValueError("invalid VMSILO extra_category_urls entry: %s" % value)
            self._add_reference(references, slug, None, "configured_extra_category")

        minimum_categories = max(1, int(self.config.get("minimum_categories", 1)))
        if not store_slug or len(store_navigation) < minimum_categories:
            raise ParseError(
                "VMSILO discovery found only %d storefront categories; expected at least %d"
                % (len(store_navigation), minimum_categories)
            )

        maximum = max(1, int(self.config.get("max_categories", 100)))
        pending: Set[str] = set(references)
        requested: Set[str] = set()
        processed: Set[str] = set()
        aliases: Dict[str, str] = {}
        products_by_id: Dict[str, Product] = {}
        category_product_counts: Dict[str, int] = {}
        duplicate_products = 0

        while pending:
            if len(references) > maximum:
                raise ParseError(
                    "VMSILO discovery exceeded max_categories=%d" % maximum
                )
            requested_slug = sorted(pending)[0]
            pending.remove(requested_slug)
            if requested_slug in requested:
                continue
            requested.add(requested_slug)

            if requested_slug == store_slug:
                document = store
            else:
                document = self._fetch_document(
                    urljoin(base_url, "store/%s" % requested_slug)
                )
            final_slug = self._category_slug(document.url)
            if not final_slug:
                raise ParseError(
                    "VMSILO category %s redirected outside the store"
                    % requested_slug
                )
            if final_slug != requested_slug:
                aliases[requested_slug] = final_slug
                self._add_reference(
                    references, final_slug, None, "redirect_target"
                )

            page_navigation = self._collect_categories(
                document,
                references,
                "category_navigation",
                sidebar_only=True,
            )
            missing = store_navigation - page_navigation
            if missing:
                raise ParseError(
                    "VMSILO category %s has incomplete navigation: missing %s"
                    % (final_slug, ", ".join(sorted(missing)))
                )
            pending.update(set(references) - requested)
            if final_slug in processed:
                continue

            label = self._category_label(document.text, final_slug)
            hidden = final_slug not in store_navigation
            source = (
                "configured_extra_category"
                if "configured_extra_category"
                in references.get(requested_slug, {}).get("sources", set())
                else (
                    "homepage_navigation"
                    if hidden
                    else "store_navigation"
                )
            )
            products = self.parse_category(
                document.text,
                document.url,
                final_slug,
                label,
                hidden=hidden,
                discovery_source=source,
            )
            category_product_counts[final_slug] = len(products)
            for product in products:
                current = products_by_id.get(product.product_id)
                if current is None:
                    products_by_id[product.product_id] = product
                else:
                    duplicate_products += 1
                    self._merge_duplicate(current, product)
            processed.add(final_slug)

        if not processed:
            raise ParseError("VMSILO discovery produced no complete category pages")

        products = [products_by_id[key] for key in sorted(products_by_id)]
        duration = time.monotonic() - started
        self.last_scan_stats = {
            "system": "WHMCS ShufyTheme",
            "data_source": "server_rendered_html",
            "public_api": False,
            "categories": len(processed),
            "category_slugs": sorted(processed),
            "category_product_counts": dict(sorted(category_product_counts.items())),
            "discovered_category_routes": len(references),
            "aliases": dict(sorted(aliases.items())),
            "products": len(products),
            "available": sum(item.available is True for item in products),
            "unavailable": sum(item.available is False for item in products),
            "unknown_availability": sum(item.available is None for item in products),
            "numeric_stock": sum(item.stock is not None for item in products),
            "boolean_stock": sum(item.stock is None for item in products),
            "hidden_public_products": sum(
                bool(item.metadata.get("discovery", {}).get("hidden"))
                for item in products
            ),
            "duplicate_products": duplicate_products,
            "requests": self._scan_request_count,
            "duration_seconds": round(duration, 3),
            "browser_required": False,
        }
        LOG.info(
            "VMSILO complete catalog categories=%d products=%d available=%d "
            "numeric_stock=%d hidden=%d duplicates=%d requests=%d duration=%.3fs",
            self.last_scan_stats["categories"],
            self.last_scan_stats["products"],
            self.last_scan_stats["available"],
            self.last_scan_stats["numeric_stock"],
            self.last_scan_stats["hidden_public_products"],
            duplicate_products,
            self._scan_request_count,
            duration,
        )
        return products

    def parse_category(
        self,
        html: str,
        category_url: str,
        category_slug: str,
        category_label: str,
        hidden: bool = False,
        discovery_source: str = "store_navigation",
    ) -> List[Product]:
        soup = BeautifulSoup(html, "lxml")
        if is_challenge_page(html):
            raise ParseError(
                "VMSILO category %s returned a challenge page" % category_slug
            )
        count_node = soup.select_one("[class*='standard__cart__'][class*='products']")
        expected_count = self._expected_product_count(count_node)
        if expected_count is None:
            raise ParseError(
                "VMSILO category %s has no recognized catalog count marker"
                % category_slug
            )
        cards = soup.select(".pricing__plans__standard__item")
        if len(cards) != expected_count:
            raise ParseError(
                "VMSILO category %s expected %d products but parsed %d"
                % (category_slug, expected_count, len(cards))
            )
        products: List[Product] = []
        seen = set()
        for card in cards:
            product = self._parse_product(
                card,
                category_url,
                category_slug,
                category_label,
                hidden,
                discovery_source,
            )
            if product.product_id in seen:
                raise ParseError(
                    "VMSILO category %s contains duplicate product slug %s"
                    % (category_slug, product.product_id)
                )
            seen.add(product.product_id)
            products.append(product)
        return products

    def _parse_product(
        self,
        card: Tag,
        category_url: str,
        category_slug: str,
        category_label: str,
        hidden: bool,
        discovery_source: str,
    ) -> Product:
        name = self._text(card.select_one(".pricing-plans-special-header h5"))
        if not name:
            raise ParseError("VMSILO category %s has a product without a name" % category_slug)
        button = card.select_one("a.btn-order-now[href]")
        sold_out = any(
            marker in " ".join(card.get_text(" ", strip=True).split()).casefold()
            for marker in self.SOLD_OUT_MARKERS
        )
        if not isinstance(button, Tag) and not sold_out:
            raise ParseError("VMSILO product %s has no order or sold-out marker" % name)

        href = str(button.get("href") or "") if isinstance(button, Tag) else ""
        product_url = urljoin(category_url, href) if href else ""
        product_slug = self._product_slug(product_url, category_slug)
        if not product_slug:
            slug_node = card.select_one("[data-product-slug]")
            product_slug = (
                str(slug_node.get("data-product-slug") or "").strip()
                if isinstance(slug_node, Tag)
                else ""
            )
        if not product_slug:
            raise ParseError("VMSILO product %s has no stable public product slug" % name)

        price_value = card.select_one(".pricing [data-row-price-min]")
        raw_amount = str(price_value.get("data-row-price-min") or "").strip() if isinstance(price_value, Tag) else ""
        if not raw_amount:
            raise ParseError("VMSILO product %s has no price" % product_slug)
        price_text = self._text(card.select_one(".pricing")) or ""
        symbol_match = re.search(r"(?:US\$|HK\$|CA\$|AU\$|€|£|¥|\$)", price_text)
        price = "%s%s" % (symbol_match.group(0) if symbol_match else "", raw_amount)
        billing_cycle = self._billing_cycle(price_text)
        if not billing_cycle:
            raise ParseError("VMSILO product %s has no billing cycle" % product_slug)

        stock_node = card.select_one(".stock, .inventory, [data-stock]")
        stock_text = self._text(stock_node) or ""
        numeric = re.search(
            r"(?<![0-9])([0-9]+)\s*(?:available|可用|庫存|库存)",
            stock_text,
            re.I,
        )
        if numeric:
            stock: Optional[int] = int(numeric.group(1))
            available = stock > 0
            stock_source = "category_card.numeric"
        else:
            stock = None
            available = False if sold_out else True
            stock_source = (
                "category_card.sold_out"
                if sold_out
                else "category_card.order_button"
            )
        if available and isinstance(button, Tag) and self._disabled(button):
            raise ParseError(
                "VMSILO product %s is available but order button is disabled"
                % product_slug
            )

        description_node = card.select_one(".pricing__plans__special__body")
        lines = self._description_lines(description_node)
        return Product(
            provider=self.name,
            product_id=product_slug,
            name=name,
            category=category_label,
            price=price,
            billing_cycle=billing_cycle,
            stock=stock,
            available=available,
            url=product_url or category_url,
            specs=self._specs(lines),
            metadata={
                "discovery": {
                    "type": "extra_category" if hidden else "catalog",
                    "hidden": hidden,
                    "source": discovery_source,
                },
                "category_slug": category_slug,
                "categories": [category_slug],
                "product_slug": product_slug,
                "description": "\n".join(lines),
                "stock_source": stock_source,
                "stable_id_source": "public_product_slug",
            },
        )

    def _fetch_document(self, url: str) -> FetchedDocument:
        attempts = max(1, int(self.config.get("retry_attempts", 2)))
        backoff = max(0.0, float(self.config.get("retry_backoff_seconds", 1.0)))
        for attempt in range(attempts):
            self._scan_request_count += 1
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                raise FetchError(
                    "request failed for %s (%s)" % (url, type(exc).__name__)
                ) from None
            text = response.text
            blocked = response.status_code in (403, 429) or is_challenge_page(text)
            retryable = blocked or response.status_code >= 500
            if retryable and attempt + 1 < attempts:
                time.sleep(backoff * (2 ** attempt))
                continue
            if response.status_code >= 400 or blocked:
                raise FetchError(
                    "HTTP %s for %s%s"
                    % (
                        response.status_code,
                        url,
                        " (challenge/block page)" if blocked else "",
                    ),
                    response.status_code,
                    blocked,
                )
            if not text.strip():
                raise FetchError("empty response from %s" % url, response.status_code)
            return FetchedDocument(url, response.url, text)
        raise FetchError("request failed for %s" % url)

    def _collect_categories(
        self,
        document: FetchedDocument,
        references: Dict[str, Dict[str, Any]],
        source: str,
        sidebar_only: bool = False,
    ) -> Set[str]:
        soup = BeautifulSoup(document.text, "lxml")
        selector = (
            'a[id^="Secondary_Sidebar-Categories-"][href]'
            if sidebar_only
            else 'a[id^="Secondary_Sidebar-Categories-"][href], a[href*="/store/"]'
        )
        discovered = set()
        for node in soup.select(selector):
            slug = self._category_slug(
                urljoin(document.url, str(node.get("href") or ""))
            )
            if not slug:
                continue
            discovered.add(slug)
            self._add_reference(
                references, slug, self._text(node), source
            )
        return discovered

    @staticmethod
    def _add_reference(
        references: Dict[str, Dict[str, Any]],
        slug: str,
        label: Optional[str],
        source: str,
    ) -> None:
        entry = references.setdefault(slug, {"labels": [], "sources": set()})
        entry["sources"].add(source)
        if label and label not in entry["labels"]:
            entry["labels"].append(label)

    def _category_slug(self, url: str) -> Optional[str]:
        base_url = str(self.config.get("base_url", self.BASE_URL))
        parsed = urlparse(urljoin(base_url, url))
        if parsed.netloc.casefold() != urlparse(base_url).netloc.casefold():
            return None
        route = unquote(parse_qs(parsed.query).get("rp", [""])[0]).rstrip("/")
        target = route if route.startswith("/store/") else unquote(parsed.path).rstrip("/")
        match = re.fullmatch(r"/store/([^/]+)", target)
        return match.group(1) if match else None

    @staticmethod
    def _product_slug(url: str, category_slug: str) -> Optional[str]:
        if not url:
            return None
        path = unquote(urlparse(url).path).rstrip("/")
        match = re.fullmatch(r"/store/%s/([^/]+)" % re.escape(category_slug), path)
        return match.group(1) if match else None

    def _category_label(self, html: str, slug: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        for node in soup.select('a[id^="Secondary_Sidebar-Categories-"].active[href]'):
            if self._category_slug(str(node.get("href") or "")) == slug:
                return self._text(node) or slug
        return slug

    @classmethod
    def _expected_product_count(cls, node: Optional[Tag]) -> Optional[int]:
        if node is None:
            return None
        for value in node.get("class", []):
            match = cls.CATEGORY_COUNT.fullmatch(str(value))
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _disabled(node: Tag) -> bool:
        return bool(
            node.has_attr("disabled")
            or str(node.get("aria-disabled") or "").casefold() == "true"
            or "disabled" in node.get("class", [])
        )

    @staticmethod
    def _billing_cycle(value: str) -> Optional[str]:
        lowered = value.casefold()
        aliases = (
            (("semi-annually", "semiannually", "半年"), "semiannually"),
            (("triennially", "三年"), "triennially"),
            (("biennially", "兩年", "两年"), "biennially"),
            (("quarterly", "季繳", "季付", "按季"), "quarterly"),
            (("annually", "yearly", "年繳", "年付", "按年"), "yearly"),
            (("monthly", "月繳", "月付", "按月"), "monthly"),
        )
        for terms, canonical in aliases:
            if any(term in lowered for term in terms):
                return canonical
        return None

    @classmethod
    def _description_lines(cls, node: Optional[Tag]) -> List[str]:
        if node is None:
            return []
        result = []
        for raw in node.get_text("\n", strip=True).splitlines():
            value = " ".join(raw.split())
            if value and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _specs(lines: List[str]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        labels = {
            "cpu cores": "cpu",
            "amount of ram": "ram",
            "disk space": "disk",
            "bandwidth": "traffic",
            "network rate": "bandwidth",
            "ipv4": "ipv4",
            "ipv6": "ipv6",
        }
        for line in lines:
            if "：" in line:
                label, value = line.split("：", 1)
            elif ":" in line:
                label, value = line.split(":", 1)
            else:
                continue
            key = labels.get(label.strip().casefold())
            if key and value.strip():
                result[key] = value.strip()
        return result

    @staticmethod
    def _merge_duplicate(current: Product, duplicate: Product) -> None:
        category_slugs = list(current.metadata.get("categories", []))
        for value in duplicate.metadata.get("categories", []):
            if value not in category_slugs:
                category_slugs.append(value)
        fields = (
            "name",
            "price",
            "billing_cycle",
            "stock",
            "available",
            "specs",
        )
        conflicts = [
            field
            for field in fields
            if getattr(current, field) != getattr(duplicate, field)
        ]
        if conflicts:
            raise ParseError(
                "VMSILO product %s conflicts across categories: %s"
                % (current.product_id, ", ".join(conflicts))
            )
        if (
            current.metadata.get("discovery", {}).get("hidden")
            and not duplicate.metadata.get("discovery", {}).get("hidden")
        ):
            current.category = duplicate.category
            current.url = duplicate.url
            current.metadata = duplicate.metadata
        current.metadata["categories"] = category_slugs

    @staticmethod
    def _text(node: Optional[Tag]) -> Optional[str]:
        if node is None:
            return None
        value = " ".join(node.get_text(" ", strip=True).split())
        return value or None
