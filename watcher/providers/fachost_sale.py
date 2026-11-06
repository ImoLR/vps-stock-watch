from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..models import Product
from .base import FetchError, ParseError, is_challenge_page
from .fachost import FachostProvider


LOG = logging.getLogger(__name__)


class FachostSaleProvider(FachostProvider):
    """Watch FACHOST's hidden sale categories (404 until a sale opens).

    Each configured category is one pseudo-product that is "available" while
    the category page opens, so 404 -> 200 alerts like a restock. While open,
    every product card becomes a product, and in-stock ones also get their
    checkout billing plans (monthly / annual ...) as ``pricing``.
    """

    DEFAULT_CATEGORIES = ("special-regulations",)

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # Last plans read per product: sold-out checkouts redirect away, so
        # keep the known plans instead of reporting them as removed.
        self._plans: Dict[str, Dict[str, str]] = {}

    def restore_state(self, provider_state: Dict[str, Any]) -> None:
        for key, item in (provider_state.get("products") or {}).items():
            if isinstance(item, dict) and item.get("pricing"):
                self._plans[str(item.get("product_id"))] = dict(item["pricing"])

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._scan_request_count = 0
        base_url = str(self.config.get("base_url", self.BASE_URL))
        spacing = max(0.0, float(self.config.get("request_spacing_seconds", 5)))
        slugs = [str(value) for value in self.config.get("categories") or self.DEFAULT_CATEGORIES]
        products: List[Product] = []
        first = True
        for slug in slugs:
            if not first:
                time.sleep(spacing)
            first = False
            url = urljoin(base_url, "products/%s" % slug)
            status, final_url, text = self._get(url)
            if status == 404:
                products.append(self._category_product(slug, url, False))
                continue
            if status != 200:
                raise FetchError("HTTP %s for %s" % (status, url), status)
            if self._category_slug(final_url) != slug:
                # Redirected elsewhere (e.g. home page): treat as still closed.
                products.append(self._category_product(slug, url, False))
                continue
            cards = self._parse_open_category(text, final_url, slug)
            products.append(
                self._category_product(slug, url, True, len(cards), self._heading(text))
            )
            for card in cards:
                if card.available and card.metadata.get("checkout_url"):
                    time.sleep(spacing)
                    plans = self._checkout_plans(card.metadata["checkout_url"])
                    if plans:
                        self._plans[card.product_id] = plans
                card.pricing = dict(self._plans.get(card.product_id, {}))
                products.append(card)
        self.last_scan_stats = {
            "categories": len(slugs),
            "open_categories": sum(
                1
                for item in products
                if item.metadata.get("catalog_event") == "sale_category" and item.available
            ),
            "products": sum(
                1 for item in products if item.metadata.get("catalog_event") != "sale_category"
            ),
            "requests": self._scan_request_count,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return products

    def _get(self, url: str):
        self._scan_request_count += 1
        try:
            response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise FetchError("request failed for %s (%s)" % (url, type(exc).__name__)) from None
        text = response.text
        # Never retry a block page: back off through the normal failure path.
        if response.status_code in (403, 429) or is_challenge_page(text):
            raise FetchError(
                "HTTP %s for %s (challenge/block page)" % (response.status_code, url),
                response.status_code,
                True,
            )
        return response.status_code, response.url, text

    def _category_product(
        self,
        slug: str,
        url: str,
        is_open: bool,
        product_count: int = 0,
        heading: Optional[str] = None,
    ) -> Product:
        return Product(
            provider=self.name,
            product_id="category:%s" % slug,
            name="活动分类 %s" % (heading or slug),
            category="活动分类",
            available=is_open,
            url=url,
            metadata={
                "catalog_event": "sale_category",
                "category_slug": slug,
                "product_count": product_count if is_open else None,
            },
        )

    def _parse_open_category(self, page_html: str, url: str, slug: str) -> List[Product]:
        """Parse an open sale category leniently; a hidden category may not
        list itself in the navigation, which the strict catalog parser needs."""
        try:
            return self.parse_category(page_html, url, slug)
        except ParseError as exc:
            LOG.info("FACHOST sale category %s: strict parse failed (%s), using cards", slug, exc)
        soup = BeautifulSoup(page_html, "lxml")
        component = soup.find(attrs={"wire:name": "products", "wire:snapshot": True})
        ids: List[str] = []
        category_id = ""
        if isinstance(component, Tag):
            try:
                data = self._snapshot(component, slug).get("data") or {}
                ids = self._model_keys(data.get("products"), "products", slug)
                category_id = self._model_key(data.get("category"), slug)
            except ParseError:
                ids = []
        root = component if isinstance(component, Tag) else soup
        cards = root.select("article.fh-product-card")
        heading = self._heading(page_html) or slug
        currency = self._currency(soup)
        products = []
        for index, card in enumerate(cards):
            details = card.select_one("a.fh-details-link[href]")
            fallback = (
                urlparse(urljoin(url, str(details.get("href")))).path.rstrip("/").rsplit("/", 1)[-1]
                if details
                else "card-%d" % index
            )
            product_id = ids[index] if len(ids) == len(cards) else fallback
            try:
                products.append(
                    self._parse_product(
                        card, product_id, url, slug, category_id, heading, None, None, currency
                    )
                )
            except ParseError as exc:
                LOG.warning("FACHOST sale category %s: card %s unreadable (%s)", slug, product_id, exc)
        return products

    @staticmethod
    def _heading(page_html: str) -> Optional[str]:
        soup = BeautifulSoup(page_html, "lxml")
        node = soup.select_one(".fh-shop-header h1") or soup.select_one("h1")
        return " ".join(node.get_text(" ", strip=True).split()) if node else None

    def _checkout_plans(self, checkout_url: str) -> Dict[str, str]:
        """Return {cycle label: price text} from the checkout plan selector."""
        try:
            status, final_url, text = self._get(checkout_url)
        except FetchError as exc:
            if exc.blocked:
                raise
            return {}
        if status != 200 or "/checkout" not in urlparse(final_url).path:
            return {}
        return self.parse_plans(text)

    @staticmethod
    def parse_plans(page_html: str) -> Dict[str, str]:
        soup = BeautifulSoup(page_html, "lxml")
        plans: Dict[str, str] = {}
        for option in soup.select("select#plan_id option"):
            text = " ".join(option.get_text(" ", strip=True).split())
            if not text:
                continue
            label, _, price = text.partition(" - ")
            plans[label.strip() or text] = price.strip() or text
        return plans


__all__ = ["FachostSaleProvider"]
