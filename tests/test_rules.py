from __future__ import annotations

import unittest

from bs4 import BeautifulSoup
from lxml import html

from watcher.rules import extract_all, extract_one


class RuleTests(unittest.TestCase):
    def test_css(self):
        root = BeautifulSoup('<div><a class="buy" href="/p/1"> Plan </a></div>', "lxml")
        self.assertEqual(extract_one(root, {"type": "css", "selector": ".buy"}), "Plan")
        self.assertEqual(
            extract_one(root, {"type": "css", "selector": ".buy", "attribute": "href"}),
            "/p/1",
        )

    def test_xpath(self):
        root = html.fromstring('<article><span class="price">$10</span></article>')
        self.assertEqual(
            extract_one(root, {"type": "xpath", "expression": ".//span/text()"}), "$10"
        )

    def test_regex(self):
        self.assertEqual(
            extract_one(
                "Stock: 12",
                {"type": "regex", "pattern": r"Stock:\s*(?P<value>\d+)", "transforms": ["int"]},
            ),
            12,
        )

    def test_jsonpath(self):
        self.assertEqual(
            extract_all({"products": [{"id": 1}, {"id": 2}]}, {"type": "jsonpath", "expression": "$.products[*].id"}),
            [1, 2],
        )


if __name__ == "__main__":
    unittest.main()
