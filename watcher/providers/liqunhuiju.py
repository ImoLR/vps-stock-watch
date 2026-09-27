from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..models import Product
from .base import BaseProvider, FetchError, ParseError, is_challenge_page


LOG = logging.getLogger(__name__)


class LiqunHuijuProvider(BaseProvider):
    """Monitor the independent public LeiKwan Bridge site."""

    BASE_URL = "https://v3.leikwanhost.com/"
    CATALOG_PATH = re.compile(r"^/(?:store|products?|plans?|pricing|shop)(?:/|$)", re.I)
    PRODUCT_CARD = "[data-product-id], [data-sku]"
    EMPTY_MARKERS = (
        "data-catalog-empty",
        "目前沒有公開商品",
        "当前没有公开商品",
        "no public products",
        "no products available",
    )
    SOLD_OUT_MARKERS = (
        "sold out",
        "out of stock",
        "售罄",
        "無庫存",
        "无库存",
    )

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.last_scan_stats: Dict[str, Any] = {}
        self._scan_request_count = 0

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._scan_request_count = 0
        base_url = str(self.config.get("base_url", self.BASE_URL))
        homepage = self._fetch(base_url)
        soup = BeautifulSoup(homepage, "lxml")
        self._validate_homepage(soup, homepage, base_url)

        catalog_urls = self._catalog_urls(soup, base_url)
        products_by_id: Dict[str, Product] = {}
        catalog_pages = 0
        empty_mode = "landing_only"
        for catalog_url in catalog_urls:
            document = self._fetch(catalog_url)
            products = self.parse_catalog(document, catalog_url)
            catalog_pages += 1
            empty_mode = "explicit_catalog"
            for product in products:
                if product.product_id in products_by_id:
                    raise ParseError(
                        "Liqun Huiju duplicate product ID %s" % product.product_id
                    )
                products_by_id[product.product_id] = product

        products = [products_by_id[key] for key in sorted(products_by_id)]
        duration = time.monotonic() - started
        self.last_scan_stats = {
            "catalog_pages": catalog_pages,
            "catalog_urls": catalog_urls,
            "empty_mode": empty_mode,
            "products": len(products),
            "available": sum(item.available is True for item in products),
            "unavailable": sum(item.available is False for item in products),
            "numeric_stock": sum(item.stock is not None for item in products),
            "hidden_public_products": 0,
            "requests": self._scan_request_count,
            "duration_seconds": round(duration, 3),
        }
        LOG.info(
            "Liqun Huiju complete public catalog pages=%d products=%d "
            "empty_mode=%s requests=%d duration=%.3fs",
            catalog_pages,
            len(products),
            empty_mode,
            self._scan_request_count,
            duration,
        )
        return products

    def validate_snapshot(
        self, products: List[Product], previous_state: Dict[str, Any]
    ) -> None:
        previous_products = previous_state.get("products", {})
        if (
            not products
            and isinstance(previous_products, dict)
            and bool(previous_products)
            and self.last_scan_stats.get("empty_mode") == "landing_only"
        ):
            raise ParseError(
                "Liqun Huiju catalog disappeared from an otherwise valid landing page"
            )

    def parse_catalog(self, html: str, url: str) -> List[Product]:
        soup = BeautifulSoup(html, "lxml")
        if is_challenge_page(html):
            raise ParseError("Liqun Huiju catalog returned a challenge page")
        cards = soup.select(self.PRODUCT_CARD)
        if not cards:
            lowered = " ".join(soup.get_text(" ", strip=True).split()).casefold()
            explicit = soup.select_one("[data-catalog-empty]") is not None or any(
                marker.casefold() in lowered for marker in self.EMPTY_MARKERS[1:]
            )
            if explicit:
                return []
            raise ParseError("Liqun Huiju catalog contained no recognized product schema")

        products: List[Product] = []
        seen = set()
        for card in cards:
            product = self._parse_product(card, url)
            if product.product_id in seen:
                raise ParseError(
                    "Liqun Huiju catalog contains duplicate product ID %s"
                    % product.product_id
                )
            seen.add(product.product_id)
            products.append(product)
        return products

    def _parse_product(self, card: Tag, page_url: str) -> Product:
        product_id = str(
            card.get("data-product-id") or card.get("data-sku") or ""
        ).strip()
        if not product_id:
            raise ParseError("Liqun Huiju product has no stable ID or SKU")
        name = self._text(
            card.select_one("[data-product-name], .product-name, h2, h3, h4")
        )
        if not name:
            raise ParseError("Liqun Huiju product %s has no name" % product_id)
        price = str(card.get("data-price") or "").strip() or self._text(
            card.select_one(".price, [data-product-price]")
        )
        if not price:
            raise ParseError("Liqun Huiju product %s has no price" % product_id)
        cycle = str(card.get("data-billing-cycle") or "").strip() or self._text(
            card.select_one(".billing-cycle, [data-cycle]")
        )
        stock, available, stock_source = self._stock(card)
        order = card.select_one(
            "a.order-button[href], a[href*='/checkout'], a[href*='/buy'], "
            "a[href*='/order']"
        )
        order_href = str(
            card.get("data-order-url")
            or card.get("data-product-url")
            or (order.get("href") if isinstance(order, Tag) else "")
            or ""
        ).strip()
        if not order_href:
            raise ParseError("Liqun Huiju product %s has no direct order URL" % product_id)
        order_url = urljoin(page_url, order_href)
        category = str(card.get("data-category") or "").strip() or self._text(
            card.select_one(".category, [data-category-name]")
        ) or "利群汇聚"
        description = self._text(card.select_one(".description, [data-description]"))
        return Product(
            provider=self.name,
            product_id=product_id,
            name=name,
            category=category,
            price=price,
            billing_cycle=cycle,
            stock=stock,
            available=available,
            url=order_url,
            metadata={
                "discovery": {
                    "type": "catalog",
                    "hidden": False,
                    "source": "public_catalog",
                },
                "description": description or "",
                "stock_source": stock_source,
            },
        )

    def _stock(self, card: Tag) -> tuple:
        raw_stock = str(card.get("data-stock") or "").strip()
        stock_node = card.select_one(".stock, .inventory, [data-stock-text]")
        stock_text = raw_stock or (self._text(stock_node) or "")
        numeric = re.search(
            r"(?<![0-9])([0-9]+)\s*(?:available|可用|庫存|库存)?",
            stock_text,
            re.I,
        )
        if numeric:
            stock = int(numeric.group(1))
            return stock, stock > 0, "catalog.numeric"
        lowered = " ".join(card.get_text(" ", strip=True).split()).casefold()
        if any(marker in lowered for marker in self.SOLD_OUT_MARKERS):
            return None, False, "catalog.sold_out"
        raw_available = str(card.get("data-available") or "").strip().casefold()
        if raw_available in ("true", "1", "yes", "available"):
            return None, True, "catalog.boolean"
        if raw_available in ("false", "0", "no", "sold_out"):
            return None, False, "catalog.boolean"
        order = card.select_one(
            "a.order-button[href], a[href*='/checkout'], a[href*='/buy'], "
            "a[href*='/order']"
        )
        if isinstance(order, Tag) and "disabled" not in order.get("class", []):
            return None, True, "catalog.order_button"
        raise ParseError("Liqun Huiju product has no recognized availability signal")

    def _fetch(self, url: str) -> str:
        self._scan_request_count += 1
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FetchError(
                "request failed for %s (%s)" % (url, type(exc).__name__)
            ) from None
        text = response.text
        blocked = response.status_code in (403, 429) or is_challenge_page(text)
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
        return text

    @staticmethod
    def _validate_homepage(soup: BeautifulSoup, html: str, base_url: str) -> None:
        if is_challenge_page(html):
            raise ParseError("Liqun Huiju homepage returned a challenge page")
        body = soup.select_one("body.pg-public.bridge-home-page")
        shell = soup.select_one(".public-shell")
        title = LiqunHuijuProvider._text(soup.title) or ""
        links = {
            urlparse(urljoin(base_url, str(node.get("href") or ""))).path
            for node in soup.select("a[href]")
        }
        if (
            body is None
            or shell is None
            or "leikwan bridge" not in title.casefold()
            or not {"/login", "/knowledgebase"}.issubset(links)
        ):
            raise ParseError("Liqun Huiju homepage schema is incomplete")

    @classmethod
    def _catalog_urls(cls, soup: BeautifulSoup, base_url: str) -> List[str]:
        base_host = urlparse(base_url).netloc.casefold()
        result = []
        for node in soup.select("a[href]"):
            absolute = urljoin(base_url, str(node.get("href") or ""))
            parsed = urlparse(absolute)
            if (
                parsed.netloc.casefold() != base_host
                or not cls.CATALOG_PATH.match(parsed.path)
            ):
                continue
            normalized = absolute.split("#", 1)[0]
            if normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _text(node: Optional[Tag]) -> Optional[str]:
        if node is None:
            return None
        value = " ".join(node.get_text(" ", strip=True).split())
        return value or None
