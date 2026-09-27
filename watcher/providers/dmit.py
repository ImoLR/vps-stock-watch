from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag

from ..models import Product
from .base import BaseProvider, ParseError


class DmitProvider(BaseProvider):
    CATALOG_URL = "https://www.dmit.io/cart.php"

    def fetch_products(self) -> List[Product]:
        fetch_mode = str(self.config.get("fetch_mode", "auto")).lower()
        html = self.get_text(
            str(self.config.get("catalog_url", self.CATALOG_URL)),
            allow_browser=fetch_mode in ("auto", "browser"),
        )
        products = self.parse_catalog(html)
        return self._merge_extra_pid_probes(products)

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
