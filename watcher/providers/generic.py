from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from lxml import html as lxml_html

from ..models import Product
from ..rules import RuleError, extract_all, extract_one
from .base import BaseProvider, ParseError


class GenericProvider(BaseProvider):
    """Config-driven provider for CSS, XPath, regex, and JSONPath sources."""

    def fetch_products(self) -> List[Product]:
        url = str(self.config["catalog_url"])
        raw = self.get_text(url, allow_browser=self.config.get("fetch_mode") == "browser")
        source_format = str(self.config.get("format", "html")).lower()
        if source_format == "json":
            try:
                document: Any = json.loads(raw)
            except ValueError as exc:
                raise ParseError("generic provider returned invalid JSON") from exc
        elif source_format == "html":
            document = BeautifulSoup(raw, "lxml")
        else:
            raise ParseError("generic format must be html or json")

        item_rule = self.config.get("item_rule")
        if not isinstance(item_rule, dict):
            raise ParseError("generic provider requires item_rule")
        items = self._items(document, raw, item_rule)
        products = [self._product(item, raw, url, index) for index, item in enumerate(items)]
        if not products:
            raise ParseError("generic provider extracted no products")
        return products

    def _items(self, document: Any, raw: str, rule: Dict[str, Any]) -> Iterable[Any]:
        kind = str(rule.get("type", "css")).lower()
        if kind == "css":
            return document.select(str(rule["selector"]))
        if kind == "xpath":
            tree = lxml_html.fromstring(raw)
            return tree.xpath(str(rule["expression"]))
        return extract_all(document, rule, raw)

    def _product(self, item: Any, raw: str, base_url: str, index: int) -> Product:
        fields = self.config.get("fields", {})
        if not isinstance(fields, dict):
            raise ParseError("generic fields must be a mapping")
        values: Dict[str, Any] = {}
        for name, rule in fields.items():
            if isinstance(rule, dict):
                values[name] = extract_one(item, rule, self._item_raw(item))
            else:
                values[name] = rule
        product_id = values.get("product_id")
        if product_id is None:
            product_id = values.get("url") or values.get("name")
        if product_id is None:
            raise RuleError("generic product %s has no product_id, URL, or name" % index)
        available = self._available(item, values)
        url = values.get("url")
        if url:
            url = urljoin(base_url, str(url))
        stock = values.get("stock")
        if stock is not None:
            stock = int(stock)
        specs = {key: str(values[key]) for key in self.config.get("spec_fields", []) if values.get(key)}
        return Product(
            provider=self.name,
            product_id=str(product_id),
            name=str(values.get("name") or product_id),
            category=_string(values.get("category")),
            region=_string(values.get("region")),
            price=_string(values.get("price")),
            billing_cycle=_string(values.get("billing_cycle")),
            stock=stock,
            available=available,
            url=url,
            specs=specs,
        )

    def _available(self, item: Any, values: Dict[str, Any]) -> Optional[bool]:
        if values.get("available") is not None:
            value = values["available"]
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "available", "in stock")
        soldout_rule = self.config.get("soldout_rule")
        if isinstance(soldout_rule, dict):
            return not bool(extract_all(item, soldout_rule, self._item_raw(item)))
        stock = values.get("stock")
        return int(stock) > 0 if stock is not None else None

    @staticmethod
    def _item_raw(item: Any) -> str:
        try:
            return str(item)
        except Exception:
            return ""


def _string(value: Any) -> Optional[str]:
    return None if value is None else str(value)
