from __future__ import annotations

from typing import Any, List, Optional


def select(root: Any, selector: str, attribute: Optional[str] = None) -> List[Any]:
    nodes = root.select(selector)
    if attribute:
        return [node.get(attribute) for node in nodes if node.get(attribute) is not None]
    return [node.get_text(" ", strip=True) for node in nodes]
