from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
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
    PRODUCT_ARTICLE_ID = re.compile(r"product([0-9]+)")
    PRICE = re.compile(
        r"(US\$|HK\$|CA\$|AU\$|€|£|¥|\$)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
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
        # Sold-out cards may drop the order link; remember pid -> slug so the
        # stable slug ID survives until the product is buyable again.
        self._slug_by_pid: Dict[str, str] = {}

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._scan_request_count = 0
        base_url = str(self.config.get("base_url", self.BASE_URL))
        store_url = str(self.config.get("store_url", self.STORE_URL))

        store = self._fetch_document(store_url)
        navigation = self._group_tabs(store)
        minimum_categories = max(1, int(self.config.get("minimum_categories", 1)))
        if len(navigation) < minimum_categories:
            raise ParseError(
                "VMSILO discovery found only %d storefront categories; expected at least %d"
                % (len(navigation), minimum_categories)
            )
        routes: Dict[str, Optional[str]] = {ref: None for ref in navigation}
        for value in self.config.get("extra_category_urls", []):
            ref = self._category_ref(urljoin(base_url, str(value)))
            if not ref:
                raise ValueError("invalid VMSILO extra_category_urls entry: %s" % value)
            routes.setdefault(ref, None)

        maximum = max(1, int(self.config.get("max_categories", 100)))
        if len(routes) > maximum:
            raise ParseError("VMSILO discovery exceeded max_categories=%d" % maximum)

        products_by_id: Dict[str, Product] = {}
        category_product_counts: Dict[str, int] = {}
        duplicate_products = 0
        processed = set()
        documents: List[Tuple[str, FetchedDocument]] = []
        store_active = self._active_tab(store)
        if store_active:
            documents.append((store_active, store))
        for ref in sorted(routes):
            if ref == store_active:
                continue
            documents.append(
                (ref, self._fetch_document(self._category_url(base_url, ref)))
            )

        for ref, document in documents:
            page_navigation = self._group_tabs(document)
            missing = set(navigation) - set(page_navigation)
            if missing:
                raise ParseError(
                    "VMSILO category %s has incomplete navigation: missing %s"
                    % (ref, ", ".join(sorted(missing)))
                )
            hidden = ref not in navigation
            active = self._active_tab(document)
            if not hidden and active != ref:
                raise ParseError(
                    "VMSILO category %s rendered %s instead" % (ref, active)
                )
            slug = self._category_slug(document.url) or ref.replace("=", "-")
            routes[ref] = slug
            if slug in processed:
                continue
            label = page_navigation.get(ref) or self._products_heading(document.text) or slug
            products = self.parse_category(
                document.text,
                document.url,
                slug,
                label,
                hidden=hidden,
                discovery_source=(
                    "configured_extra_category" if hidden else "store_navigation"
                ),
            )
            category_product_counts[slug] = len(products)
            for product in products:
                current = products_by_id.get(product.product_id)
                if current is None:
                    products_by_id[product.product_id] = product
                else:
                    duplicate_products += 1
                    self._merge_duplicate(current, product)
            processed.add(slug)

        if not processed:
            raise ParseError("VMSILO discovery produced no complete category pages")

        products = [products_by_id[key] for key in sorted(products_by_id)]
        duration = time.monotonic() - started
        self.last_scan_stats = {
            "system": "WHMCS bestvm theme",
            "data_source": "server_rendered_html",
            "public_api": False,
            "categories": len(processed),
            "category_slugs": sorted(processed),
            "category_product_counts": dict(sorted(category_product_counts.items())),
            "category_routes": dict(sorted(routes.items())),
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
        LOG.debug(
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
        if (
            soup.select_one("#order-standard_cart .bv-group-tabs") is None
            or soup.select_one("#order-standard_cart .bv-products-head") is None
        ):
            raise ParseError(
                "VMSILO category %s has no recognized catalog layout" % category_slug
            )
        cards = soup.select("article.bv-plan")
        grid_cards = soup.select(".bv-plan-grid > article.bv-plan")
        if len(cards) != len(grid_cards):
            raise ParseError(
                "VMSILO category %s has %d plan cards outside the plan grid"
                % (category_slug, len(cards) - len(grid_cards))
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
        name = self._text(card.select_one("header h3"))
        if not name:
            raise ParseError("VMSILO category %s has a product without a name" % category_slug)
        pid_match = self.PRODUCT_ARTICLE_ID.fullmatch(str(card.get("id") or ""))
        pid = pid_match.group(1) if pid_match else None
        card_text = " ".join(card.get_text(" ", strip=True).split())
        sold_out = any(
            marker in card_text.casefold() for marker in self.SOLD_OUT_MARKERS
        )
        button = None
        for node in card.select("a[href]"):
            if self._product_slug(urljoin(category_url, str(node.get("href"))), category_slug):
                button = node
                break
        if not isinstance(button, Tag) and not sold_out:
            raise ParseError("VMSILO product %s has no order or sold-out marker" % name)

        href = str(button.get("href") or "") if isinstance(button, Tag) else ""
        product_url = urljoin(category_url, href) if href else ""
        product_slug = self._product_slug(product_url, category_slug)
        if product_slug and pid:
            self._slug_by_pid[pid] = product_slug
        elif pid:
            product_slug = self._slug_by_pid.get(pid) or "pid-%s" % pid
            LOG.warning(
                "VMSILO product %s has no order link; using id %s", name, product_slug
            )
        if not product_slug:
            raise ParseError("VMSILO product %s has no stable public product slug" % name)

        price_node = card.select_one(".bv-plan-price .price")
        price_match = self.PRICE.search(self._text(price_node) or "")
        if not price_match:
            raise ParseError("VMSILO product %s has no price" % product_slug)
        price = "%s%s" % (price_match.group(1) or "", price_match.group(2))
        billing_cycle = self._billing_cycle(self._text(card.select_one(".bv-plan-price")) or "")
        if not billing_cycle:
            raise ParseError("VMSILO product %s has no billing cycle" % product_slug)

        numeric = re.search(
            r"(?<![0-9])([0-9]+)\s*(?:available|可用|庫存|库存)",
            card_text,
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

        lines = self._description_lines(card.select_one(".bv-plan-desc"))
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
                "whmcs_pid": pid,
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

    def _group_tabs(self, document: FetchedDocument) -> Dict[str, str]:
        soup = BeautifulSoup(document.text, "lxml")
        tabs: Dict[str, str] = {}
        for node in soup.select(".bv-group-tabs a[href]"):
            ref = self._category_ref(urljoin(document.url, str(node.get("href"))))
            if ref:
                tabs[ref] = self._text(node) or ref
        return tabs

    def _active_tab(self, document: FetchedDocument) -> Optional[str]:
        soup = BeautifulSoup(document.text, "lxml")
        node = soup.select_one('.bv-group-tabs a[aria-current="page"][href]')
        if node is None:
            return None
        return self._category_ref(urljoin(document.url, str(node.get("href"))))

    @classmethod
    def _products_heading(cls, html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")
        return cls._text(soup.select_one(".bv-products-head h2"))

    def _category_ref(self, url: str) -> Optional[str]:
        """Return ``gid=<N>`` for cart group links or ``store/<slug>`` for routes."""
        base_url = str(self.config.get("base_url", self.BASE_URL))
        parsed = urlparse(url)
        if parsed.netloc.casefold() != urlparse(base_url).netloc.casefold():
            return None
        gid = parse_qs(parsed.query).get("gid", [""])[0]
        if parsed.path.rstrip("/").endswith("/cart.php") and gid.isdigit():
            return "gid=%s" % gid
        slug = self._category_slug(url)
        return "store/%s" % slug if slug else None

    @staticmethod
    def _category_url(base_url: str, ref: str) -> str:
        if ref.startswith("gid="):
            return urljoin(base_url, "cart.php?%s" % ref)
        return urljoin(base_url, ref)

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
            (("one-time", "one time", "onetime", "一次性"), "onetime"),
            (("semi-annually", "semiannually", "半年"), "semiannually"),
            (("triennially", "三年"), "triennially"),
            (("biennially", "兩年", "两年"), "biennially"),
            (("quarterly", "/ quarter", "季繳", "季付", "按季"), "quarterly"),
            (("annually", "yearly", "/ year", "年繳", "年付", "按年"), "yearly"),
            (("monthly", "/ month", "/ mo", "月繳", "月付", "按月"), "monthly"),
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
