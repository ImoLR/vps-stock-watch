from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

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


class LeikwanhostProvider(BaseProvider):
    """Discover and parse LeiKwanHost's public WHMCS storefront."""

    BASE_URL = "https://buy.leikwanhost.com/"
    STORE_URL = "https://buy.leikwanhost.com/cart.php"
    PRODUCT_ID = re.compile(r"product([0-9]+)")
    EMPTY_GROUP_MARKERS = (
        "product group does not contain any visible products",
        "產品群組不包含任何可見的產品",
        "产品组中不包含任何可见的产品",
    )
    ERROR_MARKERS = (
        "oops, there's a problem",
        "糟糕，發生問題了",
        "頁面未找到",
        "page not found",
    )
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
            store, references, "store_navigation"
        )
        store_slug = self._category_slug(store.url)
        if store_slug:
            self._add_reference(
                references, store_slug, None, "store_redirect", priority=110
            )
            store_navigation.add(store_slug)
        if not references or not store_slug:
            raise ParseError(
                "LeiKwanHost discovery found no complete public store navigation"
            )

        minimum_categories = max(1, int(self.config.get("minimum_categories", 1)))
        if len(store_navigation) < minimum_categories:
            raise ParseError(
                "LeiKwanHost discovery found only %d categories; expected at least %d"
                % (len(store_navigation), minimum_categories)
            )

        maximum = max(1, int(self.config.get("max_categories", 100)))
        pending: Set[str] = set(references)
        requested: Set[str] = set()
        processed: Set[str] = set()
        aliases: Dict[str, str] = {}
        products_by_id: Dict[str, Product] = {}
        duplicate_pids = 0

        while pending:
            if len(references) > maximum:
                raise ParseError(
                    "LeiKwanHost discovery exceeded max_categories=%s" % maximum
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
                    urljoin(
                        base_url,
                        "index.php?rp=/store/%s"
                        % quote(requested_slug, safe="-._~"),
                    )
                )
            final_slug = self._category_slug(document.url)
            if not final_slug:
                raise ParseError(
                    "LeiKwanHost category %s redirected outside the store"
                    % requested_slug
                )
            if final_slug != requested_slug:
                aliases[requested_slug] = final_slug
                self._add_reference(
                    references, final_slug, None, "redirect_target", priority=110
                )

            page_navigation = self._collect_categories(
                document, references, "category_navigation"
            )
            missing = store_navigation - page_navigation
            if missing:
                raise ParseError(
                    "LeiKwanHost category %s has incomplete navigation: missing %s"
                    % (final_slug, ", ".join(sorted(missing)))
                )
            pending.update(set(references) - requested)
            if final_slug in processed:
                continue

            label = self._category_label(
                document.text,
                final_slug,
                references.get(final_slug, {}).get("labels", []),
            )
            group_id = self._category_group_id(document.text, final_slug)
            discovery_source = self._discovery_source(
                references.get(final_slug, {}).get("sources", set())
            )
            products = self.parse_category(
                document.text,
                document.url,
                final_slug,
                label,
                group_id,
                discovery_source,
            )
            for product in products:
                current = products_by_id.get(product.product_id)
                if current is None:
                    products_by_id[product.product_id] = product
                else:
                    duplicate_pids += 1
                    self._merge_duplicate(current, product)
            processed.add(final_slug)

        if not processed:
            raise ParseError("LeiKwanHost discovery produced no complete category pages")
        minimum_products = max(1, int(self.config.get("minimum_products", 1)))
        if len(products_by_id) < minimum_products:
            raise ParseError(
                "LeiKwanHost catalog found only %d products; expected at least %d"
                % (len(products_by_id), minimum_products)
            )

        products = [products_by_id[key] for key in sorted(products_by_id, key=int)]
        duration = time.monotonic() - started
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
            "boolean_stock": sum(item.stock is None for item in products),
            "normal_products": len(products),
            "hidden_public_products": 0,
            "duplicate_pids": duplicate_pids,
            "requests": self._scan_request_count,
            "duration_seconds": round(duration, 3),
        }
        LOG.info(
            "LeiKwanHost complete catalog categories=%d products=%d available=%d "
            "numeric_stock=%d duplicates=%d requests=%d duration=%.3fs",
            self.last_scan_stats["categories"],
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
        discovery_source: str = "store_navigation",
    ) -> List[Product]:
        soup = BeautifulSoup(html, "lxml")
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        lowered = page_text.casefold()
        if any(marker.casefold() in lowered for marker in self.ERROR_MARKERS):
            raise ParseError(
                "LeiKwanHost category %s returned an error page" % category_slug
            )

        cards = soup.select('div.lk-cart-card[id^="product"]')
        if not cards:
            explicitly_empty = any(
                marker.casefold() in lowered for marker in self.EMPTY_GROUP_MARKERS
            )
            if explicitly_empty:
                return []
            raise ParseError(
                "LeiKwanHost category %s contained no recognized product cards"
                % category_slug
            )

        heading = self._node_text(soup.select_one("#order-standard_cart h1"))
        products: List[Product] = []
        seen: Set[str] = set()
        for card in cards:
            match = self.PRODUCT_ID.fullmatch(str(card.get("id") or ""))
            if not match:
                raise ParseError(
                    "LeiKwanHost category %s has a product card without a numeric PID"
                    % category_slug
                )
            pid = match.group(1)
            if pid in seen:
                raise ParseError(
                    "LeiKwanHost category %s contains duplicate PID %s"
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
                    discovery_source,
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
        discovery_source: str,
    ) -> Product:
        name = self._node_text(card.select_one("#product%s-name" % pid))
        if not name:
            raise ParseError("LeiKwanHost PID %s has no product name" % pid)

        button = card.select_one("#product%s-order-button[href]" % pid)
        if not isinstance(button, Tag):
            raise ParseError("LeiKwanHost PID %s has no order button" % pid)
        disabled = self._disabled(button) or "is-disabled" in card.get("class", [])

        stock: Optional[int] = None
        stock_node = card.select_one(".lk-cart-stock")
        if stock_node:
            stock_text = self._node_text(stock_node) or ""
            numeric = re.search(
                r"(?<![0-9])([0-9]+)\s*(?:available|可用)", stock_text, re.I
            )
            if numeric:
                stock = int(numeric.group(1))
                available = stock > 0
                stock_source = "category_card.numeric_available"
            elif any(marker in stock_text.casefold() for marker in self.SOLD_OUT_MARKERS):
                available = False
                stock_source = "category_card.sold_out"
            else:
                raise ParseError(
                    "LeiKwanHost PID %s has unrecognized stock marker %r"
                    % (pid, stock_text)
                )
        else:
            available = not disabled
            stock_source = "category_card.order_button"
        if available and disabled:
            raise ParseError(
                "LeiKwanHost PID %s reports stock but has a disabled order button" % pid
            )

        price_node = card.select_one(".lk-cart-price")
        base_price = self._node_text(
            price_node.select_one(".price") if isinstance(price_node, Tag) else None
        )
        if not base_price:
            raise ParseError("LeiKwanHost PID %s has no price" % pid)
        small_texts = [
            self._node_text(node) or "" for node in price_node.select("small")
        ]
        setup_fee = self._setup_fee(small_texts)
        price = base_price + (" + %s" % setup_fee if setup_fee else "")
        billing_cycle = self._billing_cycle(" ".join(small_texts))

        description = card.select_one("#product%s-description" % pid)
        lines = self._description_lines(description)
        specs = self._specs(lines)
        region = self._region(name, lines, category_label)
        card_url = urljoin(category_url, str(button.get("href")))
        product_url = urljoin(
            str(self.config.get("base_url", self.BASE_URL)),
            "cart.php?a=add&pid=%s" % pid,
        )
        currency_match = re.search(r"([A-Z]{3})(?![A-Z])", base_price)
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
                    "source": discovery_source,
                },
                "currency": currency_match.group(1) if currency_match else None,
                "category_slug": category_slug,
                "category_group_id": category_group_id,
                "category_heading": category_heading,
                "categories": [category_record],
                "card_url": card_url,
                "description": "\n".join(lines),
                "setup_fee": setup_fee,
                "stock_source": stock_source,
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
    ) -> Set[str]:
        soup = BeautifulSoup(document.text, "lxml")
        discovered: Set[str] = set()
        selectors = (
            ('a[id^="Secondary_Sidebar-Categories-"][href]', "href", 100, "sidebar"),
            ("select option[value]", "value", 90, "category_select"),
            ("header a[href], nav a[href], footer a[href]", "href", 50, "page_navigation"),
        )
        for selector, attribute, priority, kind in selectors:
            for node in soup.select(selector):
                slug = self._category_slug(
                    urljoin(document.url, str(node.get(attribute) or ""))
                )
                if not slug:
                    continue
                discovered.add(slug)
                label = self._node_text(node)
                self._add_reference(
                    references,
                    slug,
                    label,
                    "%s:%s" % (source, kind),
                    priority,
                )
        return discovered

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
        if label and label.casefold() not in ("store", "more", "browse all"):
            value = (priority, label)
            if value not in entry["labels"]:
                entry["labels"].append(value)

    def _category_slug(self, url: str) -> Optional[str]:
        absolute = urljoin(str(self.config.get("base_url", self.BASE_URL)), url)
        parsed = urlparse(absolute)
        base_host = urlparse(str(self.config.get("base_url", self.BASE_URL))).netloc
        if parsed.netloc.casefold() != base_host.casefold():
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
            'a[id^="Secondary_Sidebar-Categories-"].active[href], '
            "select option[selected][value]"
        ):
            raw = node.get("href") if node.name == "a" else node.get("value")
            if self._category_slug(str(raw or "")) == slug:
                label = self._node_text(node)
                if label:
                    return label
        labels = sorted(
            discovered_labels,
            key=lambda item: (-item[0], -len(item[1]), item[1]),
        )
        return labels[0][1] if labels else slug

    @staticmethod
    def _discovery_source(sources: Iterable[str]) -> str:
        values = set(sources)
        if any(value.startswith("store_navigation") for value in values):
            return "store_navigation"
        if any(value.startswith("homepage_navigation") for value in values):
            return "homepage_navigation"
        return "category_navigation"

    @staticmethod
    def _category_group_id(html: str, slug: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")
        group_ids = {
            value
            for form in soup.select("form[action*='gid=']")
            for value in parse_qs(urlparse(str(form.get("action") or "")).query).get(
                "gid", []
            )
            if value
        }
        if len(group_ids) > 1:
            raise ParseError(
                "LeiKwanHost category %s exposes multiple group IDs" % slug
            )
        return next(iter(group_ids), None)

    @staticmethod
    def _disabled(node: Tag) -> bool:
        return bool(
            node.has_attr("disabled")
            or str(node.get("aria-disabled") or "").casefold() == "true"
            or "disabled" in node.get("class", [])
        )

    @staticmethod
    def _setup_fee(values: Iterable[str]) -> Optional[str]:
        for value in values:
            for part in re.split(r"[·|]", value):
                normalized = " ".join(part.split())
                if "setup fee" in normalized.casefold():
                    return normalized
        return None

    @staticmethod
    def _billing_cycle(value: str) -> Optional[str]:
        lowered = value.casefold()
        aliases = (
            (("semi-annually", "semiannually", "半年"), "semiannually"),
            (("triennially", "三年"), "triennially"),
            (("biennially", "兩年", "两年"), "biennially"),
            (("quarterly", "季繳", "季付"), "quarterly"),
            (("annually", "yearly", "年繳", "年付"), "yearly"),
            (("monthly", "月繳", "月付"), "monthly"),
        )
        for terms, canonical in aliases:
            if any(term in lowered for term in terms):
                return canonical
        return None

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

    @staticmethod
    def _specs(lines: List[str]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        joined = " | ".join(lines)
        patterns = {
            "cpu": r"(?i)([0-9]+(?:\.[0-9]+)?\s*\*?\s*(?:vCPU|cores?|核))",
            "ram": r"(?i)([0-9]+(?:\.[0-9]+)?\s*(?:MiB|GiB|MB|GB|TB|G|T)\s*(?:RAM|memory|內存|内存)?)",
            "disk": r"(?i)([0-9]+(?:\.[0-9]+)?\s*(?:MB|GB|TB|G|T)\s*(?:NVME|NVMe|SSD|HDD|disk|硬盤|硬盘))",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, joined)
            if match:
                result[key] = " ".join(match.group(1).split())
        for line in lines:
            lowered = line.casefold()
            if re.search(r"(?:mib|gib|tib|mb|gb|tb)\s*@", line, re.I):
                result.setdefault("traffic", line)
            bandwidth = re.search(r"[0-9]+(?:\.[0-9]+)?\s*(?:Mbps|Gbps)", line, re.I)
            if bandwidth:
                result.setdefault("bandwidth", bandwidth.group(0))
            if "ipv4" in lowered:
                result.setdefault("ipv4", line)
            if "ipv6" in lowered:
                result.setdefault("ipv6", line)
        return result

    @staticmethod
    def _region(name: str, lines: List[str], category_label: str) -> Optional[str]:
        code_match = re.search(r"\b(JP|HK|DE|KR|LA|SE|TW|RU)\b", name, re.I)
        if code_match:
            return {
                "JP": "日本",
                "HK": "香港",
                "DE": "德国",
                "KR": "韩国",
                "LA": "美国洛杉矶",
                "SE": "美国西雅图",
                "TW": "台湾",
                "RU": "俄罗斯",
            }[code_match.group(1).upper()]
        haystack = " | ".join([name, category_label] + lines[:4]).casefold()
        regions = (
            (("廣州", "广州", "canton"), "广州"),
            (("上海", "shanghai", "滬", "沪"), "上海"),
            (("香港", "hong kong"), "香港"),
            (("台灣", "台湾", "taiwan"), "台湾"),
            (("日本", "東京", "东京", "japan", "tokyo"), "日本"),
            (("韓國", "韩国", "korea", "seoul"), "韩国"),
            (("德國", "德国", "germany"), "德国"),
            (("美國", "美国", "los angeles", "seattle"), "美国"),
            (("俄羅斯", "俄罗斯", "moscow"), "俄罗斯"),
            (("中國大陸", "中国大陆", "mainland"), "中国大陆"),
        )
        for terms, label in regions:
            if any(term.casefold() in haystack for term in terms):
                return label
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
                "LeiKwanHost PID %s conflicts across categories: %s"
                % (current.product_id, ", ".join(conflicts))
            )
        categories = current.metadata.setdefault("categories", [])
        for value in duplicate.metadata.get("categories", []):
            if value not in categories:
                categories.append(value)

    @staticmethod
    def _node_text(node: Optional[Tag]) -> Optional[str]:
        if node is None:
            return None
        value = " ".join(node.get_text(" ", strip=True).split())
        return value or None
