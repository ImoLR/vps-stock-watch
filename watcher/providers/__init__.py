from __future__ import annotations

from typing import Any, Dict

from .base import BaseProvider
from .blossom import BlossomProvider
from .boilcloud import BoilcloudProvider
from .boilcloud_sale import BoilcloudSaleProvider
from .cloudiplc import CloudiplcOrderCheck, CloudiplcProvider
from .dmit import DmitProvider
from .fachost import FachostProvider
from .fachost_sale import FachostSaleProvider
from .generic import GenericProvider
from .highend import HighendProvider
from .leikwanhost import LeikwanhostProvider
from .liqunhuiju import LiqunHuijuProvider
from .misaka import MisakaProvider
from .nexkr import NexKrProvider
from .nnb import NnbProvider
from .tcscloud import TcscloudProvider
from .tgchannel import TgChannelProvider
from .vmsilo import VmsiloProvider


def build_provider(config: Dict[str, Any]) -> BaseProvider:
    provider_type = str(config.get("type", config.get("name", ""))).lower()
    classes = {
        "blossom": BlossomProvider,
        "boilcloud": BoilcloudProvider,
        "boilcloud_sale": BoilcloudSaleProvider,
        "cloudiplc": CloudiplcProvider,
        # Internal: CloudIPLC order pages, run only inside an isolated scan.
        "cloudiplc_orders": CloudiplcOrderCheck,
        "nexkr": NexKrProvider,
        "nnb": NnbProvider,
        "dmit": DmitProvider,
        "fachost": FachostProvider,
        "fachost_sale": FachostSaleProvider,
        "highend": HighendProvider,
        "leikwanhost": LeikwanhostProvider,
        "liqunhuiju": LiqunHuijuProvider,
        "misaka": MisakaProvider,
        "tcscloud": TcscloudProvider,
        "tg_channel": TgChannelProvider,
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
    "BoilcloudSaleProvider",
    "CloudiplcProvider",
    "FachostProvider",
    "FachostSaleProvider",
    "HighendProvider",
    "LeikwanhostProvider",
    "LiqunHuijuProvider",
    "MisakaProvider",
    "NnbProvider",
    "TcscloudProvider",
    "TgChannelProvider",
    "VmsiloProvider",
    "build_provider",
]
