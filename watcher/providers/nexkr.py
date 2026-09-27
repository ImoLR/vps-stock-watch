from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..models import Product
from .base import BaseProvider, ParseError


class NexKrProvider(BaseProvider):
    API_URL = "https://nexkr.sh/api/v1/shop/groups"
    SHOP_URL = "https://nexkr.sh/app/shop"

    def fetch_products(self) -> List[Product]:
        payload = self.get_json(str(self.config.get("catalog_url", self.API_URL)))
        groups = payload.get("groups") if isinstance(payload, dict) else None
        if not isinstance(groups, list):
            raise ParseError("NexKr response has no groups list")
        products: List[Product] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            products.extend(self._products_from_group(group))
        if not products:
            raise ParseError("NexKr catalog contained no products; refusing an empty snapshot")
        return products

    def _products_from_group(self, group: Dict[str, Any]) -> List[Product]:
        group_id = group.get("id")
        zones = {
            zone.get("id"): zone
            for zone in _nullable_list(group.get("zones"), "group %s zones" % group_id)
            if isinstance(zone, dict)
        }
        subgroups = {
            item.get("id"): item
            for item in _nullable_list(
                group.get("subgroups"), "group %s subgroups" % group_id
            )
            if isinstance(item, dict)
        }
        result: List[Product] = []
        for item in _nullable_list(group.get("products"), "group %s products" % group_id):
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            zone_ids = _nullable_list(
                item.get("zones"), "product %s zones" % item.get("id")
            )
            item_zones = [zones[value] for value in zone_ids if value in zones]
            subgroup = subgroups.get(item.get("subgroup_id"), {})
            region_parts = []
            subgroup_title = subgroup.get("title") or subgroup.get("slug")
            if subgroup_title:
                region_parts.append(str(subgroup_title))
            region_parts.extend(
                str(zone.get("title") or zone.get("slug")) for zone in item_zones
            )
            region = " / ".join(dict.fromkeys(region_parts)) or None
            specs = self._specs(item)
            description = str(item.get("description") or "")
            price = None
            if item.get("price_usd") is not None:
                price = "$%s USD" % item["price_usd"]
            cycle_months = item.get("cycle_months")
            billing_cycle = "%s month%s" % (
                cycle_months,
                "" if cycle_months == 1 else "s",
            ) if cycle_months else None
            slug = str(item.get("slug") or item["id"])
            result.append(
                Product(
                    provider=self.name,
                    product_id=str(item["id"]),
                    name=str(item.get("name") or slug),
                    category=str(group.get("title") or group.get("type") or "VPS"),
                    region=region,
                    price=price,
                    billing_cycle=billing_cycle,
                    stock=_optional_int(item.get("stock")),
                    available=_optional_bool(item.get("in_stock")),
                    url="https://nexkr.sh/app/buy/%s" % quote(slug, safe="-._~"),
                    specs=specs,
                    metadata={
                        "discovery": {
                            "type": "catalog",
                            "hidden": False,
                            "source": "public_api",
                        },
                        "slug": slug,
                        "group_id": item.get("group_id", group.get("id")),
                        "group_slug": group.get("slug"),
                        "subgroup": subgroup.get("title") or subgroup.get("slug"),
                        "zone_ids": zone_ids,
                        "limited": item.get("limited"),
                        "setup_fee_cents": item.get("setup_fee_cents"),
                        "description": description,
                    },
                )
            )
        return result

    @staticmethod
    def _specs(item: Dict[str, Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if item.get("cores") is not None:
            result["cpu"] = "%s vCPU" % item["cores"]
        if item.get("memory_mb") is not None:
            result["ram"] = "%s MB" % item["memory_mb"]
        if item.get("disk_gb") is not None:
            result["disk"] = "%s GB" % item["disk_gb"]
        if item.get("bandwidth_gb") is not None:
            result["traffic"] = "%s GB" % item["bandwidth_gb"]

        description = str(item.get("description") or "")
        resource = re.search(r"(?im)^-?\s*(\d+)c(\d+)g\s+Dedicated vResource", description)
        if resource:
            result.setdefault("cpu", "%s dedicated vCPU" % resource.group(1))
            result.setdefault("ram", "%s GB" % resource.group(2))
        disk = re.search(r"(?im)^-?\s*([^\n]*\b(?:ssd|nvme)\b[^\n]*)", description)
        if disk:
            result.setdefault("disk", disk.group(1).strip())
        traffic = re.search(r"(?im)^-?\s*([^\n]*\b(?:traffic|transfer)\b[^\n]*)", description)
        if traffic:
            result.setdefault("traffic", traffic.group(1).strip())
        bandwidth = re.search(r"(?im)^-?\s*([^\n]*(?:Mbps|Gbps)[^\n]*)", description)
        if bandwidth:
            result.setdefault("bandwidth", bandwidth.group(1).strip())
        ipv4 = re.search(r"(?im)^-?\s*([^\n]*\bIPv4\b[^\n]*)", description)
        if ipv4:
            result["ipv4"] = ipv4.group(1).strip()
        ipv6 = re.search(r"(?im)^-?\s*([^\n]*\bIPv6\b[^\n]*)", description)
        if ipv6:
            result["ipv6"] = ipv6.group(1).strip()
        return result


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nullable_list(value: Any, label: str) -> List[Any]:
    """Treat an API null list as empty, but reject other malformed types."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ParseError("NexKr %s must be a list or null" % label)
    return value


def _optional_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None
