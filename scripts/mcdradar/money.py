"""金额处理：元 / 分换算与精度保证。

P0-D 约束：
- query-meals 的 currentPrice / originalPrice 单位是「元」（字符串）
- calculate-price 的所有金额单位是「分」（整数）
- 一律用「整数分」参与运算，杜绝浮点误差

对外只暴露 fen（int）为计算单位，展示时才转元。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    "yuan_to_fen",
    "fen_to_yuan",
    "fen_to_yuan_str",
    "safe_yuan_to_fen",
    "parse_diff_price_fen",
    "Fen",
]

Fen = int  # 金额计算单位：分

_YUAN_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        # 先经 str，避免 float 二进制误差
        return Decimal(str(value))
    if value is None:
        raise InvalidOperation("price is None")
    text = str(value).strip()
    text = text.replace("¥", "").replace("￥", "").replace(",", "").strip()
    m = _YUAN_RE.search(text)
    if not m:
        raise InvalidOperation(f"无法解析金额: {value!r}")
    return Decimal(m.group(0))


def yuan_to_fen(value) -> Fen:
    """元 → 分。输入可以是 "26.5" / "26.50" / 26.5 / "¥26.5" / "26"。"""
    if value is None or value == "":
        raise ValueError(f"金额为空: {value!r}")
    dec = _to_decimal(value) * Decimal(100)
    return int(dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def safe_yuan_to_fen(value, default: Fen | None = None) -> Fen | None:
    """解析失败返回 default，不抛异常。用于菜单里偶尔缺失的价格字段。"""
    try:
        return yuan_to_fen(value)
    except (ValueError, InvalidOperation, ArithmeticError):
        return default


def fen_to_yuan(fen: Fen) -> Decimal:
    """分 → 元（Decimal，保留 2 位）。"""
    return (Decimal(int(fen)) / Decimal(100)).quantize(Decimal("0.01"))


def fen_to_yuan_str(fen: Fen) -> str:
    """分 → 展示字符串，如 2650 → "26.50"。"""
    return f"{fen_to_yuan(fen):.2f}"


def parse_diff_price_fen(text) -> Fen | None:
    """解析 query-meal-detail 的 diffPrice 字符串。

    实测形态："+ ¥0" / "+ ¥2.5" / "- ¥0.5"
    返回带符号的分值；无法解析返回 None。
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    negative = s.startswith("-")
    try:
        mag = yuan_to_fen(s.lstrip("+-").strip())
    except (ValueError, InvalidOperation, ArithmeticError):
        return None
    return -mag if negative else mag
