"""预算盲盒组合生成。

约束（P0-D / 交互设计 4.1）：
- 预算为硬约束：候选估算总额必须 <= 预算
- 同一轮抽取结果去重（组合指纹）
- 偏好与不喜欢商品过滤
- 只对少量候选调用 calculate-price（Top-N），不做无意义批量请求
- 菜单价格**仅用于候选筛选**，不作为最终应付金额承诺
- 低预算无解时如实说明，不编造
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .models import Combo, ComboItem, MenuItem
from .money import Fen

__all__ = [
    "Bucket",
    "bucketize",
    "generate_candidates",
    "BlindBoxRequest",
    "BlindBoxResult",
    "TEMPLATES",
]

# ---- 分桶启发式 ------------------------------------------------------------

MAIN_KEYWORDS = ("堡", "卷", "饭", "面", "鸡腿", "牛", "鱼", "早餐", "套餐", "麦满分", "猪柳", "鸡排")
SIDE_KEYWORDS = ("薯条", "薯饼", "鸡块", "麦乐鸡", "鸡翅", "翅", "小食", "派", "玉米", "沙拉", "饼", "扭扭", "洋葱")
DRINK_KEYWORDS = ("可乐", "雪碧", "咖啡", "奶昔", "牛奶", "饮品", "茶", "果汁", "水", "冰", "热饮", "冷饮", "美式", "拿铁")
DESSERT_KEYWORDS = ("冰淇淋", "麦旋风", "甜品", "圣代", "华夫", "松饼", "派")


@dataclass
class Bucket:
    main: list[MenuItem] = field(default_factory=list)
    side: list[MenuItem] = field(default_factory=list)
    drink: list[MenuItem] = field(default_factory=list)
    dessert: list[MenuItem] = field(default_factory=list)
    other: list[MenuItem] = field(default_factory=list)

    def total(self) -> int:
        return len(self.main) + len(self.side) + len(self.drink) + len(self.dessert) + len(self.other)


def _hay(item: MenuItem) -> str:
    return f"{item.name} {item.category} {' '.join(item.tags)}"


def bucketize(items: list[MenuItem]) -> Bucket:
    """分桶：优先用「商品名 + 标签」判断，分类名只作兜底。

    原因：实测分类名存在混合类目（如「小食甜品/其他」），
    直接用分类名会把薯条误判成甜品。
    """
    b = Bucket()
    for it in items:
        name_tags = f"{it.name} {' '.join(it.tags)}"
        category = it.category or ""
        placed = False
        for keys, target in (
            (DRINK_KEYWORDS, "drink"),
            (DESSERT_KEYWORDS, "dessert"),
            (SIDE_KEYWORDS, "side"),
            (MAIN_KEYWORDS, "main"),
        ):
            if any(k in name_tags for k in keys):
                getattr(b, target).append(it)
                placed = True
                break
        if placed:
            continue
        # 兜底：用分类名再判一次
        for keys, target in (
            (DRINK_KEYWORDS, "drink"),
            (DESSERT_KEYWORDS, "dessert"),
            (SIDE_KEYWORDS, "side"),
            (MAIN_KEYWORDS, "main"),
        ):
            if any(k in category for k in keys):
                getattr(b, target).append(it)
                placed = True
                break
        if not placed:
            b.other.append(it)

    # other 兜底并入主食候选，避免桶太小
    if not b.main and b.other:
        b.main = b.other
        b.other = []
    return b


# ---- 组合模板 --------------------------------------------------------------

TEMPLATES = {
    "complete": ("main", "side", "drink"),   # 完整餐
    "simple": ("main", "drink"),             # 简餐
    "single": ("main",),                     # 单点
    "snack": ("side", "drink"),              # 加餐
}


@dataclass
class BlindBoxRequest:
    budget_fen: Fen
    count: int = 3                       # 返回候选数（送 calculate-price 的数量）
    mode: str = "random"                 # random / full / value
    seed: int | None = None
    exclude_fingerprints: set[str] = field(default_factory=set)
    prefer_codes: list[str] = field(default_factory=list)   # 优惠优先：能用券的商品
    allow_conditional_price: bool = False                   # 是否允许 canWithOrder 商品


@dataclass
class BlindBoxResult:
    candidates: list[Combo] = field(default_factory=list)
    exhausted: bool = False
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "exhausted": self.exhausted,
            "reason": self.reason,
            "diagnostics": self.diagnostics,
        }


def _price_of(item: MenuItem) -> Fen | None:
    return item.effective_price_fen


def _score(combo: Combo, budget_fen: Fen, mode: str) -> float:
    """启发式打分：越接近预算、组合越完整，分越高。"""
    if budget_fen <= 0:
        return 0.0
    usage = combo.est_total_fen / budget_fen
    closeness = 1.0 - abs(1.0 - usage)          # 越贴近预算越好
    completeness = len(combo.items) / 3.0
    base = closeness * 0.7 + min(completeness, 1.0) * 0.3
    if mode == "full":
        base += (len(combo.items) >= 3) * 0.15
    return round(base, 4)


def generate_candidates(
    items: list[MenuItem],
    req: BlindBoxRequest,
    max_try: int = 4000,
) -> BlindBoxResult:
    rng = random.Random(req.seed)

    pool = [i for i in items if _price_of(i) is not None]
    if not req.allow_conditional_price:
        pool = [i for i in pool if not i.price_is_conditional]
    if not pool:
        return BlindBoxResult(exhausted=True, reason="当前门店没有可用于组合的带价商品", diagnostics={"pool": 0})

    bucket = bucketize(pool)
    diag = {
        "pool_size": len(pool),
        "bucket_sizes": {k: len(v) for k, v in bucket.__dict__.items()},
        "cheapest_item_fen": min(_price_of(i) for i in pool),
        "attempts": 0,
        "rejected_over_budget": 0,
        "rejected_dup": 0,
    }

    # 模式权重
    if req.mode == "full":
        order = ["complete", "complete", "simple", "single"]
    elif req.mode == "value":
        order = ["complete", "simple", "single", "snack"]
    else:
        order = ["complete", "simple", "single", "snack"]

    def pick(bucket_name: str) -> MenuItem | None:
        cands = getattr(bucket, bucket_name, [])
        if not cands:
            return None
        if req.mode == "value" and req.prefer_codes:
            preferred = [c for c in cands if c.code in req.prefer_codes]
            if preferred:
                return rng.choice(preferred)
        return rng.choice(cands)

    results: list[Combo] = []
    seen: set[str] = set(req.exclude_fingerprints)
    attempts = 0
    stagnation = 0          # 连续多少次采样没有产出新组合

    while attempts < max_try and len(results) < req.count * 6:
        attempts += 1
        if stagnation >= 300:
            # 组合空间已被穷尽，提前退出，避免无意义的循环
            break
        tname = order[attempts % len(order)] if req.mode != "random" else rng.choice(list(TEMPLATES))
        keys = TEMPLATES[tname]
        chosen: list[MenuItem] = []
        for k in keys:
            it = pick(k)
            if it is not None:
                chosen.append(it)
        if not chosen:
            continue
        # 同一个商品不要重复出现两次
        if len({c.code for c in chosen}) != len(chosen):
            continue

        total = sum(_price_of(c) or 0 for c in chosen)
        if total > req.budget_fen:
            diag["rejected_over_budget"] += 1
            stagnation += 1
            continue

        combo = Combo(
            items=[ComboItem(code=c.code, name=c.name, quantity=1, price_fen=_price_of(c)) for c in chosen],
            est_total_fen=total,
            template=tname,
        )
        fp = combo.fingerprint
        if fp in seen:
            diag["rejected_dup"] += 1
            stagnation += 1
            continue
        seen.add(fp)
        stagnation = 0
        combo.score = _score(combo, req.budget_fen, req.mode)
        results.append(combo)

    diag["attempts"] = attempts

    if not results:
        cheapest = min(_price_of(i) for i in pool)
        return BlindBoxResult(
            exhausted=True,
            reason=(f"预算 ¥{req.budget_fen / 100:.2f} 在当前门店组不出任何符合条件的组合；"
                    f"最便宜的单项为 ¥{cheapest / 100:.2f}"),
            diagnostics=diag,
        )

    results.sort(key=lambda c: (-c.score, c.est_total_fen))
    top = results[: req.count]
    return BlindBoxResult(candidates=top, diagnostics=diag)
