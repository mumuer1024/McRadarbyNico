"""优惠券：聚合、适用匹配、单券试算编排。

已知真实约束（P0-D / 实测）：
- 一单只能用 **1 张券**（官方错误码 600022「暂不支持多张券使用」）
- 只有 query-store-coupons 返回 couponId + couponCode + 适用商品 productCode，能驱动算价
- query-my-coupons / available-coupons 返回 Markdown，需尽力解析，解析不确定要如实标注
- 券池存在**同名重复批次** → 不能用券名做唯一键
- 券专用 productCode 可能不在普通菜单里，但可直接用于 calculate-price
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .models import CouponInfo
from .money import Fen

__all__ = [
    "normalize_store_coupons",
    "normalize_my_coupons",
    "normalize_available_coupons",
    "match_coupons_to_items",
    "build_single_coupon_trials",
    "pick_best_trial",
    "diff_coupon_pools",
    "CouponPool",
]

_DATE_RANGE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)\s*[-~至]\s*"
    r"(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)"
)


def _mk_uid(*parts: str) -> str:
    basis = "|".join(p for p in parts if p)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


@dataclass
class CouponPool:
    my: list[CouponInfo] = field(default_factory=list)
    store: list[CouponInfo] = field(default_factory=list)
    available: list[CouponInfo] = field(default_factory=list)
    parse_warnings: list[str] = field(default_factory=list)

    def claimable(self) -> list[CouponInfo]:
        return [c for c in self.available if c.status == "可领取"]

    def to_dict(self) -> dict:
        return {
            "my": [c.to_dict() for c in self.my],
            "store": [c.to_dict() for c in self.store],
            "available": [c.to_dict() for c in self.available],
            "parse_warnings": list(self.parse_warnings),
        }


# ---- 门店券（结构化，唯一能算价的来源）-------------------------------------

def normalize_store_coupons(raw: dict) -> list[CouponInfo]:
    data = raw.get("data") if isinstance(raw, dict) else None
    if isinstance(data, dict):
        rows = data.get("list") or data.get("coupons") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    out: list[CouponInfo] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        vf, vt = "", ""
        tr = str(r.get("tradeDateTime") or "")
        m = _DATE_RANGE_RE.search(tr)
        if m:
            vf, vt = m.group(1), m.group(2)
        elif tr:
            vf = tr
        products = r.get("products") or []
        codes = [str(p.get("productCode")) for p in products if isinstance(p, dict) and p.get("productCode")]
        cid = str(r.get("couponId") or "")
        ccode = str(r.get("couponCode") or "")
        out.append(CouponInfo(
            title=str(r.get("title") or ""),
            coupon_id=cid,
            coupon_code=ccode,
            valid_from=vf,
            valid_to=vt,
            product_codes=codes,
            source="store",
            status="可用",
            uid=_mk_uid(cid, ccode) if (cid or ccode) else _mk_uid(r.get("title", ""), str(r.get("promotionId", ""))),
        ))
    return out


# ---- 我的券（Markdown，尽力解析）-------------------------------------------

def normalize_my_coupons(raw) -> tuple[list[CouponInfo], list[str]]:
    text = _extract_text(raw)
    if not text:
        return [], ["query-my-coupons 返回无法提取文本，未解析"]

    lines = text.splitlines()
    # 🔴 实测（2026-10-10，真实 query-my-coupons）：返回结构为
    #   `## 券名` + `- **优惠**: ...` / `- **有效期**: ...` / `- **标签**: ...`
    # 旧实现把"每个 bullet 当一张券"，会把 12 张真实券解析成 48 条垃圾
    # （标题变成「优惠**: ¥29.9」…）。有 `## ` 标题时按结构化解析。
    if any(l.strip().startswith("## ") for l in lines):
        entries = _parse_my_coupons_structured(lines)
    else:
        entries = _parse_my_coupons_legacy(lines)

    warnings: list[str] = []
    if not entries:
        warnings.append("未能从 Markdown 中解析出券条目")
    else:
        warnings.append("query-my-coupons 为 Markdown 文本，券名/有效期为尽力解析；无 couponId，不能直接驱动算价")
    return entries, warnings


def _parse_my_coupons_structured(lines) -> list[CouponInfo]:
    """解析 `## 券名` + `- **字段**: 值` 的真实结构。"""
    entries: list[CouponInfo] = []
    cur: dict | None = None
    for line in lines:
        s = line.strip()
        if s.startswith("## "):
            if cur:
                entries.append(_mk_my(cur, len(entries)))
            cur = {"title": s[3:].strip()}
            continue
        if not s or cur is None:
            continue
        body = re.sub(r"^[-*]\s*", "", s).rstrip("\\").strip()
        m = re.match(r"^\*\*([^*]+)\*\*\s*[:：]\s*(.*)$", body)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if "有效期" in key:
            mm = _DATE_RANGE_RE.search(val)
            if mm:
                cur["valid_from"], cur["valid_to"] = mm.group(1), mm.group(2)
            elif val:
                cur["valid_from"] = val
        elif "标签" in key:
            cur["labels"] = [x.strip() for x in re.split(r"[、,，/]+", val) if x.strip()]
        # 其余字段（优惠 / 领取时间）不进入标题，避免产生垃圾条目
    if cur:
        entries.append(_mk_my(cur, len(entries)))
    return entries


def _parse_my_coupons_legacy(lines) -> list[CouponInfo]:
    """兜底：无 `## ` 标题时，按旧行为把每个 bullet 当作一张券名。"""
    entries: list[CouponInfo] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("-") and not line.startswith("*"):
            continue
        title = line.lstrip("-* ").strip()
        if not title:
            continue
        entries.append(_mk_my({"title": title}, len(entries)))
    return entries


def _mk_my(cur: dict, idx: int) -> CouponInfo:
    title = cur.get("title", "")
    vf = cur.get("valid_from", "")
    return CouponInfo(
        title=title,
        source="my",
        status="已持有",
        valid_from=vf,
        valid_to=cur.get("valid_to", ""),
        labels=list(cur.get("labels") or []),
        # 同名券存在多个批次（有效期不同）→ uid 必须带有效期
        uid=_mk_uid("my", title, vf, str(idx)),
    )


# ---- 可领券（Markdown：标题 + 状态 + 图片）----------------------------------

def normalize_available_coupons(raw) -> tuple[list[CouponInfo], list[str]]:
    text = _extract_text(raw)
    if not text:
        return [], ["available-coupons 返回无法提取文本，未解析"]

    entries: list[CouponInfo] = []
    warnings: list[str] = []
    cur: dict = {}
    for line in text.splitlines():
        s = line.strip().rstrip("\\").strip()
        if not s:
            continue

        # 先剥掉列表符号，再判断是否为「字段名：值」形态。
        # 实测行形如 "- 优惠券标题：麦旋风任选 \" —— 若只按 "-" 处理会把整行当成券名。
        body = re.sub(r"^[-*]\s*", "", s)
        if "：" in body:
            key, _, val = body.partition("：")
            key, val = key.strip(), val.strip()
            if "优惠券标题" in key:
                if cur.get("title"):
                    entries.append(_mk_available(cur))
                cur = {"title": val}
                continue
            if "状态" in key:
                cur["status"] = val
                continue

        if s.startswith("-") or s.startswith("*"):
            if cur.get("title"):
                entries.append(_mk_available(cur))
            cur = {"title": body}
        elif "src=" in s or "<img" in s:
            m = re.search(r'src="([^"]+)"', s)
            if m:
                cur["image"] = m.group(1)

    if cur.get("title"):
        entries.append(_mk_available(cur))

    if not entries:
        warnings.append("未能解析出可领券条目")
    return entries, warnings


def _mk_available(cur: dict) -> CouponInfo:
    title = cur.get("title", "")
    image = cur.get("image", "")
    return CouponInfo(
        title=title,
        source="available",
        status=cur.get("status", "未知"),
        # 无 couponId；用「标题 + 图片 URL」生成弱 uid
        # 实测同名券存在多个批次（图片 URL 不同），因此必须带上图片
        uid=_mk_uid("avail", title, image),
    )


def _extract_text(raw) -> str:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return ""
    for key in ("content", "text", "result"):
        v = raw.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    return item["text"]
    data = raw.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("content", "text", "markdown"):
            if isinstance(data.get(key), str):
                return data[key]
    return ""


# ---- 适用匹配与单券试算 -----------------------------------------------------

def match_coupons_to_items(coupons: list[CouponInfo], item_codes: list[str]) -> list[CouponInfo]:
    """找出适用商品与当前组合有交集的券。"""
    codes = set(str(c) for c in item_codes)
    if not codes:
        return []
    return [c for c in coupons if c.product_codes and (set(c.product_codes) & codes)]


def build_single_coupon_trials(
    base_request: dict,
    coupons: list[CouponInfo],
    target_index: int = 0,
) -> list[dict]:
    """为每张候选券生成**一张券**的 calculate-price 请求。

    不会把两张券放进同一个请求 —— 官方明确不支持（600022）。
    """
    trials: list[dict] = []
    for c in coupons:
        if not (c.coupon_id and c.coupon_code):
            continue
        req = _deepcopy(base_request)
        items = req.get("items") or []
        if not items:
            continue
        items = _deepcopy(items)
        items[target_index]["couponId"] = c.coupon_id
        items[target_index]["couponCode"] = c.coupon_code
        req["items"] = items
        trials.append({"coupon": c.to_dict(), "request": req})
    return trials


def _deepcopy(x):
    import copy
    return copy.deepcopy(x)


def pick_best_trial(entries: list[dict]) -> dict:
    """entries: [{coupon, request, result(PricingResult.to_dict()|原始)}]

    选实付最低的一个。返回 {"best":..., "compared": n, "note":...}
    """
    scored = []
    for e in entries:
        res = e.get("result") or {}
        data = res.get("data") if isinstance(res, dict) else None
        payload = data if isinstance(data, dict) else (res if isinstance(res, dict) else {})
        ok = res.get("success", True) if isinstance(res, dict) else True
        payable = payload.get("price")
        if not ok or payable is None:
            scored.append({"coupon": e.get("coupon"), "payable_fen": None,
                           "error": (payload.get("message") if isinstance(payload, dict) else "请求失败")})
            continue
        scored.append({"coupon": e.get("coupon"), "payable_fen": int(payable),
                       "discount_fen": int(payload.get("discount") or 0)})

    valid = [s for s in scored if s.get("payable_fen") is not None]
    best = min(valid, key=lambda s: s["payable_fen"]) if valid else None
    note = "每张券单独试算，取实付最低；券不可叠加（600022）" if best else "没有可用券试算成功"
    return {"best": best, "compared": len(scored), "all": scored, "note": note}


# ---- 领券前后对比（同名重复批次处理）---------------------------------------

def diff_coupon_pools(before: CouponPool, after: CouponPool) -> dict:
    """对比领取前后的券池。

    ⚠️ 若接口未给出可靠唯一标识，只报告「可可靠确认的变化」，
    绝不虚构精确领取数量。
    """
    before_claimable = before.claimable()
    after_claimable = after.claimable()

    before_uids = {c.uid for c in before.claimable()}
    after_uids = {c.uid for c in after.claimable()}

    # 同名但不同 uid（重复批次）会导致按名比对失真
    before_titles = _title_counts(before_claimable)
    after_titles = _title_counts(after_claimable)

    reliable = bool(before_uids) and all(c.uid for c in after_claimable)

    newly_claimed_uids = before_uids - after_uids
    new_claimable = after_uids - before_uids

    result = {
        "before_claimable": len(before_claimable),
        "after_claimable": len(after_claimable),
        "reliable_uid": reliable,
        "confirmed_no_longer_claimable": len(newly_claimed_uids),
        "new_claimable_appeared": len(new_claimable),
        "title_delta": _delta_counts(before_titles, after_titles),
    }

    if reliable:
        result["conclusion"] = (
            f"可可靠确认：{len(newly_claimed_uids)} 张券不再处于「可领取」状态"
            + (f"；另有 {len(new_claimable)} 张新的可领券出现" if new_claimable else "")
        )
        result["confidence"] = "high"
    else:
        result["conclusion"] = (
            "接口未提供可靠唯一标识，只能按券名计数比对；"
            f"可领取券由 {len(before_claimable)} 张变为 {len(after_claimable)} 张。"
            "不声称精确领取数量。"
        )
        result["confidence"] = "low"
    return result


def _title_counts(coupons: list[CouponInfo]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in coupons:
        out[c.title] = out.get(c.title, 0) + 1
    return out


def _delta_counts(before: dict[str, int], after: dict[str, int]) -> list[dict]:
    keys = sorted(set(before) | set(after))
    delta = []
    for k in keys:
        b, a = before.get(k, 0), after.get(k, 0)
        if b != a:
            delta.append({"title": k, "before": b, "after": a, "change": a - b})
    return delta
