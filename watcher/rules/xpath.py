from __future__ import annotations

from typing import Any, List


def select(root: Any, expression: str) -> List[Any]:
    nodes = root.xpath(expression)
    values: List[Any] = []
    for node in nodes:
        if isinstance(node, (str, int, float, bool)):
            values.append(node)
        else:
            values.append(" ".join(node.itertext()).strip())
    return values
