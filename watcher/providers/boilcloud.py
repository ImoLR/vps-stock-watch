from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from xml.etree import ElementTree

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


class BoilcloudProvider(BaseProvider):
    """Discover and parse BOILCLOUD's complete public WHMCS storefront."""

    BASE_URL = "https://cloud.boil.network/"
    STORE_URL = "https://cloud.boil.network/index.php?rp=/store"
    SITEMAP_URL = "https://cloud.boil.network/sitemap.xml"
    PRODUCT_ID = re.compile(r"product([0-9]+)")
    EMPTY_GROUP_MARKERS = (
        "product group does not contain any visible products",
        "產品群組不包含任何可見的產品",
        "产品组中不包含任何可见的产品",
    )
    ERROR_MARKERS = (
        "糟糕，發生問題了",
        "oops, there's a problem",
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
        sitemap_url = str(self.config.get("sitemap_url", self.SITEMAP_URL))

        homepage = self._fetch_document(base_url)
        store = self._fetch_document(store_url)
        sitemap = self._fetch_document(sitemap_url)

        references: Dict[str, Dict[str, Any]] = {}
        self._collect_html_categories(homepage, references, "homepage_navigation")
        self._collect_html_categories(store, references, "store_navigation")
        self._collect_sitemap_categories(sitemap, references)
        store_slug = self._category_slug(store.url)
        if store_slug:
            self._add_reference(references, store_slug, None, "store_redirect")
        if not references:
            raise ParseError("BOILCLOUD discovery found no public store categories")

        maximum = max(1, int(self.config.get("max_categories", 100)))
        pending: Set[str] = set(references)
        processed: Set[str] = set()
        requested: Set[str] = set()
        aliases: Dict[str, str] = {}
        products_by_id: Dict[str, Product] = {}
        category_product_counts: Dict[str, int] = {}
        duplicate_pids = 0

        while pending:
            if len(references) > maximum:
                raise ParseError(
                    "BOILCLOUD discovery exceeded max_categories=%s" % maximum
                )
            requested_slug = sorted(pending)[0]
            pending.remove(requested_slug)
            if requested_slug in requested:
                continue
            requested.add(requested_slug)

            if store_slug == requested_slug:
                document = store
            else:
                document = self._fetch_document(
                    urljoin(base_url, "store/%s" % quote(requested_slug, safe="-._~"))
                )
            final_slug = self._category_slug(document.url)
            if not final_slug:
                raise ParseError(
                    "BOILCLOUD category %s redirected outside the store" % requested_slug
                )
            if final_slug != requested_slug:
                aliases[requested_slug] = final_slug
                self._add_reference(references, final_slug, None, "redirect_target")
            self._collect_html_categories(document, references, "category_navigation")
            pending.update(set(references) - requested)
            if final_slug in processed:
                continue

            label = self._category_label(
                document.text,
                final_slug,
                references.get(final_slug, {}).get("labels", []),
            )
            group_id = self._category_group_id(document.text, final_slug)
            products = self.parse_category(
                document.text,
                document.url,
                final_slug,
                label,
                group_id,
            )
            category_product_counts[final_slug] = len(products)
            for product in products:
                current = products_by_id.get(product.product_id)
                if current is None:
                    products_by_id[product.product_id] = product
                else:
                    duplicate_pids += 1
                    self._merge_duplicate(current, product)
            processed.add(final_slug)

        if not processed:
            raise ParseError("BOILCLOUD discovery produced no complete category pages")
        if not products_by_id:
            raise ParseError("BOILCLOUD complete catalog contained no products")

        products = [products_by_id[key] for key in sorted(products_by_id, key=int)]
        duration = time.monotonic() - started
        suspicious = [
            item.product_id
            for item in products
            if re.search(r"(?:test|beta|internal|temporary|temp|測試|测试)", item.name, re.I)
        ]
        navigation_only_categories = sorted(
            slug
            for slug in processed
            if "sitemap" not in references.get(slug, {}).get("sources", set())
            and not any(
                source.endswith(":sidebar")
                for source in references.get(slug, {}).get("sources", set())
            )
        )
        self.last_scan_stats = {
            "categories": len(processed),
            "category_slugs": sorted(processed),
            "discovered_category_routes": len(references),
            "aliases": dict(sorted(aliases.items())),
            "products": len(products),
            "available": sum(item.available is True for item in products),
            "unavailable": sum(item.available is False for item in products),
            "unknown_availability": sum(item.available is None for item in products),
            "numeric_stock": sum(item.stock is not None for item in products),
            "duplicate_pids": duplicate_pids,
            "suspicious_pids": suspicious,
            "navigation_only_category_slugs": navigation_only_categories,
            "navigation_only_products": sum(
                category_product_counts.get(slug, 0)
                for slug in navigation_only_categories
            ),
            "requests": self._scan_request_count,
            "duration_seconds": round(duration, 3),
        }
        LOG.info(
            "BOILCLOUD complete catalog categories=%d routes=%d products=%d "
            "available=%d numeric_stock=%d duplicate_pids=%d requests=%d duration=%.3fs",
            self.last_scan_stats["categories"],
            self.last_scan_stats["discovered_category_routes"],
            self.last_scan_stats["products"],
            self.last_scan_stats["available"],
            self.last_scan_stats["numeric_stock"],
            duplicate_pids,
            self._scan_request_count,
            duration,
        )
        return products

    def parse_category(
        self,
        html: str,
        category_url: str,
        category_slug: str,
        category_label: Optional[str] = None,
        category_group_id: Optional[str] = None,
    ) -> List[Product]:
        soup = BeautifulSoup(html, "lxml")
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        lowered = page_text.casefold()
        if any(marker.casefold() in lowered for marker in self.ERROR_MARKERS):
            raise ParseError("BOILCLOUD category %s returned an error page" % category_slug)

        cards = soup.select('div.tt-single-product[id^="product"]')
        if not cards:
            explicitly_empty = any(
                marker.casefold() in lowered for marker in self.EMPTY_GROUP_MARKERS
            )
            if explicitly_empty:
                return []
            raise ParseError(
                "BOILCLOUD category %s contained no recognized product cards" % category_slug
            )

        heading = self._text(soup, "#order-standard_cart h2.font-size-22")
        products: List[Product] = []
        seen: Set[str] = set()
        for card in cards:
            card_id = str(card.get("id") or "")
            match = self.PRODUCT_ID.fullmatch(card_id)
            if not match:
                raise ParseError(
                    "BOILCLOUD category %s has a product card without a numeric PID"
                    % category_slug
                )
            pid = match.group(1)
            if pid in seen:
                raise ParseError(
                    "BOILCLOUD category %s contains duplicate PID %s"
                    % (category_slug, pid)
                )
            seen.add(pid)
            products.append(
                self._parse_product(
                    card,
                    category_url,
                    category_slug,
                    category_label or category_slug,
                    category_group_id,
                    heading,
                    pid,
                )
            )
        return products

    def _parse_product(
        self,
        card: Tag,
        category_url: str,
        category_slug: str,
        category_label: str,
        category_group_id: Optional[str],
        category_heading: Optional[str],
        pid: str,
    ) -> Product:
        name = self._text(card, "#product%s-name" % pid) or self._text(
            card, ".tt-product-name"
        )
        if not name:
            raise ParseError("BOILCLOUD PID %s has no product name" % pid)

        price = self._text(card, ".product-pricing .price") or None
        cycle_node = card.select_one(".product-pricing [data-key]")
        billing_cycle = None
        if cycle_node:
            billing_cycle = self._billing_cycle(
                str(cycle_node.get("data-key") or ""),
                " ".join(cycle_node.get_text(" ", strip=True).split()),
            )

        qty = card.select_one(".qty")
        stock: Optional[int] = None
        available: Optional[bool]
        stock_source: Optional[str] = None
        if qty:
            stock_text = " ".join(qty.get_text(" ", strip=True).split())
            stock_match = re.search(r"(?<![0-9])([0-9]+)\s*(?:可用|available)", stock_text, re.I)
            if not stock_match:
                raise ParseError("BOILCLOUD PID %s has an unrecognized stock value" % pid)
            stock = int(stock_match.group(1))
            available = stock > 0
            stock_source = "category_card.qty"
        else:
            card_text = " ".join(card.get_text(" ", strip=True).split()).casefold()
            if any(marker in card_text for marker in ("out of stock", "售罄", "無庫存", "缺货")):
                available = False
                stock_source = "category_card.soldout_text"
            else:
                order_button = card.select_one("a.btn-order-now")
                disabled = bool(
                    order_button
                    and (
                        order_button.has_attr("disabled")
                        or str(order_button.get("aria-disabled") or "").lower() == "true"
                        or "disabled" in order_button.get("class", [])
                    )
                )
                available = True if order_button and not disabled else None
                stock_source = "category_card.order_button" if order_button else None

        description = card.select_one("#product%s-description" % pid)
        lines = self._description_lines(description)
        specs = self._specs(lines)
        region = self._region(description, lines, category_label)
        card_link = card.select_one("a.btn-order-now[href]")
        card_url = urljoin(category_url, str(card_link.get("href"))) if card_link else None
        product_url = urljoin(
            str(self.config.get("base_url", self.BASE_URL)),
            "cart.php?a=add&pid=%s" % pid,
        )
        currency_match = re.search(r"\b([A-Z]{3})\b", price or "")
        category_record = {
            "label": category_label,
            "slug": category_slug,
            "group_id": category_group_id,
        }
        return Product(
            provider=self.name,
            product_id=pid,
            name=name,
            category=category_label,
            region=region,
            price=price,
            billing_cycle=billing_cycle,
            stock=stock,
            available=available,
            url=product_url,
            specs=specs,
            metadata={
                "discovery": {
                    "type": "catalog",
                    "hidden": False,
                    "source": "store_discovery",
                },
                "currency": currency_match.group(1) if currency_match else None,
                "category_slug": category_slug,
                "category_group_id": category_group_id,
                "category_heading": category_heading,
                "categories": [category_record],
                "card_url": card_url,
                "description": "\n".join(lines),
                "stock_source": stock_source,
            },
        )

    def _fetch_document(self, url: str) -> FetchedDocument:
        attempts = max(1, int(self.config.get("retry_attempts", 2)))
        backoff = max(0.0, float(self.config.get("retry_backoff_seconds", 1.0)))
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            self._scan_request_count += 1
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
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
        raise FetchError("request failed for %s: %s" % (url, last_error))

    def _collect_html_categories(
        self,
        document: FetchedDocument,
        references: Dict[str, Dict[str, Any]],
        source: str,
    ) -> None:
        soup = BeautifulSoup(document.text, "lxml")
        selectors = (
            ('.cart-sidebar [menuitemname="Categories"] a[href]', 100, "sidebar"),
            ("select option[value]", 90, "category_select"),
            ("header a[href], nav a[href], footer a[href]", 50, "page_navigation"),
        )
        for selector, priority, kind in selectors:
            for node in soup.select(selector):
                raw = node.get("href") if node.name == "a" else node.get("value")
                slug = self._category_slug(urljoin(document.url, str(raw or "")))
                if not slug:
                    continue
                label = " ".join(node.get_text(" ", strip=True).split()) or None
                self._add_reference(
                    references, slug, label, "%s:%s" % (source, kind), priority
                )

    def _collect_sitemap_categories(
        self,
        document: FetchedDocument,
        references: Dict[str, Dict[str, Any]],
    ) -> None:
        try:
            root = ElementTree.fromstring(document.text)
        except ElementTree.ParseError as exc:
            raise ParseError("BOILCLOUD sitemap is invalid XML: %s" % exc) from exc
        for node in root.findall(".//{*}loc"):
            slug = self._category_slug((node.text or "").strip())
            if slug:
                self._add_reference(references, slug, None, "sitemap", 10)

    @staticmethod
    def _add_reference(
        references: Dict[str, Dict[str, Any]],
        slug: str,
        label: Optional[str],
        source: str,
        priority: int = 0,
    ) -> None:
        entry = references.setdefault(slug, {"labels": [], "sources": set()})
        entry["sources"].add(source)
        if label and label not in ("商店", "更多", "Hong Kong"):
            value = (priority, label)
            if value not in entry["labels"]:
                entry["labels"].append(value)

    def _category_slug(self, url: str) -> Optional[str]:
        absolute = urljoin(str(self.config.get("base_url", self.BASE_URL)), url)
        parsed = urlparse(absolute)
        base_host = urlparse(str(self.config.get("base_url", self.BASE_URL))).netloc
        if parsed.netloc.lower() != base_host.lower():
            return None
        route = unquote(parse_qs(parsed.query).get("rp", [""])[0]).rstrip("/")
        target = route if route.startswith("/store/") else unquote(parsed.path).rstrip("/")
        match = re.fullmatch(r"/store/([^/]+)", target)
        return match.group(1) if match else None

    def _category_label(
        self,
        html: str,
        slug: str,
        discovered_labels: Iterable[Tuple[int, str]],
    ) -> str:
        soup = BeautifulSoup(html, "lxml")
        for node in soup.select(
            '.cart-sidebar [menuitemname="Categories"] a.active[href], select option[selected][value]'
        ):
            raw = node.get("href") if node.name == "a" else node.get("value")
            if self._category_slug(str(raw or "")) == slug:
                label = " ".join(node.get_text(" ", strip=True).split())
                if label and "查看其他分類" not in label:
                    return label
        labels = sorted(discovered_labels, key=lambda item: (-item[0], -len(item[1]), item[1]))
        return labels[0][1] if labels else slug

    def _category_group_id(self, html: str, slug: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")
        for node in soup.select("[data-gid], [gid], a[href*='gid=']"):
            raw_url = str(node.get("href") or "")
            node_slug = self._category_slug(raw_url) if raw_url else slug
            if node_slug != slug:
                continue
            value = node.get("data-gid") or node.get("gid")
            if value is None and raw_url:
                value = parse_qs(urlparse(raw_url).query).get("gid", [None])[0]
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _merge_duplicate(current: Product, duplicate: Product) -> None:
        fields = (
            "name",
            "region",
            "price",
            "billing_cycle",
            "stock",
            "available",
            "url",
            "specs",
        )
        conflicts = [
            field for field in fields if getattr(current, field) != getattr(duplicate, field)
        ]
        if conflicts:
            raise ParseError(
                "BOILCLOUD PID %s conflicts across categories: %s"
                % (current.product_id, ", ".join(conflicts))
            )
        categories = current.metadata.setdefault("categories", [])
        for value in duplicate.metadata.get("categories", []):
            if value not in categories:
                categories.append(value)

    @staticmethod
    def _billing_cycle(key: str, text: str) -> Optional[str]:
        lowered = "%s %s" % (key.casefold(), text.casefold())
        aliases = (
            (("monthly", "月繳", "月付"), "monthly"),
            (("quarterly", "季繳", "季付"), "quarterly"),
            (("semiannually", "半年"), "semiannually"),
            (("annually", "yearly", "年繳", "年付"), "yearly"),
            (("biennially", "兩年", "两年"), "biennially"),
            (("triennially", "三年"), "triennially"),
        )
        for values, canonical in aliases:
            if any(value in lowered for value in values):
                return canonical
        return text or key or None

    @classmethod
    def _description_lines(cls, node: Optional[Tag]) -> List[str]:
        if node is None:
            return []
        result: List[str] = []
        for raw in node.get_text("\n", strip=True).splitlines():
            value = " ".join(raw.split())
            if value and value not in result:
                result.append(value)
        return result

    @classmethod
    def _specs(cls, lines: List[str]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        joined = " | ".join(lines)
        patterns = {
            "cpu": r"(?i)([0-9]+(?:\.[0-9]+)?\s*(?:核心\s*vCPU|核心|核|vCPU|cores?))",
            "ram": r"(?i)([0-9]+(?:\.[0-9]+)?\s*(?:MB|GB|TB|G|T)\s*(?:內存|内存|記憶體|RAM|memory))",
            "disk": r"(?i)([0-9]+(?:\.[0-9]+)?\s*(?:MB|GB|TB|G|T)\s*(?:SSD|HDD|NVMe|存儲|存储|儲存|disk))",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, joined)
            if match:
                result[key] = match.group(1)
        for line in lines:
            lowered = line.casefold()
            if "traffic" in lowered or "流量" in line or "unmetered" in lowered:
                result.setdefault("traffic", line)
            if (
                re.search(r"(?:mbps|gbps)", line, re.I)
                and any(
                    marker in lowered
                    for marker in ("頻寬", "带宽", "bandwidth", "shared", "dedicated", "上載", "下載")
                )
            ):
                result.setdefault("bandwidth", line)
            if "ipv4" in lowered:
                result.setdefault("ipv4", line)
            if "ipv6" in lowered:
                result.setdefault("ipv6", line)
        return result

    @classmethod
    def _region(
        cls,
        description: Optional[Tag],
        lines: List[str],
        category_label: str,
    ) -> Optional[str]:
        terms = (
            "香港",
            "台灣",
            "台湾",
            "日本",
            "加拿大",
            "美國",
            "美国",
            "澳門",
            "澳门",
            "越南",
            "新加坡",
            "Hong Kong",
            "Taiwan",
            "Japan",
            "Canada",
            "United States",
            "Singapore",
            "USA",
        )
        if description is not None:
            marker = description.select_one(".fa-map-marker-alt")
            if marker:
                following = marker.find_next(["strong", "span"])
                value = cls._node_text(following)
                if value and any(term.casefold() in value.casefold() for term in terms):
                    return value
        for line in lines[:4]:
            if any(term.casefold() in line.casefold() for term in terms):
                return line
        for term in terms:
            if term.casefold() in category_label.casefold():
                return term
        return None

    @staticmethod
    def _text(root: Any, selector: str) -> str:
        node = root.select_one(selector)
        return BoilcloudProvider._node_text(node) or ""

    @staticmethod
    def _node_text(node: Optional[Tag]) -> Optional[str]:
        if node is None:
            return None
        value = " ".join(node.get_text(" ", strip=True).split())
        return value or None
