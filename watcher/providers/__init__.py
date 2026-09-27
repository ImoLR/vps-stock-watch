from __future__ import annotations

from typing import Any, Dict

from .base import BaseProvider
from .blossom import BlossomProvider
from .boilcloud import BoilcloudProvider
from .dmit import DmitProvider
from .fachost import FachostProvider
from .generic import GenericProvider
from .leikwanhost import LeikwanhostProvider
from .nexkr import NexKrProvider


def build_provider(config: Dict[str, Any]) -> BaseProvider:
    provider_type = str(config.get("type", config.get("name", ""))).lower()
    classes = {
        "blossom": BlossomProvider,
        "boilcloud": BoilcloudProvider,
        "nexkr": NexKrProvider,
        "dmit": DmitProvider,
        "fachost": FachostProvider,
        "leikwanhost": LeikwanhostProvider,
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
    "build_provider",
]
