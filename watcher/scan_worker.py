from __future__ import annotations

import json
import sys
from typing import Any, Dict

from .providers import build_provider


def main() -> int:
    try:
        config = json.load(sys.stdin)
        if not isinstance(config, dict):
            raise ValueError("provider config must be an object")
        products = build_provider(config).fetch_products()
        result: Dict[str, Any] = {
            "ok": True,
            "products": [product.to_dict() for product in products],
        }
        code = 0
    except Exception as exc:
        result = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc).replace("\n", " ")[:1000],
        }
        code = 1
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
