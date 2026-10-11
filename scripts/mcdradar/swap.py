"""套餐子项替换与预算微调。

依据：query-meal-detail 返回 rounds[].choices[]，其中 diffPrice **可以为负数**
（实测小杯玉米杯 "- ¥0.5"）。预算差一点时，优先用「换便宜子项」压进预算，
而不是整组换掉。

只做建议（返回可替换方案），不直接改订单。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .money import Fen, parse_diff_price_fen

__all__ = ["SwapOption", "SwapPlan", "parse_rounds", "plan_swaps"]


@dataclass
class SwapOption:
    round_no: str
    round_name: str
    from_code: str
    from_name: str
    to_code: str
    to_name: str
    delta_fen: Fen             # 负数表示能省钱
    is_default: bool = False

    def to_dict(self) -> dict:
        return {
            "round": self.round_no,
            "roundName": self.round_name,
            "from": {"code": self.from_code, "name": self.from_name},
            "to": {"code": self.to_code, "name": self.to_name},
            "deltaFen": self.delta_fen,
        }


@dataclass
class SwapPlan:
    over_fen: Fen
    options: list[SwapOption] = field(default_factory=list)
    feasible: bool = False
    best: SwapOption | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "overFen": self.over_fen,
            "feasible": self.feasible,
            "options": [o.to_dict() for o in self.options],
            "best": self.best.to_dict() if self.best else None,
            "note": self.note,
        }


def parse_rounds(detail: dict) -> list[dict]:
    """从 query-meal-detail 返回里提取 rounds，并规范化 diffPrice 为分。"""
    data = detail.get("data") if isinstance(detail, dict) else None
    data = data if isinstance(data, dict) else (detail if isinstance(detail, dict) else {})
    rounds = data.get("rounds") or []
    out: list[dict] = []
    for r in rounds:
        if not isinstance(r, dict):
            continue
        choices = []
        for c in r.get("choices") or []:
            if not isinstance(c, dict):
                continue
            choices.append({
                "code": str(c.get("code") or ""),
                "name": str(c.get("name") or ""),
                "isDefault": bool(c.get("isDefault")),
                "diffFen": parse_diff_price_fen(c.get("diffPrice")),
            })
        out.append({
            "id": str(r.get("id") or ""),
            "name": str(r.get("name") or ""),
            "choices": choices,
        })
    return out


def plan_swaps(rounds: list[dict], over_fen: Fen) -> SwapPlan:
    """超预算 over_fen 时，找出所有能省钱的替换项。

    over_fen <= 0 表示没超预算，返回 feasible=True 的空方案。
    """
    if over_fen <= 0:
        return SwapPlan(over_fen=over_fen, feasible=True, note="未超预算，无需替换")

    options: list[SwapOption] = []
    for r in rounds:
        defaults = [c for c in r["choices"] if c["isDefault"]]
        base = defaults[0] if defaults else (r["choices"][0] if r["choices"] else None)
        if not base:
            continue
        base_fen = base["diffFen"] or 0
        for c in r["choices"]:
            if c["code"] == base["code"]:
                continue
            if c["diffFen"] is None:
                continue
            delta = (c["diffFen"] - base_fen)
            if delta < 0:
                options.append(SwapOption(
                    round_no=r["id"],
                    round_name=r["name"],
                    from_code=base["code"],
                    from_name=base["name"],
                    to_code=c["code"],
                    to_name=c["name"],
                    delta_fen=delta,
                    is_default=True,
                ))

    options.sort(key=lambda o: o.delta_fen)   # 最省的排前面
    best = None
    for o in options:
        if -o.delta_fen >= over_fen:
            best = o
            break

    plan = SwapPlan(
        over_fen=over_fen,
        options=options,
        feasible=best is not None,
        best=best,
    )
    if best:
        plan.note = (f"把「{best.from_name}」换成「{best.to_name}」可省 "
                     f"¥{abs(best.delta_fen) / 100:.2f}，足以压进预算")
    elif options:
        plan.note = (f"最省的替换只可省 ¥{abs(options[0].delta_fen) / 100:.2f}，"
                     f"仍差 ¥{(over_fen + options[0].delta_fen) / 100:.2f}，需提高预算或换组合")
    else:
        plan.note = "该套餐没有可降价的替换项，需换组合或提高预算"
    return plan


def apply_swap(round_list: list[dict], swap: SwapOption) -> list[dict]:
    """把替换应用到 create-order / calculate-price 的 roundList 上。"""
    import copy
    rl = copy.deepcopy(round_list)
    for r in rl:
        if str(r.get("round")) == str(swap.round_no):
            items = r.get("comboItemList") or []
            for it in items:
                if str(it.get("code")) == swap.from_code:
                    it["code"] = swap.to_code
            r["comboItemList"] = items
    return rl
