from __future__ import annotations

from typing import Any, Dict

from .base import BaseProvider
from .blossom import BlossomProvider
from .boilcloud import BoilcloudProvider
from .cloudiplc import CloudiplcOrderCheck, CloudiplcProvider
from .dmit import DmitProvider
from .fachost import FachostProvider
from .generic import GenericProvider
from .highend import HighendProvider
from .leikwanhost import LeikwanhostProvider
from .liqunhuiju import LiqunHuijuProvider
from .misaka import MisakaProvider
from .nexkr import NexKrProvider
from .nnb import NnbProvider
from .vmsilo import VmsiloProvider


def build_provider(config: Dict[str, Any]) -> BaseProvider:
    provider_type = str(config.get("type", config.get("name", ""))).lower()
    classes = {
        "blossom": BlossomProvider,
        "boilcloud": BoilcloudProvider,
        "cloudiplc": CloudiplcProvider,
        # Internal: CloudIPLC order pages, run only inside an isolated scan.
        "cloudiplc_orders": CloudiplcOrderCheck,
        "nexkr": NexKrProvider,
        "nnb": NnbProvider,
        "dmit": DmitProvider,
        "fachost": FachostProvider,
        "highend": HighendProvider,
        "leikwanhost": LeikwanhostProvider,
        "liqunhuiju": LiqunHuijuProvider,
        "misaka": MisakaProvider,
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
    "CloudiplcProvider",
    "FachostProvider",
    "HighendProvider",
    "LeikwanhostProvider",
    "LiqunHuijuProvider",
    "MisakaProvider",
    "NnbProvider",
    "VmsiloProvider",
    "build_provider",
]
