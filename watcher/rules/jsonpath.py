from __future__ import annotations

from typing import Any, List


def select(root: Any, expression: str) -> List[Any]:
    try:
        from jsonpath_ng.ext import parse
    except ImportError:  # Debian's older package name; kept for Python 3.9 test hosts.
        from jsonpath_rw_ext import parse

    return [match.value for match in parse(expression).find(root)]
