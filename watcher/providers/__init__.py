from __future__ import annotations

from typing import Any, Dict

from .base import BaseProvider
from .blossom import BlossomProvider
from .boilcloud import BoilcloudProvider
from .dmit import DmitProvider
from .fachost import FachostProvider
from .generic import GenericProvider
from .leikwanhost import LeikwanhostProvider
from .liqunhuiju import LiqunHuijuProvider
from .nexkr import NexKrProvider
from .vmsilo import VmsiloProvider


def build_provider(config: Dict[str, Any]) -> BaseProvider:
    provider_type = str(config.get("type", config.get("name", ""))).lower()
    classes = {
        "blossom": BlossomProvider,
        "boilcloud": BoilcloudProvider,
        "nexkr": NexKrProvider,
        "dmit": DmitProvider,
        "fachost": FachostProvider,
        "leikwanhost": LeikwanhostProvider,
        "liqunhuiju": LiqunHuijuProvider,
        "vmsilo": VmsiloProvider,
        "generic": GenericProvider,
    }
    if provider_type not in classes:
        raise ValueError("unknown provider type: %s" % provider_type)
    return classes[provider_type](config)


__all__ = [
    "BaseProvider",
    "BlossomProvider",
    "BoilcloudProvider",
    "FachostProvider",
    "LeikwanhostProvider",
    "LiqunHuijuProvider",
    "VmsiloProvider",
    "build_provider",
]
