from __future__ import annotations

from typing import Dict, Iterable, List

from .models import Change, ChangeType, Product


def index_products(products: Iterable[Product]) -> Dict[str, Product]:
    result: Dict[str, Product] = {}
    for product in products:
        if product.key in result:
            raise ValueError("duplicate product key: %s" % product.key)
        result[product.key] = product
    return result


def compare_products(previous: Iterable[Product], current: Iterable[Product]) -> List[Change]:
    """Compare two complete, successful catalog snapshots.

    Stock-number changes are emitted only when both snapshots expose a number.
    This prevents a boolean-only provider from inventing 0/N transitions.
    """
    old_map = index_products(previous)
    new_map = index_products(current)
    changes: List[Change] = []

    for key in sorted(new_map.keys() - old_map.keys()):
        changes.append(Change(ChangeType.NEW, new_map[key]))

    for key in sorted(old_map.keys() & new_map.keys()):
        old, new = old_map[key], new_map[key]
        availability_changed = False
        stock_changed = old.stock is not None and new.stock is not None and old.stock != new.stock
        if old.available is False and new.available is True:
            fields = ["available"] + (["stock"] if stock_changed else [])
            changes.append(Change(ChangeType.RESTOCK, new, old, fields))
            availability_changed = True
        elif old.available is True and new.available is False:
            fields = ["available"] + (["stock"] if stock_changed else [])
            changes.append(Change(ChangeType.SOLD_OUT, new, old, fields))
            availability_changed = True

        if stock_changed and not availability_changed:
            changes.append(Change(ChangeType.STOCK, new, old, ["stock"]))
        price_fields = []
        if old.price != new.price:
            price_fields.append("price")
        if old.billing_cycle != new.billing_cycle:
            price_fields.append("billing_cycle")
        if price_fields:
            changes.append(Change(ChangeType.PRICE, new, old, price_fields))
        if old.name != new.name:
            changes.append(Change(ChangeType.NAME, new, old, ["name"]))

    for key in sorted(old_map.keys() - new_map.keys()):
        changes.append(Change(ChangeType.REMOVED, old_map[key], old_map[key]))
    return changes
