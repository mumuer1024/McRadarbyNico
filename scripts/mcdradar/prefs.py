"""口味偏好、不喜欢的餐品、辣度过滤。

红线：
- 只做「不喜欢的餐品/食材」与「是否吃辣」这类简单偏好
- **不做过敏原识别，不承诺过敏安全**
- 辣度无法判断时返回 UNKNOWN，保留该商品但标记，输出时必须声明
  「无法保证完全不辣」

三态辣度：SPICY / NOT_SPICY / UNKNOWN
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import MenuItem

__all__ = [
    "SPICY", "NOT_SPICY", "UNKNOWN",
    "Preferences",
    "spiciness",
    "filter_items",
    "load_prefs",
    "save_prefs",
]

SPICY = "spicy"
NOT_SPICY = "not_spicy"
UNKNOWN = "unknown"

# 辣度判定词表（可维护；命中即判辣）
SPICY_KEYWORDS = (
    "辣", "川香", "韩式", "麻辣", "香辣", "劲辣", "爆辣", "藤椒", "花椒",
    "麦辣", "辣翅", "辣腿", "川味", "剁椒", "辣椒",
)
# 明确不辣的锚点词（用于抵消误判，如"不辣"）
NOT_SPICY_KEYWORDS = ("不辣", "原味", "无辣")

# 默认忌口分类（用户可增删）
DEFAULT_EXCLUDE_CATEGORIES: tuple[str, ...] = ()


@dataclass
class Preferences:
    exclude_categories: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)   # 不喜欢的餐品/食材
    dislike_codes: list[str] = field(default_factory=list)      # 精确到商品编码
    no_spicy: bool = False
    allow_unknown_spicy: bool = True   # True：未知辣度保留但标记

    def to_dict(self) -> dict:
        return {
            "exclude_categories": list(self.exclude_categories),
            "exclude_keywords": list(self.exclude_keywords),
            "dislike_codes": list(self.dislike_codes),
            "no_spicy": self.no_spicy,
            "allow_unknown_spicy": self.allow_unknown_spicy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Preferences":
        return cls(
            exclude_categories=list(d.get("exclude_categories") or []),
            exclude_keywords=list(d.get("exclude_keywords") or []),
            dislike_codes=list(d.get("dislike_codes") or []),
            no_spicy=bool(d.get("no_spicy")),
            allow_unknown_spicy=bool(d.get("allow_unknown_spicy", True)),
        )


def spiciness(item: MenuItem) -> str:
    """返回 SPICY / NOT_SPICY / UNKNOWN。"""
    haystack = " ".join([item.name, item.category, " ".join(item.tags)]).lower()
    if any(k in haystack for k in NOT_SPICY_KEYWORDS):
        # "不辣" 等显式锚点优先
        if "辣" in haystack and "不辣" in haystack:
            return NOT_SPICY
    for k in SPICY_KEYWORDS:
        if k in haystack:
            return SPICY
    return UNKNOWN


def filter_items(items: Iterable[MenuItem], prefs: Preferences) -> tuple[list[MenuItem], dict]:
    """按偏好过滤。返回 (保留列表, 诊断信息)。"""
    kept: list[MenuItem] = []
    diag = {
        "excluded_by_category": [],
        "excluded_by_keyword": [],
        "excluded_by_code": [],
        "excluded_by_spicy": [],
        "kept_unknown_spicy": [],
        "spicy_unknown_note": False,
    }

    for it in items:
        if it.category and it.category in prefs.exclude_categories:
            diag["excluded_by_category"].append(it.name)
            continue
        if it.code in prefs.dislike_codes:
            diag["excluded_by_code"].append(it.name)
            continue
        hay = f"{it.name} {it.category} {' '.join(it.tags)}".lower()
        hit = [k for k in prefs.exclude_keywords if str(k).lower() in hay]
        if hit:
            diag["excluded_by_keyword"].append({"name": it.name, "hit": hit})
            continue
        if prefs.no_spicy:
            level = spiciness(it)
            if level == SPICY:
                diag["excluded_by_spicy"].append(it.name)
                continue
            if level == UNKNOWN:
                if not prefs.allow_unknown_spicy:
                    diag["excluded_by_spicy"].append(it.name)
                    continue
                diag["kept_unknown_spicy"].append(it.name)
                diag["spicy_unknown_note"] = True
        kept.append(it)

    return kept, diag


def load_prefs(path: Path) -> Preferences:
    if not Path(path).exists():
        return Preferences()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Preferences.from_dict(json.load(f))
    except (json.JSONDecodeError, OSError):
        return Preferences()


def save_prefs(path: Path, prefs: Preferences) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs.to_dict(), f, ensure_ascii=False, indent=2)
