from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


@dataclass
class Product:
    provider: str
    product_id: str
    name: str
    category: Optional[str] = None
    region: Optional[str] = None
    price: Optional[str] = None
    billing_cycle: Optional[str] = None
    stock: Optional[int] = None
    available: Optional[bool] = None
    url: Optional[str] = None
    specs: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    pricing: Dict[str, str] = field(default_factory=dict)
    original_price: Optional[str] = None
    sale_price: Optional[str] = None
    discount_amount: Optional[str] = None
    discount_percentage: Optional[str] = None
    promotion: Optional[str] = None

    @property
    def key(self) -> str:
        return "%s:%s" % (self.provider, self.product_id)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Product":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})


class ChangeType(str, Enum):
    NEW = "new"
    RESTOCK = "restock"
    SOLD_OUT = "sold_out"
    STOCK = "stock"
    PRICE = "price"
    PROMOTION = "promotion"
    NAME = "name"
    REMOVED = "removed"


@dataclass
class Change:
    type: ChangeType
    product: Product
    old: Optional[Product] = None
    fields: List[str] = field(default_factory=list)
