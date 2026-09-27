from __future__ import annotations

import re
from typing import Any, List


def select(root: Any, pattern: str, group: Any = None, flags: int = re.I | re.S) -> List[Any]:
    text = root if isinstance(root, str) else str(root)
    compiled = re.compile(pattern, flags)
    values: List[Any] = []
    for match in compiled.finditer(text):
        if group is not None:
            values.append(match.group(group))
        elif "value" in match.groupdict():
            values.append(match.group("value"))
        elif match.lastindex:
            values.append(match.group(1))
        else:
            values.append(match.group(0))
    return values
