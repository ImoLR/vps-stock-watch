from __future__ import annotations

import unittest

from watcher.diff import compare_products
from watcher.models import ChangeType, Product


def product(product_id="1", **values):
    defaults = dict(provider="test", product_id=product_id, name="Plan", price="$10", stock=None, available=False)
    defaults.update(values)
    return Product(**defaults)


class DiffTests(unittest.TestCase):
    def kinds(self, old, new):
        return [change.type for change in compare_products(old, new)]

    def test_new_sku(self):
        self.assertEqual(self.kinds([product()], [product(), product("2")]), [ChangeType.NEW])

    def test_restock_and_stock_number_are_one_event(self):
        changes = compare_products(
            [product(stock=0, available=False)], [product(stock=3, available=True)]
        )
        self.assertEqual([item.type for item in changes], [ChangeType.RESTOCK])
        self.assertEqual(changes[0].fields, ["available", "stock"])

    def test_sold_out(self):
        self.assertEqual(
            self.kinds([product(available=True)], [product(available=False)]),
            [ChangeType.SOLD_OUT],
        )

    def test_stock_change(self):
        self.assertEqual(
            self.kinds([product(stock=2, available=True)], [product(stock=5, available=True)]),
            [ChangeType.STOCK],
        )

    def test_boolean_only_stock_does_not_invent_number_change(self):
        self.assertEqual(
            self.kinds([product(stock=None, available=True)], [product(stock=None, available=True)]),
            [],
        )

    def test_price_and_name_changes(self):
        self.assertEqual(
            self.kinds([product()], [product(name="New Plan", price="$11")]),
            [ChangeType.PRICE, ChangeType.NAME],
        )

    def test_billing_cycle_change_is_a_price_event(self):
        changes = compare_products(
            [product(billing_cycle="monthly")],
            [product(billing_cycle="quarterly")],
        )
        self.assertEqual([item.type for item in changes], [ChangeType.PRICE])
        self.assertEqual(changes[0].fields, ["billing_cycle"])

    def test_removed(self):
        self.assertEqual(self.kinds([product()], []), [ChangeType.REMOVED])


if __name__ == "__main__":
    unittest.main()
