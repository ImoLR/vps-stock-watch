from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..models import Change, Product
from .base import BaseProvider, ParseError


class BlossomProvider(BaseProvider):
    CATALOG_URL = "https://blossomhost.us/api/catalog"
    BUY_URL = "https://blossomhost.us/#/buy/"
    SILENT_NOTIFICATION_FAMILIES = frozenset(("isp", "metal"))

    def fetch_products(self) -> List[Product]:
        payload = self.get_json(str(self.config.get("catalog_url", self.CATALOG_URL)))
        return self.parse_catalog(payload)

    def parse_catalog(self, payload: Any) -> List[Product]:
        plans = payload.get("plans") if isinstance(payload, dict) else None
        if not isinstance(plans, list) or not plans:
            raise ParseError("Blossom catalog has no non-empty plans list")

        products: List[Product] = []
        seen = set()
        for index, plan in enumerate(plans):
            if not isinstance(plan, dict):
                raise ParseError("Blossom plan %s is not an object" % index)
            key = str(plan.get("key") or "").strip()
            if not key:
                raise ParseError("Blossom plan %s has no stable key" % index)
            if key in seen:
                raise ParseError("Blossom catalog contains duplicate plan key %s" % key)
            seen.add(key)

            available = plan.get("in_stock")
            if not isinstance(available, bool):
                raise ParseError("Blossom plan %s has no boolean in_stock" % key)

            name = str(plan.get("name") or key).strip()
            family = _text(plan.get("family"))
            products.append(
                Product(
                    provider=self.name,
                    product_id=key,
                    name=name,
                    category=_text(plan.get("family_label") or family),
                    region=_text(plan.get("location")),
                    price=_monthly_price(plan.get("monthly_usd")),
                    billing_cycle="monthly",
                    stock=_optional_int(plan.get("stock_count")),
                    available=available,
                    url=self.BUY_URL + quote(key, safe="-._~"),
                    specs=self._specs(plan),
                    metadata={
                        "discovery": {
                            "type": "catalog",
                            "hidden": False,
                            "source": "public_api",
                        },
                        "catalog_revision": payload.get("catalog_revision"),
                        "offer_key": plan.get("offer_key"),
                        "offer_label": plan.get("offer_label"),
                        "family": family,
                        "notification_suppressed": self._silent_family(family),
                        "carrier": plan.get("carrier"),
                        "coast": plan.get("coast"),
                        "availability_status": plan.get("availability_status"),
                        "capacity_mode": plan.get("capacity_mode"),
                        "provider_stock_count": plan.get("provider_stock_count"),
                        "coming_soon": plan.get("coming_soon"),
                        "physical": plan.get("physical"),
                        "last_unit": plan.get("last_unit"),
                    },
                )
            )
        residential = payload.get("residential_inventory", [])
        if not isinstance(residential, list):
            raise ParseError("Blossom residential_inventory is not a list")
        for index, offer in enumerate(residential):
            if not isinstance(offer, dict):
                raise ParseError("Blossom residential offer %s is not an object" % index)
            product = self._residential_product(offer, payload.get("catalog_revision"))
            if product.product_id in seen:
                raise ParseError(
                    "Blossom catalog contains duplicate product key %s" % product.product_id
                )
            seen.add(product.product_id)
            products.append(product)
        return products

    def should_notify(self, change: Change) -> bool:
        # `family` is Blossom's stable machine category. Checking it here as
        # well as storing the generic metadata flag also protects REMOVED
        # changes loaded from snapshots written before that flag existed.
        if self._silent_family(change.product.metadata.get("family")):
            return False
        return super().should_notify(change)

    @classmethod
    def _silent_family(cls, value: Any) -> bool:
        return str(value or "").strip().casefold() in cls.SILENT_NOTIFICATION_FAMILIES

    @staticmethod
    def _specs(plan: Dict[str, Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        vcpu = _optional_number(plan.get("vcpu"))
        if vcpu is not None:
            result["cpu"] = "%s %s" % (
                vcpu,
                "cores" if plan.get("physical") else "vCPU",
            )
        ram = _optional_number(plan.get("ram_gb"))
        if ram is not None:
            memory_type = _text(plan.get("memory_type"))
            result["ram"] = "%s GB%s" % (
                ram,
                " " + memory_type if memory_type and memory_type.upper() != "RAM" else "",
            )

        storage_summary = _text(plan.get("storage_summary"))
        if storage_summary:
            result["disk"] = storage_summary
        else:
            disk = _optional_number(plan.get("disk_gb"))
            hdd = _optional_number(plan.get("hdd_gb"))
            parts = []
            if disk is not None:
                parts.append("%s GB" % disk)
            if hdd is not None and Decimal(hdd) > 0:
                parts.append("%s GB HDD" % hdd)
            if parts:
                result["disk"] = " + ".join(parts)

        transfer = _optional_number(plan.get("transfer_gb"))
        if transfer is not None:
            result["traffic"] = "%s GB / month" % transfer
        network = _text(plan.get("network_label"))
        bandwidth = _optional_number(plan.get("bandwidth_gbps"))
        if network:
            result["bandwidth"] = network
        elif bandwidth is not None:
            result["bandwidth"] = "%s Gbps" % bandwidth
        ip_label = _text(plan.get("ip_label"))
        if ip_label:
            result["ipv4"] = ip_label
        return result

    def _residential_product(self, offer: Dict[str, Any], revision: Any) -> Product:
        offer_id = offer.get("offer_id")
        if offer_id is None or isinstance(offer_id, bool):
            raise ParseError("Blossom residential offer has no stable offer_id")
        product_id = "residential-offer-%s" % offer_id
        stock = _optional_int(offer.get("remaining_seats"))
        price = _money(offer.get("current_price"))
        if stock is not None:
            available: Optional[bool] = stock > 0 and price is not None
        else:
            available = False if price is None else None

        provider = offer.get("provider") if isinstance(offer.get("provider"), dict) else {}
        spec = offer.get("spec") if isinstance(offer.get("spec"), dict) else {}
        name = _text(offer.get("isp") or provider.get("name")) or product_id
        specs: Dict[str, str] = {}
        vcpu = _optional_number(spec.get("vcpu"))
        if vcpu is not None:
            specs["cpu"] = "%s vCPU" % vcpu
        ram_mb = _optional_number(spec.get("ram_mb"))
        if ram_mb is not None:
            specs["ram"] = "%s GB" % _decimal_text(Decimal(ram_mb) / Decimal(1024))
        disk = _optional_number(spec.get("disk_gb"))
        if disk is not None:
            specs["disk"] = "%s GB" % disk

        return Product(
            provider=self.name,
            product_id=product_id,
            name=name,
            category="Residential",
            region=_text(offer.get("region")),
            price=price,
            billing_cycle="monthly",
            stock=stock,
            available=available,
            url="https://blossomhost.us/#/buy",
            specs=specs,
            metadata={
                "discovery": {
                    "type": "catalog",
                    "hidden": False,
                    "source": "public_api",
                },
                "catalog_revision": revision,
                "family": "residential",
                "notification_suppressed": False,
                "offer_id": offer_id,
                "availability_status": offer.get("availability"),
                "active_seats": offer.get("active_seats"),
                "held_seats": offer.get("held_seats"),
                "total_seats": offer.get("total_seats"),
                "display_price": offer.get("display_price"),
                "asn": offer.get("asn"),
                "description": "Residential beta offer",
            },
        )


def _monthly_price(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    amount = _optional_number(value.get("linux"))
    return "$%s USD" % amount if amount is not None else None


def _money(value: Any) -> Optional[str]:
    amount = _optional_number(value)
    return "$%s USD" % amount if amount is not None else None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParseError("Blossom stock_count must be an integer or null")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ParseError("Blossom stock_count must be an integer or null")
    if parsed != parsed.to_integral_value() or parsed < 0:
        raise ParseError("Blossom stock_count must be a non-negative integer")
    return int(parsed)


def _optional_number(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return _decimal_text(parsed)


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
