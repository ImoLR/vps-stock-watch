from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import css, jsonpath, regex, xpath


class RuleError(ValueError):
    pass


def extract_all(root: Any, rule: Dict[str, Any], raw: Optional[str] = None) -> List[Any]:
    kind = str(rule.get("type", "css")).lower()
    if kind == "css":
        return css.select(root, _required(rule, "selector"), rule.get("attribute"))
    if kind == "xpath":
        return xpath.select(root, _required(rule, "expression"))
    if kind == "regex":
        return regex.select(raw if raw is not None else root, _required(rule, "pattern"), rule.get("group"))
    if kind == "jsonpath":
        return jsonpath.select(root, _required(rule, "expression"))
    raise RuleError("unsupported rule type: %s" % kind)


def extract_one(
    root: Any,
    rule: Dict[str, Any],
    raw: Optional[str] = None,
    default: Any = None,
) -> Any:
    values = extract_all(root, rule, raw)
    if not values:
        return rule.get("default", default)
    value = values[0]
    if isinstance(value, str):
        value = " ".join(value.split())
    transforms = rule.get("transforms", [])
    for transform in transforms:
        if transform == "strip" and isinstance(value, str):
            value = value.strip()
        elif transform == "lower" and isinstance(value, str):
            value = value.lower()
        elif transform == "int":
            value = int(value)
        elif transform == "float":
            value = float(value)
        else:
            raise RuleError("unsupported transform: %s" % transform)
    return value


def _required(rule: Dict[str, Any], name: str) -> Any:
    if name not in rule:
        raise RuleError("%s rule requires %s" % (rule.get("type", "css"), name))
    return rule[name]
