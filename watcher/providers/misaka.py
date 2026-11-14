from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests

from ..models import Change, ChangeType, Product
from .base import BaseProvider, FetchError, ParseError, is_challenge_page


class MisakaProvider(BaseProvider):
    """Monitor Misaka's global catalog and focused APAC inventory.

    focus_regions entries match a region slug (LAX02) or a country code (HK).
    With lowest_tier_only, only the cheapest plan of each routing profile in a
    focus region keeps stock; the other plans there are catalog-only too.
    With focus_regions_only, plans are fetched for the focus regions only and
    new regions / new non-watched plans no longer alert.
    """

    API_BASE = "https://api.misaka.io/mc2"
    BUY_BASE = "https://app.misaka.io/iaas/vm/create"
    DEFAULT_FOCUS_REGIONS = ("HK", "TW", "JP")

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        configured = config.get("focus_regions", self.DEFAULT_FOCUS_REGIONS)
        if not isinstance(configured, list) and not isinstance(configured, tuple):
            raise ValueError("Misaka focus_regions must be a list")
        self.focus_regions = tuple(
            str(value).strip().upper() for value in configured if str(value).strip()
        )
        if not self.focus_regions:
            raise ValueError("Misaka focus_regions cannot be empty")
        labels = config.get("focus_region_labels", {})
        self.focus_region_labels = (
            {str(key).upper(): str(value) for key, value in labels.items()}
            if isinstance(labels, dict)
            else {}
        )
        self.lowest_tier_only = bool(config.get("lowest_tier_only", False))
        self.focus_regions_only = bool(config.get("focus_regions_only", False))
        self.last_scan_stats: Dict[str, Any] = {}
        self._catalog_regions: Dict[str, Dict[str, Any]] = {}
        self._lowest_tiers: Dict[str, Dict[str, Any]] = {}
        self._request_count = 0

    def fetch_products(self) -> List[Product]:
        started = time.monotonic()
        self._request_count = 0
        api_base = str(self.config.get("api_base", self.API_BASE)).rstrip("/")
        regions_payload = self._get_json("%s/regions" % api_base)
        regions = self._parse_regions(regions_payload)

        minimum_regions = max(1, int(self.config.get("minimum_regions", 1)))
        if len(regions) < minimum_regions:
            raise ParseError(
                "Misaka catalog found only %d regions; expected at least %d"
                % (len(regions), minimum_regions)
            )
        maximum_regions = max(1, int(self.config.get("max_regions", 100)))
        if len(regions) > maximum_regions:
            raise ParseError(
                "Misaka catalog found %d regions; max_regions=%d"
                % (len(regions), maximum_regions)
            )

        products: List[Product] = []
        focus_seen: Set[str] = set()
        region_plan_counts: Dict[str, int] = {}
        self._lowest_tiers = {}
        scanned = 0
        for region in regions:
            if self.focus_regions_only and self._focus_entry(region) is None:
                continue
            scanned += 1
            slug = region["slug"]
            plans_payload = self._get_json(
                "%s/regions/%s/plans" % (api_base, quote(slug, safe="-._~"))
            )
            plans = self._parse_plans(plans_payload, region)
            region_plan_counts[slug] = len(plans)
            if self._focus_entry(region) is not None:
                focus_seen.add(self._focus_entry(region))
                if not plans:
                    raise ParseError(
                        "Misaka focus region %s returned an empty plan catalog" % slug
                    )
            products.extend(plans)

        missing_focus = set(self.focus_regions) - focus_seen
        if missing_focus:
            raise ParseError(
                "Misaka catalog is missing focus regions: %s"
                % ", ".join(sorted(missing_focus))
            )
        minimum_products = max(1, int(self.config.get("minimum_global_products", 1)))
        if len(products) < minimum_products:
            raise ParseError(
                "Misaka catalog found only %d plans; expected at least %d"
                % (len(products), minimum_products)
            )

        focused = [item for item in products if item.metadata.get("focused")]
        duration = time.monotonic() - started
        self.last_scan_stats = {
            "focus_regions": list(self.focus_regions),
            "lowest_tier_only": self.lowest_tier_only,
            "focus_regions_only": self.focus_regions_only,
            "scanned_regions": scanned,
            "focused_products": len(focused),
            "focused_available": sum(item.available is True for item in focused),
            "focused_unavailable": sum(item.available is False for item in focused),
            "global_regions": len(regions),
            "global_products": len(products),
            "region_plan_counts": region_plan_counts,
            "requests": self._request_count,
            "duration_seconds": round(duration, 3),
            "numeric_stock": 0,
            "browser_required": False,
        }
        return products

    def validate_snapshot(
        self, products: List[Product], previous_state: Dict[str, Any]
    ) -> None:
        stats = self.last_scan_stats
        if not stats or not self._catalog_regions:
            raise ParseError("Misaka snapshot has no complete catalog metadata")

        previous_stats = previous_state.get("catalog_stats")
        if isinstance(previous_stats, dict):
            self._reject_large_drop(
                "regions",
                int(previous_stats.get("global_regions", 0)),
                int(stats["global_regions"]),
                float(self.config.get("minimum_region_retention_ratio", 0.8)),
            )
        # Switching between the full catalog and focus-only scans changes the
        # plan count on purpose; only compare scans of the same scope.
        if isinstance(previous_stats, dict) and bool(
            previous_stats.get("focus_regions_only")
        ) == self.focus_regions_only:
            self._reject_large_drop(
                "plans",
                int(previous_stats.get("global_products", 0)),
                int(stats["global_products"]),
                float(self.config.get("minimum_product_retention_ratio", 0.7)),
            )

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        old_products = previous_state.get("products", {})
        if not isinstance(old_products, dict):
            old_products = {}
        for product in products:
            old = old_products.get(product.key, {})
            old_metadata = old.get("metadata", {}) if isinstance(old, dict) else {}
            first_seen = (
                old_metadata.get("first_seen_at")
                if isinstance(old_metadata, dict)
                else None
            )
            product.metadata["first_seen_at"] = first_seen or now

    def additional_changes(
        self, products: List[Product], previous_state: Dict[str, Any]
    ) -> List[Change]:
        if not previous_state.get("initialized"):
            return []
        old_regions = previous_state.get("catalog_regions", {})
        if not isinstance(old_regions, dict):
            old_regions = {}
        changes: List[Change] = self._lowest_tier_changes(
            previous_state.get("lowest_tiers")
        )
        if self.focus_regions_only:
            return changes
        for region_id in sorted(set(self._catalog_regions) - set(old_regions)):
            region = self._catalog_regions[region_id]
            changes.append(
                Change(
                    ChangeType.NEW,
                    Product(
                        provider=self.name,
                        product_id=region_id,
                        name=region["name"],
                        category="Region",
                        region=region.get("country"),
                        metadata={
                            "catalog_event": "region",
                            "country_code": region.get("country_code"),
                        },
                    ),
                )
            )
        return changes

    def update_state_after_success(
        self, provider_state: Dict[str, Any], products: List[Product]
    ) -> None:
        provider_state["catalog_regions"] = dict(self._catalog_regions)
        provider_state["catalog_stats"] = dict(self.last_scan_stats)
        if self.lowest_tier_only:
            provider_state["lowest_tiers"] = dict(self._lowest_tiers)
        else:
            provider_state.pop("lowest_tiers", None)

    def _lowest_tier_changes(self, old_tiers: Any) -> List[Change]:
        # No saved tiers yet (first scan, or the mode was just switched on):
        # take the current picks as the baseline without alerting.
        if not self.lowest_tier_only or not isinstance(old_tiers, dict):
            return []
        changes: List[Change] = []
        for group in sorted(set(old_tiers) | set(self._lowest_tiers)):
            old = old_tiers.get(group)
            new = self._lowest_tiers.get(group)
            if (old or {}).get("plan_key") == (new or {}).get("plan_key"):
                continue
            shown = new or old
            changes.append(
                Change(
                    ChangeType.DETAILS,
                    Product(
                        provider=self.name,
                        product_id="lowest:%s" % group,
                        name=shown.get("name") or group,
                        category=shown.get("region_label"),
                        region=shown.get("region"),
                        url=shown.get("url"),
                        metadata={
                            "catalog_event": "lowest_tier",
                            "routing_profile": shown.get("routing_profile"),
                            "old_tier": old,
                            "new_tier": new,
                        },
                    ),
                    fields=["lowest_tier"],
                )
            )
        return changes

    def should_notify(self, change: Change) -> bool:
        if change.product.metadata.get("catalog_event") == "region":
            return change.type == ChangeType.NEW
        if change.product.metadata.get("catalog_event") == "lowest_tier":
            return True
        if change.product.metadata.get("catalog_only"):
            return change.type == ChangeType.NEW and not self.focus_regions_only
        return super().should_notify(change)

    def _parse_regions(self, payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, list) or not payload:
            raise ParseError("Misaka regions response is not a non-empty list")
        result: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        catalog: Dict[str, Dict[str, Any]] = {}
        for index, value in enumerate(payload):
            if not isinstance(value, dict):
                raise ParseError("Misaka region %d is not an object" % index)
            region_id = self._required_text(value, "id", "region %d" % index)
            slug = self._required_text(value, "slug", "region %s" % region_id)
            name = self._required_text(value, "name", "region %s" % region_id)
            country_code = self._required_text(
                value, "country_code", "region %s" % region_id
            ).upper()
            if slug in seen:
                raise ParseError("Misaka contains duplicate region slug %s" % slug)
            seen.add(slug)
            region = dict(value)
            region.update(
                {
                    "id": region_id,
                    "slug": slug,
                    "name": name,
                    "country_code": country_code,
                }
            )
            result.append(region)
            catalog[slug] = {
                "id": region_id,
                "slug": slug,
                "name": name,
                "country_code": country_code,
                "country": value.get("country"),
                "facility": value.get("facility"),
                "type": value.get("type"),
            }
        self._catalog_regions = catalog
        return result

    def _parse_plans(
        self, payload: Any, region: Dict[str, Any]
    ) -> List[Product]:
        if not isinstance(payload, list):
            raise ParseError(
                "Misaka plans response for %s is not a list" % region["slug"]
            )
        products: List[Product] = []
        ranks: List[Tuple[float, float, float, str]] = []
        seen: Set[str] = set()
        focused = self._focus_entry(region) is not None
        for index, value in enumerate(payload):
            if not isinstance(value, dict):
                raise ParseError(
                    "Misaka plan %s/%d is not an object"
                    % (region["slug"], index)
                )
            plan_id = self._stable_id(value.get("id"), region["slug"], index)
            if plan_id in seen:
                raise ParseError(
                    "Misaka region %s contains duplicate plan ID %s"
                    % (region["slug"], plan_id)
                )
            seen.add(plan_id)
            slug = self._required_text(
                value, "slug", "plan %s/%s" % (region["slug"], plan_id)
            )
            name = self._required_text(
                value, "name", "plan %s/%s" % (region["slug"], plan_id)
            )
            available = value.get("available")
            if not isinstance(available, bool):
                raise ParseError(
                    "Misaka plan %s/%s has no boolean available"
                    % (region["slug"], plan_id)
                )
            pricing = self._pricing(value, region["slug"], plan_id)
            display_region = self._region_label(region)
            product_id = "%s:%s" % (region["slug"], plan_id)
            stock = self._optional_stock(value.get("stock"))
            if stock is not None and available != (stock > 0):
                raise ParseError(
                    "Misaka plan %s/%s has conflicting stock and availability"
                    % (region["slug"], plan_id)
                )
            products.append(
                Product(
                    provider=self.name,
                    product_id=product_id,
                    name=name,
                    category=display_region,
                    region="%s (%s)" % (region["name"], region["slug"]),
                    price=pricing.get("monthly"),
                    billing_cycle="monthly",
                    stock=stock if focused else None,
                    available=available if focused else None,
                    url="%s/%s/%s"
                    % (
                        str(self.config.get("buy_base", self.BUY_BASE)).rstrip("/"),
                        quote(region["slug"], safe="-._~"),
                        quote(slug, safe="-._~"),
                    ),
                    specs=self._specs(value),
                    metadata={
                        "discovery": {
                            "type": "catalog",
                            "hidden": False,
                            "source": "public_api",
                        },
                        "focused": focused,
                        "catalog_only": not focused,
                        "catalog_available": available,
                        "unavailable_reason": value.get("unavailable_reason"),
                        "region_id": region["id"],
                        "region_slug": region["slug"],
                        "region_name": region["name"],
                        "country_code": region["country_code"],
                        "country": region.get("country"),
                        "facility": region.get("facility"),
                        "plan_id": value.get("id"),
                        "plan_slug": slug,
                        "currency": "USD",
                        "routing_profile": value.get("routing_profile"),
                        "network_billing_model": value.get("network_billing_model"),
                        "tags": value.get("tags") if isinstance(value.get("tags"), list) else [],
                        "sort_order": self._focus_sort_order(region),
                    },
                    pricing=pricing,
                    original_price=self._money(value.get("original_price_monthly")),
                    sale_price=self._money(value.get("sale_price_monthly")),
                    discount_amount=self._money(value.get("discount_amount")),
                    discount_percentage=self._percentage(
                        value.get("discount_percentage", value.get("discount_percent"))
                    ),
                    promotion=self._promotion(value.get("promotion")),
                )
            )
            monthly = value.get("sale_price_monthly")
            if not isinstance(monthly, (int, float)) or isinstance(monthly, bool):
                monthly = value["price_monthly"]
            ranks.append(
                (
                    float(monthly),
                    float(value.get("vcores") or 0),
                    float(value.get("memory") or 0),
                    plan_id,
                )
            )
        if focused and self.lowest_tier_only:
            self._keep_lowest_tiers(products, ranks, region)
        return products

    def _keep_lowest_tiers(
        self,
        products: List[Product],
        ranks: List[Tuple[float, float, float, str]],
        region: Dict[str, Any],
    ) -> None:
        """Keep stock only on the cheapest plan of each routing profile."""
        cheapest: Dict[str, int] = {}
        for index, product in enumerate(products):
            profile = str(product.metadata.get("routing_profile") or "-")
            best = cheapest.get(profile)
            if best is None or ranks[index] < ranks[best]:
                cheapest[profile] = index
        keep = set(cheapest.values())
        for index, product in enumerate(products):
            product.metadata["lowest_tier"] = index in keep
            if index in keep:
                continue
            product.available = None
            product.stock = None
            product.metadata["focused"] = False
            product.metadata["catalog_only"] = True
        for profile, index in cheapest.items():
            product = products[index]
            self._lowest_tiers["%s:%s" % (region["slug"], profile)] = {
                "plan_key": product.key,
                "name": product.name,
                "price": product.price,
                "available": product.available,
                "url": product.url,
                "region": product.region,
                "region_label": product.category,
                "routing_profile": product.metadata.get("routing_profile"),
            }

    def _pricing(
        self, value: Dict[str, Any], region_slug: str, plan_id: str
    ) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for source, cycle in (
            ("price_monthly", "monthly"),
            ("price_semiannual", "semiannual"),
            ("price_annual", "annual"),
        ):
            price = self._money(value.get(source))
            if price is None:
                raise ParseError(
                    "Misaka plan %s/%s has no numeric %s"
                    % (region_slug, plan_id, source)
                )
            result[cycle] = price
        sale = self._money(value.get("sale_price_monthly"))
        if sale is not None:
            result["monthly"] = sale
        return result

    @staticmethod
    def _specs(value: Dict[str, Any]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if isinstance(value.get("vcores"), (int, float)) and not isinstance(
            value.get("vcores"), bool
        ):
            result["cpu"] = "%s vCPU" % value["vcores"]
        for source, target in (("memory", "ram"), ("disk", "disk")):
            raw = value.get(source)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                result[target] = _mib(raw)
        transfer = value.get("transfer")
        if isinstance(transfer, (int, float)) and not isinstance(transfer, bool):
            result["traffic"] = "%s / month" % _mib(transfer)
        routing = value.get("routing_profile")
        if routing:
            result["bandwidth"] = str(routing)
        return result

    def _focus_entry(self, region: Dict[str, Any]) -> Optional[str]:
        for key in (region["slug"].upper(), region["country_code"]):
            if key in self.focus_regions:
                return key
        return None

    def _region_label(self, region: Dict[str, Any]) -> str:
        return (
            self.focus_region_labels.get(region["slug"].upper())
            or self.focus_region_labels.get(region["country_code"])
            or region["name"]
        )

    def _focus_sort_order(self, region: Dict[str, Any]) -> int:
        entry = self._focus_entry(region)
        if entry is None:
            return len(self.focus_regions) + 1
        return self.focus_regions.index(entry)

    def _get_json(self, url: str) -> Any:
        attempts = max(1, int(self.config.get("retry_attempts", 2)))
        backoff = max(0.0, float(self.config.get("retry_backoff_seconds", 0.5)))
        for attempt in range(attempts):
            self._request_count += 1
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                raise FetchError(
                    "Misaka request failed for %s (%s)"
                    % (url, type(exc).__name__)
                ) from None
            if response.status_code >= 400 or is_challenge_page(response.text):
                if attempt + 1 < attempts and response.status_code >= 500:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                raise FetchError(
                    "Misaka HTTP %s for %s" % (response.status_code, url),
                    response.status_code,
                    response.status_code in (403, 429),
                )
            try:
                return response.json()
            except ValueError:
                raise ParseError("Misaka returned invalid JSON for %s" % url) from None
        raise FetchError("Misaka request failed for %s" % url)

    @staticmethod
    def _required_text(value: Dict[str, Any], field: str, label: str) -> str:
        result = str(value.get(field) or "").strip()
        if not result:
            raise ParseError("Misaka %s has no %s" % (label, field))
        return result

    @staticmethod
    def _stable_id(value: Any, region_slug: str, index: int) -> str:
        if value is None or isinstance(value, bool):
            raise ParseError(
                "Misaka plan %s/%d has no stable ID" % (region_slug, index)
            )
        result = str(value).strip()
        if not result:
            raise ParseError(
                "Misaka plan %s/%d has an empty stable ID" % (region_slug, index)
            )
        return result

    @staticmethod
    def _money(value: Any) -> Optional[str]:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return "$%.2f USD" % float(value)

    @staticmethod
    def _percentage(value: Any) -> Optional[str]:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return "%s%%" % ("%g" % float(value))

    @staticmethod
    def _optional_stock(value: Any) -> Optional[int]:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ParseError("Misaka stock must be a non-negative integer")
        return value

    @staticmethod
    def _promotion(value: Any) -> Optional[str]:
        if value is None or value == "" or value == {}:
            return None
        if isinstance(value, str):
            return value.strip() or None
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _reject_large_drop(
        label: str, previous: int, current: int, retention_ratio: float
    ) -> None:
        if previous > 0 and current < previous * retention_ratio:
            raise ParseError(
                "Misaka incomplete catalog: %s dropped from %d to %d"
                % (label, previous, current)
            )


def _mib(value: float) -> str:
    amount = float(value)
    if amount >= 1024 and amount % 1024 == 0:
        return "%g GB" % (amount / 1024)
    return "%g MB" % amount
