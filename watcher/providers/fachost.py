from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

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


class FachostProvider(BaseProvider):
    """Discover FACHOST's public Paymenter catalog from server-rendered HTML."""

    BASE_URL = "https://fachost.cloud/"
    CATEGORY_PATH = re.compile(r"^/products/([^/]+)/?$")
    AVAILABLE_STOCK = re.compile(r"(?<![0-9])([0-9]+)\s+available\b", re.I)
    SOLD_OUT_MARKERS = ("sold out", "out of stock")

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.last_scan_stats: Dict[str, Any] = {}
        self._scan_request_count = 0

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._scan_request_count = 0
        base_url = str(self.config.get("base_url", self.BASE_URL))

        homepage = self._fetch_document(base_url)
        references: Dict[str, Dict[str, Any]] = {}
        self._collect_categories(homepage, references, "homepage_navigation")
        if not references:
            raise ParseError("FACHOST homepage exposed no public product categories")

        maximum = max(1, int(self.config.get("max_categories", 100)))
        pending: Set[str] = set(references)
        processed: Set[str] = set()
        products_by_id: Dict[str, Product] = {}
        category_product_counts: Dict[str, int] = {}
        duplicate_product_ids = 0
        hidden_public_products = 0

        while pending:
            if len(references) > maximum:
                raise ParseError(
                    "FACHOST discovery exceeded max_categories=%s" % maximum
                )
            slug = sorted(pending)[0]
            pending.remove(slug)
            if slug in processed:
                continue

            document = self._fetch_document(
                urljoin(base_url, "products/%s" % slug)
            )
            final_slug = self._category_slug(document.url)
            if final_slug != slug:
                raise ParseError(
                    "FACHOST category %s redirected to unexpected route %s"
                    % (slug, document.url)
                )

            self._collect_categories(document, references, "category_navigation")
            products = self.parse_category(document.text, document.url, slug, references)
            category_product_counts[slug] = len(products)
            for product in products:
                existing = products_by_id.get(product.product_id)
                if existing is None:
                    products_by_id[product.product_id] = product
                else:
                    duplicate_product_ids += 1
                    self._merge_duplicate(existing, product)

            processed.add(slug)
            pending.update(set(references) - processed)

        extra_urls = self._extra_checkout_urls(base_url)
        for checkout_url in extra_urls:
            try:
                document = self._fetch_document(checkout_url)
            except FetchError as exc:
                if exc.status == 404:
                    continue
                raise
            product = self.parse_checkout(document.text, document.url, references)
            existing = products_by_id.get(product.product_id)
            if existing is None:
                products_by_id[product.product_id] = product
                hidden_public_products += 1
            else:
                duplicate_product_ids += 1
                self._merge_duplicate(existing, product)

        if not processed:
            raise ParseError("FACHOST discovery produced no complete category pages")
        if not products_by_id:
            raise ParseError("FACHOST complete public catalog contained no products")

        products = [
            products_by_id[key]
            for key in sorted(
                products_by_id,
                key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
            )
        ]
        duration = time.monotonic() - started
        suspicious = [
            item.product_id
            for item in products
            if re.search(
                r"(?:test|beta|internal|temporary|temp|測試|测试)",
                "%s %s" % (item.name, item.metadata.get("description", "")),
                re.I,
            )
        ]
        self.last_scan_stats = {
            "categories": len(processed),
            "category_slugs": sorted(processed),
            "category_product_counts": dict(sorted(category_product_counts.items())),
            "products": len(products),
            "available": sum(item.available is True for item in products),
            "unavailable": sum(item.available is False for item in products),
            "unknown_availability": sum(item.available is None for item in products),
            "numeric_stock": sum(item.stock is not None for item in products),
            "duplicate_product_ids": duplicate_product_ids,
            "hidden_public_products": hidden_public_products,
            "extra_checkout_urls": len(extra_urls),
            "suspicious_product_ids": suspicious,
            "requests": self._scan_request_count,
            "duration_seconds": round(duration, 3),
        }
        LOG.info(
            "FACHOST complete catalog categories=%d products=%d available=%d "
            "numeric_stock=%d duplicates=%d requests=%d duration=%.3fs",
            self.last_scan_stats["categories"],
            self.last_scan_stats["products"],
            self.last_scan_stats["available"],
            self.last_scan_stats["numeric_stock"],
            duplicate_product_ids,
            self._scan_request_count,
            duration,
        )
        return products

    def parse_checkout(
        self,
        page_html: str,
        checkout_url: str,
        references: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Product:
        parsed_url = urlparse(checkout_url)
        parts = [part for part in parsed_url.path.split("/") if part]
        if len(parts) != 4 or parts[0] != "products" or parts[3] != "checkout":
            raise ParseError("FACHOST extra product has an invalid checkout URL")
        category_slug, product_slug = parts[1], parts[2]

        soup = BeautifulSoup(page_html, "lxml")
        component = soup.find(
            attrs={"wire:name": "products.checkout", "wire:snapshot": True}
        )
        if not isinstance(component, Tag):
            raise ParseError(
                "FACHOST checkout %s has no checkout Livewire snapshot" % product_slug
            )
        snapshot = self._snapshot(component, "checkout %s" % product_slug)
        memo_path = str((snapshot.get("memo") or {}).get("path") or "").strip("/")
        expected_path = "/".join(parts)
        if memo_path != expected_path:
            raise ParseError(
                "FACHOST checkout %s snapshot path mismatch" % product_slug
            )
        data = snapshot.get("data")
        if not isinstance(data, dict):
            raise ParseError("FACHOST checkout %s snapshot has no data" % product_slug)
        product_id = self._model_key(data.get("product"), "checkout %s" % product_slug)
        category_id = self._model_key(
            data.get("category"), "checkout %s" % product_slug
        )

        checkout_button = component.find("button", attrs={"wire:click": "checkout"})
        if not isinstance(checkout_button, Tag) or checkout_button.has_attr("disabled"):
            raise ParseError(
                "FACHOST checkout %s is not currently orderable" % product_slug
            )
        name = self._text(component.select_one("h1"))
        if not name:
            raise ParseError("FACHOST checkout %s has no product name" % product_slug)

        total = data.get("total")
        if not isinstance(total, list) or not total or not isinstance(total[0], dict):
            raise ParseError("FACHOST checkout %s has invalid pricing" % product_slug)
        total_data = total[0]
        formatted = total_data.get("formatted")
        if not isinstance(formatted, dict) or not formatted.get("price"):
            raise ParseError("FACHOST checkout %s has no formatted price" % product_slug)
        price = str(formatted["price"])
        currency_data = total_data.get("currency")
        currency = (
            str(currency_data.get("code"))
            if isinstance(currency_data, dict) and currency_data.get("code")
            else self._currency(soup)
        )
        plan_id = str(data.get("plan_id") or "")
        plan_option = component.select_one('select[id="plan_id"] option[value="%s"]' % plan_id)
        billing_cycle = self._billing_cycle(self._text(plan_option))

        description_node = component.select_one("article.prose")
        description = self._text(description_node)
        raw_specs: Dict[str, str] = {}
        specs: Dict[str, str] = {}
        if description_node:
            for row in description_node.select("ul > li > p"):
                strong = row.find("strong")
                if not isinstance(strong, Tag):
                    continue
                key = (self._text(strong) or "").rstrip(":：")
                value = self._text(row)
                if not key or not value:
                    continue
                value = re.sub(
                    r"^%s\s*[:：]?\s*" % re.escape(self._text(strong) or ""),
                    "",
                    value,
                    count=1,
                ).strip()
                if not value:
                    continue
                raw_specs[key] = value
                normalized = {
                    "cpu": "cpu",
                    "memory": "ram",
                    "storage": "disk",
                    "traffic": "traffic",
                    "bandwidth": "bandwidth",
                }.get(key.casefold())
                if normalized:
                    specs[normalized] = value
                elif key.casefold() == "network":
                    ipv4 = self._network_part(value, "IPv4")
                    ipv6 = self._network_part(value, "IPv6")
                    if ipv4:
                        specs["ipv4"] = ipv4
                    if ipv6:
                        specs["ipv6"] = ipv6

        category_label = category_slug
        if references and category_slug in references:
            labels = references[category_slug].get("labels", [])
            if labels:
                category_label = str(labels[0])
        category_record = {
            "label": category_label,
            "slug": category_slug,
            "category_id": category_id,
        }
        return Product(
            provider=self.name,
            product_id=product_id,
            name=name,
            category=category_label,
            region=self._region(category_label),
            price=price,
            billing_cycle=billing_cycle,
            stock=None,
            available=True,
            url=checkout_url,
            specs=specs,
            metadata={
                "discovery": {
                    "type": "extra_url",
                    "hidden": True,
                    "source": "extra_product_urls",
                },
                "currency": currency,
                "category_slug": category_slug,
                "category_id": category_id,
                "categories": [category_record],
                "product_slug": product_slug,
                "product_type": None,
                "description": description,
                "raw_specs": raw_specs,
                "detail_url": checkout_url.rsplit("/checkout", 1)[0],
                "checkout_url": checkout_url,
                "stock_source": "checkout.orderable",
                "hidden_public": True,
                "discovery_source": "configured_public_checkout",
                "plan_id": plan_id or None,
            },
        )

    def parse_category(
        self,
        page_html: str,
        category_url: str,
        category_slug: str,
        references: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Product]:
        soup = BeautifulSoup(page_html, "lxml")
        component = soup.find(attrs={"wire:name": "products", "wire:snapshot": True})
        if not isinstance(component, Tag):
            raise ParseError(
                "FACHOST category %s has no products Livewire snapshot" % category_slug
            )
        snapshot = self._snapshot(component, category_slug)
        memo_path = str((snapshot.get("memo") or {}).get("path") or "").strip("/")
        if memo_path != "products/%s" % category_slug:
            raise ParseError(
                "FACHOST category %s snapshot path mismatch: %s"
                % (category_slug, memo_path or "missing")
            )

        data = snapshot.get("data")
        if not isinstance(data, dict):
            raise ParseError("FACHOST category %s snapshot has no data" % category_slug)
        product_ids = self._model_keys(data.get("products"), "products", category_slug)
        category_ids = self._model_keys(data.get("categories"), "categories", category_slug)
        child_category_ids = self._model_keys(
            data.get("childCategories"), "childCategories", category_slug
        )
        category_id = self._model_key(data.get("category"), category_slug)

        navigation = self._component_categories(component, category_url)
        if not navigation:
            raise ParseError(
                "FACHOST category %s has no category navigation" % category_slug
            )
        discovered_category_ids = category_ids + child_category_ids
        if len(navigation) != len(discovered_category_ids):
            raise ParseError(
                "FACHOST category %s navigation/category ID count mismatch (%d != %d)"
                % (category_slug, len(navigation), len(discovered_category_ids))
            )
        navigation_ids = {
            slug: discovered_id
            for (slug, _label), discovered_id in zip(
                navigation, discovered_category_ids
            )
        }
        if navigation_ids.get(category_slug) != category_id:
            raise ParseError(
                "FACHOST category %s current category ID mismatch" % category_slug
            )
        if references is not None:
            for (slug, label), discovered_id in zip(
                navigation, discovered_category_ids
            ):
                record = references.setdefault(
                    slug, {"labels": [], "sources": set(), "category_ids": set()}
                )
                if label and label not in record["labels"]:
                    record["labels"].append(label)
                record["sources"].add("category_snapshot")
                record["category_ids"].add(discovered_id)
                if len(record["category_ids"]) > 1:
                    raise ParseError(
                        "FACHOST category %s has inconsistent category IDs" % slug
                    )

        cards = component.select("article.fh-product-card")
        if len(cards) != len(product_ids):
            raise ParseError(
                "FACHOST category %s product ID/card count mismatch (%d != %d)"
                % (category_slug, len(product_ids), len(cards))
            )

        heading = self._text(component.select_one(".fh-shop-header h1"))
        if not heading:
            raise ParseError("FACHOST category %s has no heading" % category_slug)
        kicker = self._text(component.select_one(".fh-kicker"))
        description = self._text(component.select_one(".fh-category-description"))
        currency = self._currency(soup)
        return [
            self._parse_product(
                card,
                product_id,
                category_url,
                category_slug,
                category_id,
                heading,
                kicker,
                description,
                currency,
            )
            for product_id, card in zip(product_ids, cards)
        ]

    def _parse_product(
        self,
        card: Tag,
        product_id: str,
        category_url: str,
        category_slug: str,
        category_id: str,
        category_label: str,
        category_kicker: Optional[str],
        category_description: Optional[str],
        currency: Optional[str],
    ) -> Product:
        name = self._text(card.select_one(".fh-product-top h2"))
        if not name:
            raise ParseError("FACHOST product %s has no name" % product_id)

        stock_node = card.select_one(".fh-stock")
        stock_text = self._text(stock_node)
        if not stock_text:
            raise ParseError("FACHOST product %s has no stock marker" % product_id)
        stock_match = self.AVAILABLE_STOCK.search(stock_text)
        lowered_stock = stock_text.casefold()
        stock: Optional[int]
        available: Optional[bool]
        stock_source: str
        if stock_match:
            stock = int(stock_match.group(1))
            available = stock > 0
            stock_source = "category_card.numeric_available"
        elif any(marker in lowered_stock for marker in self.SOLD_OUT_MARKERS):
            stock = None
            available = False
            stock_source = "category_card.sold_out"
        elif "available" in lowered_stock:
            stock = None
            available = True
            stock_source = "category_card.boolean_available"
        else:
            raise ParseError(
                "FACHOST product %s has unrecognized stock marker %r"
                % (product_id, stock_text)
            )

        price_node = card.select_one(".fh-product-price")
        price_amount = self._text(price_node.select_one("strong")) if price_node else None
        price_suffix = self._text(price_node.select_one("span")) if price_node else None
        price = " ".join(value for value in (price_amount, price_suffix) if value) or None
        billing_cycle = self._billing_cycle(price_suffix)

        details = card.select_one("a.fh-details-link[href]")
        buy = card.select_one("a.fh-product-buy[href]")
        detail_url = urljoin(category_url, str(details.get("href"))) if details else None
        checkout_url = urljoin(category_url, str(buy.get("href"))) if buy else None
        if not detail_url:
            raise ParseError("FACHOST product %s has no detail URL" % product_id)

        specs: Dict[str, str] = {}
        raw_specs: Dict[str, str] = {}
        for row in card.select(".fh-product-specs > div"):
            key = self._text(row.select_one("dt"))
            value = self._text(row.select_one("dd"))
            if not key or not value:
                raise ParseError("FACHOST product %s has an incomplete spec row" % product_id)
            raw_specs[key] = value
            normalized = {
                "cpu": "cpu",
                "memory": "ram",
                "storage": "disk",
                "traffic": "traffic",
                "bandwidth": "bandwidth",
            }.get(key.casefold())
            if normalized:
                specs[normalized] = value
            elif key.casefold() == "network":
                ipv4 = self._network_part(value, "IPv4")
                ipv6 = self._network_part(value, "IPv6")
                if ipv4:
                    specs["ipv4"] = ipv4
                if ipv6:
                    specs["ipv6"] = ipv6

        product_type = self._text(card.select_one(".fh-product-type"))
        product_slug = urlparse(detail_url).path.rstrip("/").rsplit("/", 1)[-1]
        category_record = {
            "label": category_label,
            "slug": category_slug,
            "category_id": category_id,
        }
        return Product(
            provider=self.name,
            product_id=product_id,
            name=name,
            category=category_label,
            region=self._region(category_label),
            price=price,
            billing_cycle=billing_cycle,
            stock=stock,
            available=available,
            url=checkout_url or detail_url,
            specs=specs,
            metadata={
                "discovery": {
                    "type": "catalog",
                    "hidden": False,
                    "source": "category",
                },
                "currency": currency,
                "category_slug": category_slug,
                "category_id": category_id,
                "categories": [category_record],
                "product_slug": product_slug,
                "product_type": product_type,
                "category_kicker": category_kicker,
                "description": category_description,
                "raw_specs": raw_specs,
                "detail_url": detail_url,
                "checkout_url": checkout_url,
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
    ) -> None:
        soup = BeautifulSoup(document.text, "lxml")
        for link in soup.select("a[href]"):
            absolute = urljoin(document.url, str(link.get("href")))
            slug = self._category_slug(absolute)
            if not slug:
                continue
            label = self._text(link)
            record = references.setdefault(
                slug, {"labels": [], "sources": set(), "category_ids": set()}
            )
            if label and label not in record["labels"]:
                record["labels"].append(label)
            record["sources"].add(source)

    def _extra_checkout_urls(self, base_url: str) -> List[str]:
        configured = self.config.get("extra_product_urls", [])
        if configured is None:
            return []
        if not isinstance(configured, list):
            raise ParseError("FACHOST extra_product_urls must be a list")
        base = urlparse(base_url)
        urls: List[str] = []
        for value in configured:
            absolute = urljoin(base_url, str(value))
            parsed = urlparse(absolute)
            parts = [part for part in parsed.path.split("/") if part]
            if (
                parsed.netloc.casefold() != base.netloc.casefold()
                or len(parts) != 4
                or parts[0] != "products"
                or parts[3] != "checkout"
            ):
                raise ParseError("FACHOST has an invalid extra product checkout URL")
            if absolute not in urls:
                urls.append(absolute)
        return urls

    def _component_categories(self, component: Tag, category_url: str) -> List[Any]:
        categories = []
        seen: Set[str] = set()
        for link in component.select(".fh-category-panel nav a[href]"):
            absolute = urljoin(category_url, str(link.get("href")))
            slug = self._category_slug(absolute)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            categories.append((slug, self._text(link)))
        return categories

    def _category_slug(self, url: str) -> Optional[str]:
        parsed = urlparse(urljoin(str(self.config.get("base_url", self.BASE_URL)), url))
        base = urlparse(str(self.config.get("base_url", self.BASE_URL)))
        if parsed.netloc.casefold() != base.netloc.casefold():
            return None
        match = self.CATEGORY_PATH.fullmatch(parsed.path)
        return match.group(1) if match else None

    @staticmethod
    def _snapshot(component: Tag, category_slug: str) -> Dict[str, Any]:
        try:
            value = json.loads(html.unescape(str(component.get("wire:snapshot") or "")))
        except (TypeError, ValueError) as exc:
            raise ParseError(
                "FACHOST category %s has invalid Livewire snapshot" % category_slug
            ) from exc
        if not isinstance(value, dict):
            raise ParseError(
                "FACHOST category %s has invalid Livewire snapshot type" % category_slug
            )
        return value

    @staticmethod
    def _model_keys(value: Any, label: str, category_slug: str) -> List[str]:
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[1], dict)
            or not isinstance(value[1].get("keys"), list)
        ):
            raise ParseError(
                "FACHOST category %s has invalid %s collection"
                % (category_slug, label)
            )
        keys = [str(key) for key in value[1]["keys"]]
        if any(not key.isdigit() for key in keys) or len(keys) != len(set(keys)):
            raise ParseError(
                "FACHOST category %s has invalid %s IDs" % (category_slug, label)
            )
        return keys

    @staticmethod
    def _model_key(value: Any, category_slug: str) -> str:
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[1], dict)
        ):
            raise ParseError(
                "FACHOST category %s has invalid category model" % category_slug
            )
        key = str(value[1].get("key", ""))
        if not key.isdigit():
            raise ParseError(
                "FACHOST category %s has invalid category ID" % category_slug
            )
        return key

    @staticmethod
    def _currency(soup: BeautifulSoup) -> Optional[str]:
        component = soup.find(
            attrs={"wire:name": "components.currency-switch", "wire:snapshot": True}
        )
        if not isinstance(component, Tag):
            return None
        try:
            snapshot = json.loads(html.unescape(str(component.get("wire:snapshot") or "")))
        except (TypeError, ValueError):
            return None
        currency = (snapshot.get("data") or {}).get("currentCurrency")
        return str(currency) if currency else None

    @staticmethod
    def _billing_cycle(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        lowered = value.casefold().strip(" /\t\r\n")
        mapping = {
            "day": "daily",
            "week": "weekly",
            "month": "monthly",
            "year": "yearly",
            "one time": "one-time",
            "月付": "monthly",
            "年付": "yearly",
        }
        for marker, normalized in mapping.items():
            if lowered == marker or marker in lowered:
                return normalized
        return lowered or None

    @staticmethod
    def _network_part(value: str, family: str) -> Optional[str]:
        match = re.search(r"([^+，,;]*%s[^+，,;]*)" % re.escape(family), value, re.I)
        return " ".join(match.group(1).split()) if match else None

    @staticmethod
    def _region(category_label: str) -> str:
        parts = [part for part in category_label.split("-") if part]
        if not parts:
            return category_label
        countries = {"tw": "Taiwan", "hk": "Hong Kong"}
        country = countries.get(parts[0].casefold(), parts[0])
        detail = [part for part in parts[1:] if part.casefold() not in {"vds", "vps"}]
        return " · ".join([country] + detail) if detail else country

    @staticmethod
    def _text(node: Optional[Tag]) -> Optional[str]:
        if not node:
            return None
        value = " ".join(node.get_text(" ", strip=True).split())
        return value or None

    @staticmethod
    def _merge_duplicate(existing: Product, duplicate: Product) -> None:
        for field in ("name", "price", "stock", "available"):
            if getattr(existing, field) != getattr(duplicate, field):
                raise ParseError(
                    "FACHOST product ID %s has inconsistent %s across categories"
                    % (existing.product_id, field)
                )
        categories = existing.metadata.setdefault("categories", [])
        for category in duplicate.metadata.get("categories", []):
            if category not in categories:
                categories.append(category)
