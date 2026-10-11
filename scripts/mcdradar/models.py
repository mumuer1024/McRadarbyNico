"""共享数据模型。

设计原则：所有结构都能 from_dict(真实 MCP 返回) / to_dict(可落盘、可喂回 MCP)。
只保留业务必需字段，敏感字段一律不进模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .money import Fen, safe_yuan_to_fen

__all__ = [
    "MenuItem",
    "StoreInfo",
    "CouponInfo",
    "ComboItem",
    "Combo",
    "OrderContext",
    "PricingResult",
    "FinalSummary",
]


def _as_dict(o: Any) -> dict:
    return o if isinstance(o, dict) else {}


@dataclass
class MenuItem:
    code: str
    name: str
    category: str = ""
    tags: list[str] = field(default_factory=list)
    current_price_fen: Fen | None = None       # 现价（元 → 分）
    original_price_fen: Fen | None = None     # 原价（元 → 分）
    discount_type: str | None = None
    can_with_order: bool = False
    with_order: dict | None = None
    image: str = ""

    @property
    def effective_price_fen(self) -> Fen | None:
        """用于候选筛选的价格。

        ⚠️ P0-D：canWithOrder=true 的 currentPrice 可能依赖随单购麦金卡，
        筛选时可用，但**不得作为最终应付金额承诺**。
        """
        return self.current_price_fen if self.current_price_fen is not None else self.original_price_fen

    @property
    def price_is_conditional(self) -> bool:
        """价格是否依赖随单购会员卡等附加条件。"""
        return self.can_with_order and self.discount_type is not None and "随单购" in str(self.discount_type)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StoreInfo:
    store_code: str
    store_name: str
    be_type: int
    be_code: str = ""
    order_type: int = 1
    address: str = ""

    @property
    def context(self) -> dict:
        ctx = {"storeCode": self.store_code, "orderType": self.order_type, "beType": self.be_type}
        if self.be_type != 1 and self.be_code:
            ctx["beCode"] = self.be_code
        return ctx

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CouponInfo:
    title: str
    coupon_id: str = ""
    coupon_code: str = ""
    valid_from: str = ""
    valid_to: str = ""
    product_codes: list[str] = field(default_factory=list)
    source: str = ""            # my / store / available
    status: str = ""            # 已领取 / 可领取 ...
    labels: list[str] = field(default_factory=list)
    # 唯一性：券池存在同名重复批次，不能用 title 做唯一键
    uid: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ComboItem:
    code: str
    name: str
    quantity: int = 1
    price_fen: Fen | None = None
    round_no: str = ""          # 套餐 round（子项替换用）
    diff_price_fen: Fen | None = None
    modification: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Combo:
    items: list[ComboItem] = field(default_factory=list)
    est_total_fen: Fen = 0
    template: str = ""
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """组合指纹：商品编码 + 数量排序拼接。用于同轮去重。"""
        parts = sorted(f"{i.code}x{i.quantity}" for i in self.items)
        return "|".join(parts)

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "est_total_fen": self.est_total_fen,
            "template": self.template,
            "score": self.score,
            "notes": list(self.notes),
            "fingerprint": self.fingerprint,
        }


@dataclass
class OrderContext:
    store: StoreInfo
    take_way_code: str = ""
    take_way_title: str = ""
    address_id: str = ""
    reservation_date: str = ""
    need_tableware: bool | None = None
    remark: str = ""
    with_order: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PricingResult:
    """calculate-price 返回的关键字段（金额单位已统一为分）。"""
    product_original_fen: Fen = 0
    product_price_fen: Fen = 0
    packing_fen: Fen = 0
    delivery_fen: Fen = 0
    tableware_fen: Fen = 0
    original_fen: Fen = 0
    discount_fen: Fen = 0
    payable_fen: Fen = 0
    take_way_list: list[dict] = field(default_factory=list)
    product_list: list[dict] = field(default_factory=list)
    enjoyed: dict = field(default_factory=dict)
    enjoyable: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_mcp(cls, data: dict) -> "PricingResult":
        d = _as_dict(data)
        g = lambda k: int(d.get(k) or 0)
        return cls(
            product_original_fen=g("productOriginalPrice"),
            product_price_fen=g("productPrice"),
            packing_fen=g("packingPrice"),
            delivery_fen=g("deliveryPrice"),
            tableware_fen=g("tablewarePrice"),
            original_fen=g("originalPrice"),
            discount_fen=g("discount"),
            payable_fen=g("price"),
            take_way_list=d.get("takeWayList") or [],
            product_list=d.get("productList") or [],
            enjoyed=d.get("enjoyed") or {},
            enjoyable=d.get("enjoyable") or {},
            raw=d,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class FinalSummary:
    """最终订单摘要：展示给用户并作为确认依据。"""
    store_name: str
    store_code: str
    be_type: int
    order_type: int
    take_way_title: str
    items: list[dict]
    coupon_title: str
    original_fen: Fen
    discount_fen: Fen
    payable_fen: Fen
    pricing_at: str
    warnings: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
