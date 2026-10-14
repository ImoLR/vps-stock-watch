"""NewNB Cloud (nnb.ltd).

The storefront is a Nuxt SPA; its order page calls the public
``GET https://api.nnb.ltd/api/v1/nodes``. NewNB sells configurable VMs per
node rather than fixed plans: each node has resource limits, per-unit
monthly prices and one or more network cards (NICs), each NIC with its own
fee and monthly traffic allowance. The cheapest order is the node minimum
on every resource, so ``base + NIC fee`` is the advertised starting price.

The API exposes no stock counter. A node is orderable while it is listed
with ``orderable: true`` and ``status: active``; nodes are added for a sale
and switched off when it sells out.

The pricing helpers mirror the site's own bundle (``BZwFLV5w.js``) and are
shared with :mod:`watcher.nnb_autobuy`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..models import Product
from .base import BaseProvider, ParseError


API_BASE = "https://api.nnb.ltd/api/v1"
SITE = "https://nnb.ltd"
RAW_PER_YUAN = 10000


class NnbProvider(BaseProvider):
    def fetch_products(self) -> List[Product]:
        base = str(self.config.get("api_base", API_BASE)).rstrip("/")
        return self.parse_nodes(self.get_json(base + "/nodes"))

    def parse_nodes(self, payload: Any) -> List[Product]:
        products: List[Product] = []
        for node in node_items(payload):
            nics = [nic for nic in node.get("primary_nics") or [] if isinstance(nic, dict)]
            for nic in nics or [None]:
                products.append(self._product(node, nic))
        if not products:
            raise ParseError("NewNB node list is empty; refusing an empty snapshot")
        return products

    def _product(self, node: Dict[str, Any], nic: Optional[Dict[str, Any]]) -> Product:
        node_id = node["id"]
        group = node.get("group") if isinstance(node.get("group"), dict) else {}
        product_id = "node-%s" % node_id
        name = str(node.get("name") or product_id)
        if nic is not None:
            product_id += "-nic-%s" % nic.get("id")
            name += " · %s" % (nic.get("label") or "NIC %s" % nic.get("id"))
        spec = default_spec(node, nic)
        price = starting_price_raw(node, nic, spec)
        specs = {
            "cpu": "%s vCPU" % spec["cpu"],
            "ram": "%s GB" % _number(spec["memory_gb"]),
            "disk": "%s GB" % _number(spec["disk_gb"]),
            "bandwidth": "%s Mbps" % spec["bandwidth_mbps"],
        }
        if spec.get("traffic_gb") is not None:
            specs["traffic"] = "%s GB/月" % spec["traffic_gb"]
        if nic is not None and nic.get("cmcc_nat"):
            specs["network"] = "CMCC NAT"
        extras = [
            "%s +¥%s" % (extra.get("label"), yuan(_raw(extra.get("monthly_price"))))
            for extra in node.get("extra_nics") or []
            if isinstance(extra, dict)
        ]
        if extras:
            specs["extra_nics"] = "; ".join(extras)
        return Product(
            provider=self.name,
            product_id=product_id,
            name=name,
            category=str(group.get("name") or "NewNB"),
            region=str(node.get("region") or "") or None,
            price="¥%s CNY 起" % yuan(price),
            billing_cycle="monthly" if node.get("monthly_available") else "hourly",
            stock=None,
            available=node_available(node),
            url="%s/dashboard/create?node=%s" % (SITE, node_id),
            specs=specs,
            metadata={
                "discovery": {"type": "catalog", "hidden": False, "source": "public_api"},
                "node_id": node_id,
                "nic_id": nic.get("id") if nic else None,
                "status": node.get("status"),
                "orderable": node.get("orderable"),
                "provisionable": node.get("provisionable"),
                "monthly_available": node.get("monthly_available"),
                "hourly_available": node.get("hourly_available"),
                "description": str(node.get("description") or ""),
                "nic_hint": str((nic or {}).get("hint") or ""),
            },
        )


def node_items(payload: Any) -> List[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ParseError("NewNB /nodes response has no data.items list")
    nodes = []
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        if not isinstance(item.get("limits"), dict) or not isinstance(
            item.get("monthly_prices"), dict
        ):
            raise ParseError("NewNB node %s has no limits/monthly_prices" % item.get("id"))
        nodes.append(item)
    return nodes


def node_available(node: Dict[str, Any]) -> bool:
    return (
        node.get("orderable") is not False
        and str(node.get("status") or "active") == "active"
        and bool(node.get("monthly_available") or node.get("hourly_available"))
    )


def yuan(raw: int) -> str:
    return str((Decimal(int(raw)) / RAW_PER_YUAN).quantize(Decimal("0.01")))


def _raw(price: Any) -> int:
    if isinstance(price, dict) and isinstance(price.get("raw"), int):
        return price["raw"]
    return 0


def _number(value: Any) -> str:
    return ("%f" % float(value)).rstrip("0").rstrip(".")


def _clamp(limit: Dict[str, Any], value: float) -> float:
    return min(limit["max"], max(limit["min"], value))


def traffic_bandwidth_cap(traffic: Optional[Dict[str, Any]], gb: int, cap: int) -> int:
    """Highest bandwidth allowed for this traffic amount (the site's tier table)."""
    for tier in (traffic or {}).get("tiers") or []:
        if gb <= tier.get("up_to_gb", 0):
            return min(int(tier.get("max_bandwidth_mbps") or cap), cap)
    return cap


def default_spec(node: Dict[str, Any], nic: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The cheapest monthly configuration: every resource at the node minimum.

    Bandwidth is free on every node seen so far, so it is raised to the most
    the minimum traffic tier allows; otherwise it stays at the minimum.
    """
    limits = node["limits"]
    traffic = (nic or {}).get("traffic") if isinstance((nic or {}).get("traffic"), dict) else None
    spec: Dict[str, Any] = {
        "cpu": int(limits["cpu"]["min"]),
        "memory_gb": limits["memory_gb"]["min"],
        "disk_gb": int(limits["disk_gb"]["min"]),
        "bandwidth_mbps": int(limits["bandwidth_mbps"]["min"]),
        "traffic_gb": int(traffic["min_gb"]) if traffic else None,
    }
    if _raw(node["monthly_prices"].get("bandwidth")) == 0:
        cap = int(limits["bandwidth_mbps"]["max"])
        if traffic:
            cap = traffic_bandwidth_cap(traffic, spec["traffic_gb"], cap)
        spec["bandwidth_mbps"] = int(_clamp(limits["bandwidth_mbps"], cap))
    return spec


def _memory_raw(gb: float, unit_raw: int) -> int:
    # Memory is billed in 0.5 GB steps.
    return int(round(gb / 0.5) * unit_raw // 2)


def resources_raw(node: Dict[str, Any], spec: Dict[str, Any]) -> int:
    prices = node["monthly_prices"]
    limits = node["limits"]

    def over(key: str) -> float:
        return max(spec[key] - limits[key]["min"], 0)

    return (
        _raw(prices.get("base"))
        + int(over("cpu")) * _raw(prices.get("cpu"))
        + _memory_raw(over("memory_gb"), _raw(prices.get("memory_gb")))
        + int(over("disk_gb")) * _raw(prices.get("disk_gb"))
        + int(over("bandwidth_mbps")) * _raw(prices.get("bandwidth"))
    )


def traffic_raw(traffic: Optional[Dict[str, Any]], gb: Optional[int]) -> int:
    if not traffic or gb is None:
        return 0
    step = max(int(traffic.get("step_gb") or 1), 1)
    cost = (max(gb - int(traffic["min_gb"]), 0) // step) * _raw(traffic.get("price"))
    for tier in traffic.get("tiers") or []:
        if gb <= tier.get("up_to_gb", 0):
            discount = min(int(tier.get("discount") or 0), 100)
            return cost * (100 - discount) // 100 if discount > 0 else cost
    return cost


def extra_nic_raw(extra: Dict[str, Any], bandwidth_mbps: int) -> int:
    cost = _raw(extra.get("monthly_price"))
    if extra.get("max_bandwidth_mbps"):
        cost += max(bandwidth_mbps - int(extra.get("min_bandwidth_mbps") or 0), 0) * _raw(
            extra.get("bandwidth_monthly_price")
        )
    return cost


def starting_price_raw(
    node: Dict[str, Any],
    nic: Optional[Dict[str, Any]],
    spec: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    """Monthly price for one month, before any coupon."""
    total = resources_raw(node, spec)
    if nic is not None:
        total += _raw(nic.get("monthly_price"))
        total += traffic_raw(nic.get("traffic"), spec.get("traffic_gb"))
    if extra is not None:
        extra_bw = int(extra.get("min_bandwidth_mbps") or 0)
        total += extra_nic_raw(extra, extra_bw)
        traffic = extra.get("traffic") if isinstance(extra.get("traffic"), dict) else None
        if traffic:
            total += traffic_raw(traffic, int(traffic["min_gb"]))
    return total
