"""菜单规范化（共享事实层）。

输入：query-meals 的原始返回
输出：MenuItem 列表 + 分类索引 + 规范化诊断

P0-D 要点：
- currentPrice / originalPrice 单位是「元」→ 统一转「分」
- canWithOrder=true 的价格可能依赖随单购麦金卡 → 打标，不作承诺
"""

from __future__ import annotations

from typing import Any

from .models import MenuItem
from .money import safe_yuan_to_fen

__all__ = ["normalize_menu", "menu_digest", "MenuSnapshot"]


class MenuSnapshot:
    def __init__(self, items: list[MenuItem], categories: dict[str, list[str]], raw_meta: dict | None = None):
        self.items = items
        self.categories = categories          # 分类名 -> [code]
        self.raw_meta = raw_meta or {}

    def by_code(self, code: str) -> MenuItem | None:
        for it in self.items:
            if it.code == code:
                return it
        return None

    def size(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "categories": self.categories,
            "raw_meta": self.raw_meta,
        }


def normalize_menu(raw: dict) -> MenuSnapshot:
    """把 query-meals 返回规范化。兼容几种可能的嵌套形态。"""
    data = raw.get("data") if isinstance(raw, dict) else None
    data = data if isinstance(data, dict) else (raw if isinstance(raw, dict) else {})

    meals_map: dict[str, dict] = {}
    raw_meals = data.get("meals")
    if isinstance(raw_meals, dict):
        # {code: {...}} 形态（实测为这一种）
        for code, body in raw_meals.items():
            if isinstance(body, dict):
                m = dict(body)
                m.setdefault("code", code)
                meals_map[str(code)] = m
    elif isinstance(raw_meals, list):
        for body in raw_meals:
            if isinstance(body, dict) and body.get("code"):
                meals_map[str(body["code"])] = body

    categories: dict[str, list[str]] = {}
    raw_cats = data.get("categories")
    if isinstance(raw_cats, list):
        for cat in raw_cats:
            if not isinstance(cat, dict):
                continue
            name = str(cat.get("name") or "未分类")
            codes: list[str] = []
            for m in cat.get("meals") or []:
                if isinstance(m, dict) and m.get("code"):
                    codes.append(str(m["code"]))
                elif isinstance(m, str):
                    codes.append(m)
            categories[name] = codes

    # 反查：若某商品没出现在任何分类里，也可从自身 category 字段补
    items: list[MenuItem] = []
    for code, m in meals_map.items():
        tags = m.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        item = MenuItem(
            code=str(code),
            name=str(m.get("name") or code),
            category=str(m.get("category") or _category_of(categories, code) or ""),
            tags=[str(t) for t in tags],
            current_price_fen=safe_yuan_to_fen(m.get("currentPrice")),
            original_price_fen=safe_yuan_to_fen(m.get("originalPrice")),
            discount_type=m.get("discountType"),
            can_with_order=bool(m.get("canWithOrder")),
            with_order=m.get("withOrder") if isinstance(m.get("withOrder"), dict) else None,
            image=str(m.get("image") or ""),
        )
        items.append(item)

    return MenuSnapshot(items, categories, {"frequent": data.get("frequent")})


def _category_of(categories: dict[str, list[str]], code: str) -> str:
    for name, codes in categories.items():
        if code in codes:
            return name
    return ""


def menu_digest(snapshot: MenuSnapshot) -> str:
    """菜单指纹：用于检测菜单是否发生变化（旧确认是否失效）。"""
    import hashlib

    payload = "|".join(
        f"{i.code}:{i.current_price_fen}:{i.original_price_fen}" for i in sorted(snapshot.items, key=lambda x: x.code)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
