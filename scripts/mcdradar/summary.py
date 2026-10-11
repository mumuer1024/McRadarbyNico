"""最终订单摘要与一致性校验。

交互设计 4.3 要求：
- 展示商品、套餐子项、门店、优惠券、最终应付金额
- 确认内容与即将提交的订单请求必须完全一致
- 不允许确认后静默替换商品、优惠券或门店

实现：把「订单请求」规范化后算 sha256 摘要（digest）。
摘要同时写入摘要文本与本地状态，提交前比对，不一致即拒绝提交。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .models import FinalSummary
from .money import Fen, fen_to_yuan_str

__all__ = [
    "canonical_json",
    "digest_input",
    "is_internal_key",
    "request_digest",
    "build_summary",
    "render_summary",
    "verify_confirmation",
]

# ---------------------------------------------------------------------------
# 指纹策略：**黑名单**而非白名单。
#
# 早期实现维护了一个"参与校验字段"白名单（storeCode/orderType/.../items），
# 结果遗漏了 remark / withOrder / gmServiceCode —— 这三个都会真实影响订单
# 内容或价格：
#   · remark     外送备注，会随订单提交给门店
#   · withOrder  随单购权益，直接影响商品与成交价
#   · gmServiceCode  团餐服务编码，决定履约方式
# 白名单每加一个 MCP 字段就要手工同步一次，迟早再漏。
# 因此改为：规范化后的**完整下单请求**参与指纹，只排除明确的内部跟踪字段。
# ---------------------------------------------------------------------------

# 仅用于内部跟踪、不参与订单内容的元数据（不会送达到服务端业务语义）
_INTERNAL_KEY_HINTS = (
    "requestid", "traceid", "clientrequestid", "spanid", "timestamp",
    "nonce", "idempotencykey",
)


def is_internal_key(k: str) -> bool:
    """内部跟踪字段判定：下划线前缀（如 _est_payable_fen）或已知跟踪键。"""
    lk = str(k).lower()
    return lk.startswith("_") or any(h in lk for h in _INTERNAL_KEY_HINTS)


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_input(request: dict) -> dict:
    """参与指纹计算的字段集合（供测试与排障查看）。"""
    return {
        k: v for k, v in (request or {}).items()
        if not is_internal_key(k) and v not in (None, "", [], {})
    }


def request_digest(request: dict) -> str:
    """订单请求指纹：规范化后的完整下单请求（排除内部跟踪字段）。

    任何会影响订单内容 / 价格 / 优惠 / 门店 / 履约方式的字段都自动纳入，
    无需手工维护字段清单。
    """
    return hashlib.sha256(canonical_json(digest_input(request)).encode("utf-8")).hexdigest()


def build_summary(
    *,
    store_name: str,
    store_code: str,
    be_type: int,
    order_type: int,
    take_way_title: str,
    items: list[dict],
    coupon_title: str,
    pricing: dict,
    pricing_at: str,
    warnings: list[str] | None = None,
) -> FinalSummary:
    d = pricing.get("data") if isinstance(pricing, dict) else None
    p = d if isinstance(d, dict) else (pricing if isinstance(pricing, dict) else {})
    return FinalSummary(
        store_name=store_name,
        store_code=store_code,
        be_type=be_type,
        order_type=order_type,
        take_way_title=take_way_title or "（取餐方式未知）",
        items=items,
        coupon_title=coupon_title,
        original_fen=int(p.get("originalPrice") or 0),
        discount_fen=int(p.get("discount") or 0),
        payable_fen=int(p.get("price") or 0),
        pricing_at=pricing_at,
        warnings=list(warnings or []),
        unknown_fields=_unknown_fields(p),
    )


def _unknown_fields(p: dict) -> list[str]:
    missing = []
    for k in ("price", "originalPrice", "discount"):
        if p.get(k) is None:
            missing.append(k)
    if not p.get("takeWayList"):
        missing.append("takeWayList")
    return missing


def render_summary(s: FinalSummary) -> str:
    lines = [
        "## 🧾 最终订单摘要（请核对）",
        "",
        f"- **门店**：{s.store_name}（`{s.store_code}`）",
        f"- **就餐方式**：beType={s.be_type} / orderType={s.order_type}",
        f"- **取餐方式**：{s.take_way_title}",
        f"- **优惠券**：{s.coupon_title or '不使用优惠券'}",
        "",
        "| 商品 | 数量 |",
        "|---|---|",
    ]
    for it in s.items:
        sub = it.get("comboItemList") or []
        name = it.get("name") or it.get("productCode")
        lines.append(f"| {name} | {it.get('quantity', 1)} |")
        for c in sub:
            lines.append(f"| &nbsp;&nbsp;↳ {c.get('name') or c.get('code')} | {c.get('quantity', 1)} |")
    lines += [
        "",
        f"- **原价**：¥{fen_to_yuan_str(s.original_fen)}",
        f"- **优惠**：-¥{fen_to_yuan_str(s.discount_fen)}",
        f"- **应付**：**¥{fen_to_yuan_str(s.payable_fen)}**",
        f"- **核价时间**：{s.pricing_at}",
        "",
    ]
    if s.unknown_fields:
        lines.append(f"> ⚠️ 服务端未返回字段：{', '.join(s.unknown_fields)}，相关数值无法确认。")
    for w in s.warnings:
        lines.append(f"> ⚠️ {w}")
    lines.append("")
    lines.append("确认后将按以上内容原样提交，**不会再更换商品、门店或优惠券**。")
    return "\n".join(lines)


def verify_confirmation(expected_digest: str, actual_request: dict) -> dict:
    """提交前校验：实际请求与用户确认时的请求是否一致。"""
    actual = request_digest(actual_request)
    ok = actual == expected_digest
    return {
        "ok": ok,
        "expected": expected_digest,
        "actual": actual,
        "message": "一致，可以提交" if ok else "❌ 请求已变更，必须重新向用户展示摘要并重新确认",
    }
