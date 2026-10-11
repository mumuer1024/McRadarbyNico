"""统一订单闭环（S4）：请求构造 → 摘要确认 → 提交 → 结果解析 → 支付期限 → 状态核对。

⚠️ 本模块**不发起任何网络/MCP 调用**。
真实 `create-order` 由宿主（WorkBuddy Skill）在拿到 may_submit=True 之后调用一次。

硬约束落地：
- P0-A：create-order 的结果未知 → 绝不自动重试（由 order_guard 阻断）
- P0-C：指纹 + 状态机 + 核对流程
- P0-E：状态只看 orderStatus 文本；没有 expirePayTime 就不给倒计时
- 4.3：确认后不静默替换任何内容（digest 校验）
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone

from .money import Fen, fen_to_yuan_str
from .order_guard import (
    CONFIRMED, FAILED, PREPARED, SUBMITTED, UNKNOWN, USER_CONFIRMED,
    TRUST_BOUNDARY, ConcurrentTransition,
    authorize_resubmit, mark_confirmed, mark_failed, mark_submitted,
    mark_unknown, may_submit, prepare, confirm, revoke_confirmation,
    confirmation_status, reconcile_plan, get_record, record_key,
)
from .state import StateStore, iso_ts, scrub
from .summary import canonical_json, request_digest, verify_confirmation
from .tool_risk import assert_may_call, retry_policy

__all__ = [
    "TRUST_BOUNDARY", "USER_CONFIRMED",
    "BE_TYPE_LABEL", "BE_TYPE_ORDER_TYPE", "BE_CODE_REQUIRED", "PRICING_CHECK_TTL",
    "PRICING_TRUST_BOUNDARY", "PRICING_SCOPE_NOTE", "ConcurrentTransition",
    "build_order_request",
    "validate_order_request",
    "prepare_order",
    "confirm_order",
    "revoke_confirmation",
    "confirmation_status",
    "record_pricing_check",
    "get_pricing_check",
    "pricing_check_key",
    "preflight",
    "begin_submit",
    "finish_unknown",
    "finish_confirmed",
    "finish_failed",
    "authorize_retry",
    "reconcile",
    "parse_order_result",
    "payment_deadline",
    "interpret_order_status",
    "verify_pricing_covers_items",
]

# ---- 状态文本映射（P0-E：只认文本，不认数字码）-----------------------------

PAID_LIKE = ("已支付", "配餐中", "配送中", "已完成", "已评价")
UNPAID_LIKE = ("待支付",)
CANCELLED_LIKE = ("已取消", "已关闭", "已退款")

# 支付入口形态 —— 2026-10-11 首次真实下单后由用户实测确认（非推测）
PAY_ENTRY_VERIFIED_NOTE = (
    "已实测确认：链接可在浏览器直接打开，呈现**扫码画面**；用麦当劳 App 或微信扫码后"
    "跳转麦当劳 App（未安装则跳应用商店）。App 未登录过需登录，已登录过免登录。"
    "此外，未支付订单会直接出现在麦当劳 App / 微信小程序里，**不扫码也可完成支付**。"
)
PAY_ENTRY_FALLBACK_NOTE = (
    "返回中未发现支付链接字段。可降级为：打开**麦当劳 App**或**麦当劳微信小程序**，"
    "在订单列表里找到该未支付订单完成支付（此路径已实测可用）。"
)

# beType → (orderType, 是否必传 beCode, 场景名)，来源：docs/03-mcp-capability-audit.md
BE_TYPE_LABEL = {1: "到店自取", 5: "得来速", 2: "麦乐送到家", 6: "企业团餐"}
BE_TYPE_ORDER_TYPE = {1: 1, 5: 1, 2: 2, 6: 2}
BE_CODE_REQUIRED = {1: False, 5: True, 2: True, 6: True}

# 算价覆盖校验结果的有效期：过期必须重新 calculate-price
PRICING_CHECK_TTL = 10 * 60

# 🔴 实测（2026-10-11 真实下单）：MCP 的 `now-time-info` 返回 `timezone: GMT+08:00`，
# 订单时间字段（expirePayTime / createTime）均为**北京时间**的朴素字符串。
# 若直接 `.timestamp()`，会按宿主本机时区解释 —— 在非 +08:00 的机器上，
# 15 分钟的支付期限会被算成 900+ 分钟。因此必须显式按 +08:00 解释。
MCP_TZ = timezone(timedelta(hours=8))

# 算价校验的可信边界（必须随结果一起对外说明）
PRICING_TRUST_BOUNDARY = (
    "⚠️ 信任边界：Python 只能校验「传入的订单请求」与「传入的算价响应」是否彼此一致、"
    "以及响应是否覆盖全部所选商品；它**不能独立证明该响应确实来自真实 MCP 服务端**。"
    "宿主必须传入由自身调用 calculate-price 得到的**原始响应**，"
    "禁止传入自行拼装、猜测或缓存伪造的数据。"
)

# 结论的适用范围（不得夸大）
PRICING_SCOPE_NOTE = (
    "「商品覆盖完整」仅表示本次 calculate-price 的返回包含全部所选商品，"
    "**不等于服务端承诺最终订单价格不变**；实际应付金额以提交订单时服务端返回为准。"
)


def _status_node(raw: dict) -> dict:
    """定位承载订单状态 / 支付期限的节点。

    🔴 实测（2026-10-11 真实下单）：`create-order` 把 `orderStatus` / `expirePayTime`
    放在 **`data.orderDetail`** 下，而 `query-order` 放在 `data` 顶层 ——
    只读顶层会把「待支付 + 明确的 2026-10-11 13:35:51 支付截止」误报成「未知」。
    因此优先取 `data.orderDetail`（当其确实含状态/期限字段时），否则回退到 `data`。
    """
    if not isinstance(raw, dict):
        return {}
    d = raw.get("data")
    d = d if isinstance(d, dict) else raw
    od = d.get("orderDetail")
    if isinstance(od, dict) and any(k in od for k in ("orderStatus", "expirePayTime", "status")):
        return od
    return d


def interpret_order_status(order_detail: dict) -> dict:
    """解释订单状态。未知文本一律返回 unknown，绝不误判成功或失败。"""
    d = _status_node(order_detail)
    text = str(d.get("orderStatus") or "").strip()
    code = d.get("status")

    if not text:
        return {"state": "unknown", "text": "", "code": code,
                "note": "未返回 orderStatus 文本，无法判断状态"}
    for kw in CANCELLED_LIKE:
        if kw in text:
            return {"state": "cancelled", "text": text, "code": code, "note": f"命中「{kw}」"}
    for kw in UNPAID_LIKE:
        if kw in text:
            return {"state": "unpaid", "text": text, "code": code, "note": f"命中「{kw}」，尚未支付"}
    for kw in PAID_LIKE:
        if kw in text:
            return {"state": "paid", "text": text, "code": code, "note": f"命中「{kw}」"}
    return {"state": "unknown", "text": text, "code": code,
            "note": f"未识别的状态文本「{text}」，不做成功/失败推断"}


def payment_deadline(order_detail: dict, now: float | None = None) -> dict:
    """P0-E：按订单**实际** expirePayTime 计算剩余时间。

    没有可靠到期时间 → known=False，绝不虚构倒计时。
    """
    now = now or time.time()
    d = _status_node(order_detail)
    raw = d.get("expirePayTime")
    if not raw:
        return {"known": False, "expire_at": None, "remain_seconds": None,
                "note": "该订单未返回 expirePayTime，不显示支付倒计时"}

    s = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=MCP_TZ)
            ts = dt.timestamp()
            remain = int(ts - now)
            return {
                "known": True,
                "expire_at": s,
                "remain_seconds": max(remain, 0),
                "note": f"剩余 {max(remain, 0) // 60} 分 {max(remain, 0) % 60} 秒" if remain > 0
                        else "支付期限已过",
            }
        except ValueError:
            continue
    return {"known": False, "expire_at": s, "remain_seconds": None,
            "note": f"expirePayTime 格式无法解析（{s}），不显示倒计时"}


# ---- 请求构造 --------------------------------------------------------------

def build_order_request(
    *,
    store_code: str,
    order_type: int,
    be_type: int,
    be_code: str = "",
    items: list[dict],
    take_way_code: str = "",
    address_id: str = "",
    reservation_date: str = "",
    need_tableware: bool | None = None,
    remark: str = "",
    with_order: dict | None = None,
    gm_service_code: str = "",
) -> dict:
    """构造 create-order 请求体。

    入参与 calculate-price 高度同构（实测），这里只补 takeWayCode / addressId 等。
    """
    req: dict = {
        "storeCode": str(store_code),
        "orderType": int(order_type),
        "beType": int(be_type),
        "items": _norm_items(items),
    }
    # beCode：beType=1 不传；2/5/6 必传
    if int(be_type) != 1 and be_code:
        req["beCode"] = str(be_code)
    if order_type == 1:
        if take_way_code:
            req["takeWayCode"] = str(take_way_code)
    else:
        if address_id:
            req["addressId"] = str(address_id)
        if remark:
            req["remark"] = str(remark)[:50]
    if reservation_date:
        req["reservationDate"] = reservation_date
    if need_tableware is not None:
        req["needTableware"] = bool(need_tableware)
    if with_order:
        req["withOrder"] = with_order
    if int(be_type) == 6 and gm_service_code:
        req["gmServiceCode"] = gm_service_code
    return req


def _norm_items(items: list[dict]) -> list[dict]:
    out = []
    for it in items or []:
        o = {"productCode": str(it.get("productCode") or it.get("code")),
             "quantity": int(it.get("quantity", 1))}
        for k in ("couponId", "couponCode", "modification", "roundList"):
            if it.get(k) not in (None, [], {}):
                o[k] = it[k]
        out.append(o)
    return out


def _collect_codes(products) -> dict[str, int]:
    """递归收集算价返回中的商品编码（含套餐展开后的子项）。

    套餐在返回里可能被展开成子项（comboItemList / roundList），
    若只比对顶层 productCode 会把「套餐已正确算价」误判成「商品被丢弃」。
    """
    out: dict[str, int] = {}
    for p in products or []:
        if not isinstance(p, dict):
            continue
        code = str(p.get("productCode") or p.get("code") or "")
        if code:
            out[code] = out.get(code, 0) + int(p.get("quantity", 0) or 0)
        for key in ("comboItemList", "roundList", "productList", "items", "subList"):
            nested = p.get(key)
            if isinstance(nested, list):
                for c, q in _collect_codes(nested).items():
                    out[c] = out.get(c, 0) + q
    return out


def verify_pricing_covers_items(items: list[dict], pricing: dict) -> dict:
    """核对实时算价结果是否覆盖全部所选商品。

    🔴 实测发现的真实风险：请求 {麦香鸡, 可乐中杯}，服务端 productList 只回了麦香鸡，
    price 也只算了麦香鸡 —— **未匹配的商品被静默丢弃**，应付金额与用户选品不符。

    因此下单前必须核对：缺任何一个商品都要拦下来，不能让用户为一个"少了东西"的订单付款。

    安全失败原则（fail-closed）：
    - 响应结构无法识别（没有 productList）→ 判为**不可靠**，禁止下单
    - 套餐展开 / 券专用商品编码做兼容识别，但兼容识别后仍缺 → 仍禁止下单
    """
    d = pricing.get("data") if isinstance(pricing, dict) else None
    d = d if isinstance(d, dict) else (pricing if isinstance(pricing, dict) else {})

    requested: dict[str, int] = {}
    for it in items or []:
        code = str(it.get("productCode") or it.get("code"))
        requested[code] = requested.get(code, 0) + int(it.get("quantity", 1) or 0)

    product_list = d.get("productList")
    structure_known = isinstance(product_list, list)
    returned = _collect_codes(product_list or [])

    # 优惠券专用商品编码可能不出现在 productList，而在券/促销列表里
    for key in ("couponList", "promotionList", "discountList"):
        for c, q in _collect_codes(d.get(key) or []).items():
            returned[c] = returned.get(c, 0) + q

    missing = {c: q for c, q in requested.items() if returned.get(c, 0) < q}
    extra = {c: q for c, q in returned.items() if c not in requested}

    if not structure_known:
        return {
            "ok": False,
            "missing": dict(missing),
            "extra": extra,
            "requested": requested,
            "returned": returned,
            "structure_known": False,
            "reliable": False,
            "message": ("⚠️ 算价响应中未找到 productList，无法可靠核对商品完整性。"
                        "按安全失败处理：禁止下单，请重新调用 calculate-price 并核对返回结构"),
        }

    ok = not missing
    return {
        "ok": ok,
        "missing": missing,
        "extra": extra,
        "requested": requested,
        "returned": returned,
        "structure_known": True,
        "reliable": True,
        "message": ("算价结果覆盖全部所选商品（" + PRICING_SCOPE_NOTE + "）" if ok else
                    f"⚠️ 算价结果缺少商品 {missing}，服务端可能已下架或当前门店不可售。"
                    f"必须重新拉取菜单并重新生成方案，不得按此金额下单"),
    }


def validate_order_request(req: dict) -> list[str]:
    """提交前自检。返回问题列表（空列表表示通过）。"""
    errs: list[str] = []
    if not req.get("storeCode"):
        errs.append("缺少 storeCode")
    if req.get("orderType") not in (1, 2):
        errs.append("orderType 必须为 1（到店）或 2（外送）")
    if req.get("beType") not in (1, 2, 5, 6):
        errs.append("beType 必须为 1/2/5/6")
    if not req.get("items"):
        errs.append("items 不能为空")
    else:
        for i, it in enumerate(req["items"]):
            if not it.get("productCode"):
                errs.append(f"items[{i}] 缺少 productCode")
            if int(it.get("quantity", 0)) < 1:
                errs.append(f"items[{i}] quantity 必须 >= 1")
    ot, bt = req.get("orderType"), req.get("beType")

    # orderType 与 beType 的对应关系（见 docs/03-mcp-capability-audit.md）
    want_ot = BE_TYPE_ORDER_TYPE.get(bt)
    if want_ot and ot != want_ot:
        errs.append(f"beType={bt} 应对应 orderType={want_ot}（{BE_TYPE_LABEL.get(bt, '')}），"
                    f"当前 orderType={ot}")

    if ot == 1 and not req.get("takeWayCode"):
        errs.append("orderType=1（到店自取/得来速）必须传 takeWayCode"
                    "（取自 calculate-price.takeWayList[].code）")
    if ot == 2 and not req.get("addressId"):
        errs.append("orderType=2（麦乐送/团餐）必须传 addressId")

    # beCode 规则按 **beType** 判定，不能按 orderType 一刀切：
    #   beType=1 到店自取 → orderType=1，**不传** beCode（传了会报错）
    #   beType=5 得来速   → orderType=1，**必传** beCode
    #   beType=2/6        → orderType=2，**必传** beCode
    if bt == 1:
        if req.get("beCode"):
            errs.append("beType=1（到店自取）不应传 beCode（官方规则：beType=1 不传）")
    elif bt in (2, 5, 6):
        if not req.get("beCode"):
            errs.append(f"beType={bt}（{BE_TYPE_LABEL.get(bt, '')}）必须传 beCode")
    return errs


# ---- 状态机 ----------------------------------------------------------------

def prepare_order(store: StateStore, req: dict, est_payable_fen: Fen | None = None) -> dict:
    """展示摘要后登记请求（此时**尚未确认**，状态为 awaiting_confirm）。

    流程必须是：prepare_order → 展示摘要 → confirm_order(用户明确确认) → preflight。
    """
    errs = validate_order_request(req)
    if errs:
        return {"ok": False, "errors": errs}
    payload = dict(req)
    if est_payable_fen is not None:
        payload["_est_payable_fen"] = est_payable_fen
    digest = request_digest(req)
    rec = prepare(store, digest, payload)
    return {"ok": True, "digest": digest, "record": rec,
            "status": rec.get("status"),
            "note": "已登记待确认请求（awaiting_confirm）；"
                    "必须展示完整摘要并取得用户明确确认后才可提交"}


def confirm_order(store: StateStore, digest: str, req: dict, *,
                  user_confirmed: bool, note: str = "") -> dict:
    """P0-2：把「用户已明确确认」变成可验证的状态。

    ⚠️ 信任边界：本函数只能记录宿主的声明，**不能证明用户真实意图**。
    宿主必须在收到用户明确的下单授权后，才以 user_confirmed=True 调用；
    禁止用模型推断、猜测或任何自动流程代替。
    """
    return confirm(store, digest, req, user_confirmed=user_confirmed, note=note)


# ---- 算价覆盖校验闸门（P0-5）-----------------------------------------------

def pricing_check_key(digest: str) -> str:
    return f"pricing_check_{digest}"


def record_pricing_check(store: StateStore, digest: str, *,
                         request: dict, pricing: dict,
                         now: float | None = None) -> dict:
    """登记一次算价覆盖校验结果（供提交闸门强制核查）。

    🔴 **不接受外部直接给出的 ok**：结果必须由「当前订单请求」+「宿主传入的
    calculate-price 原始响应」现场计算得出，再绑定到订单指纹与响应证据上。

    安全失败：请求与指纹不符 / 响应结构不可靠 / 缺商品 / 数量不足 → 一律记为不通过。
    """
    now = now or time.time()

    if not isinstance(request, dict) or not isinstance(pricing, dict):
        return {"ok": False, "recorded": False,
                "reason": "必须同时提供订单请求（request）与 calculate-price 原始响应（pricing）",
                "note": PRICING_TRUST_BOUNDARY}

    actual = request_digest(request)
    if actual != digest:
        return {"ok": False, "recorded": False,
                "reason": "订单请求与指纹不一致，校验结果不能用于该订单",
                "expected": digest, "actual": actual,
                "note": PRICING_TRUST_BOUNDARY}

    computed = verify_pricing_covers_items(request.get("items") or [], pricing)
    d = pricing.get("data") if isinstance(pricing, dict) else None
    d = d if isinstance(d, dict) else (pricing if isinstance(pricing, dict) else {})

    rec = {
        "digest": digest,
        "request_digest": actual,
        # 证据绑定：记录本次所依据的原始响应指纹，便于追溯结果来源
        "response_digest": hashlib.sha256(canonical_json(pricing).encode("utf-8")).hexdigest(),
        "payable_fen": int(d.get("price") or 0),
        "ok": bool(computed.get("ok")),
        "reliable": bool(computed.get("reliable", False)),
        "missing": computed.get("missing") or {},
        "at": now,
        "note": computed.get("message", ""),
    }
    ok = bool(rec["ok"] and rec["reliable"])
    store.write(pricing_check_key(digest), scrub(rec))
    return {"ok": ok, "recorded": True, "record": rec, "check": computed,
            "reason": rec["note"], "note": PRICING_SCOPE_NOTE + " " + PRICING_TRUST_BOUNDARY}


def get_pricing_check(store: StateStore, digest: str) -> dict | None:
    return store.read(pricing_check_key(digest))


def _check_pricing_gate(store: StateStore, digest: str,
                        ttl: float = PRICING_CHECK_TTL, now: float | None = None) -> dict:
    now = now or time.time()
    pc = get_pricing_check(store, digest)
    if pc is None:
        return {"ok": False,
                "reason": "缺少算价覆盖校验记录：下单前必须先用 check-pricing 核对 "
                          "calculate-price 的返回是否覆盖全部所选商品"}
    if not pc.get("ok"):
        return {"ok": False,
                "reason": f"算价覆盖校验未通过（缺商品 {pc.get('missing')}），禁止下单"}
    if not pc.get("reliable", True):
        return {"ok": False, "reason": "算价响应结构无法可靠核对，按安全失败处理，禁止下单"}
    if pc.get("digest") != digest or pc.get("request_digest") != digest:
        return {"ok": False, "reason": "算价校验记录与当前订单指纹不匹配，必须重新核价"}
    if not pc.get("response_digest"):
        return {"ok": False,
                "reason": "算价校验记录缺少原始响应证据（response_digest），"
                          "无法确认结果来源，按安全失败处理，禁止下单"}
    age = now - (pc.get("at") or 0)
    if age > ttl:
        return {"ok": False,
                "reason": f"算价结果已过 {int(age)}s（有效期 {int(ttl)}s），"
                          f"必须重新调用 calculate-price 并重新核对"}
    return {"ok": True, "reason": f"算价覆盖校验通过（{int(age)}s 前核价）",
            "age_seconds": int(age)}


def _full_submit_gate(store: StateStore, digest: str, req: dict | None, *,
                      require_pricing_check: bool = True,
                      now: float | None = None) -> dict:
    """🔒 **唯一**的提交闸门：任何提交路径都必须经过它，不得另开分支。

    四重校验，全部通过才允许提交：
      1. 请求结构合法（validate_order_request）
      2. 当前请求与用户确认时的指纹一致（内容未被改动）
      3. 状态机允许提交（**且用户已完成最终确认**）
      4. 存在新鲜、可靠且通过的算价覆盖校验（防 MCP 静默丢商品）

    preflight() 与 begin_submit() 共用本函数 —— 后者**不依赖**调用方是否先跑过
    preflight()，从而堵住「跳过 preflight 直接 begin_submit」的绕过路径。
    """
    now = now or time.time()
    policy = retry_policy("create-order")
    rp = {"max_auto_retries": policy.max_auto_retries,
          "on_unknown_result": policy.on_unknown_result}

    if not isinstance(req, dict) or not req:
        return {
            "allowed": False, "structural": ["缺少当前订单请求内容，无法校验指纹"],
            "request_consistent": {"ok": False, "message": "无请求内容"},
            "guard": may_submit(store, digest, now=now),
            "pricing_check": {"ok": False, "reason": "无请求内容，无法核对算价"},
            "retry_policy": rp,
            "message": "禁止提交：必须传入当前完整订单请求以复核指纹与算价覆盖",
        }

    structural = validate_order_request(req)
    check = verify_confirmation(digest, req)
    gate = may_submit(store, digest, now=now)
    pricing = (_check_pricing_gate(store, digest, now=now) if require_pricing_check
               else {"ok": True, "reason": "已显式跳过算价覆盖校验（仅限测试）"})

    allowed = bool(not structural and check["ok"] and gate["allowed"] and pricing["ok"])

    if structural:
        reason = "请求结构不合法：" + "；".join(structural)
    elif not check["ok"]:
        reason = check.get("message")
    elif not gate["allowed"]:
        reason = gate.get("reason")
    elif not pricing["ok"]:
        reason = pricing.get("reason")
    else:
        reason = "可以提交（仅此一次，失败不得自动重试）"

    return {
        "allowed": allowed,
        "structural": structural,
        "request_consistent": check,
        "guard": gate,
        "pricing_check": pricing,
        "retry_policy": rp,
        "message": reason,
    }


def preflight(store: StateStore, digest: str, req: dict, *,
              require_pricing_check: bool = True, now: float | None = None) -> dict:
    """提交前校验（只读，不改状态）。等价于 begin_submit 内部的闸门。"""
    return _full_submit_gate(store, digest, req,
                             require_pricing_check=require_pricing_check, now=now)


def begin_submit(store: StateStore, digest: str, req: dict | None = None, *,
                 require_pricing_check: bool = True,
                 now: float | None = None) -> dict:
    """真实调用 create-order **之前**调用，登记 submitted。

    🔒 本函数**内部强制执行完整闸门**（指纹 + 用户确认 + 状态 + 算价覆盖），
    不依赖调用方是否先跑过 preflight()。

    TOCTOU：闸门通过后，状态转换在同一 read-modify-write 内复校验期望状态，
    若期间状态已被改动则抛 ConcurrentTransition，不会带着过期判断写入 submitted。
    """
    gate = _full_submit_gate(store, digest, req,
                             require_pricing_check=require_pricing_check, now=now)
    if not gate["allowed"]:
        return {
            "ok": False,
            "reason": gate["message"],
            "gate": gate,
            "reconcile": gate["guard"].get("reconcile"),
            "note": "begin_submit 内部强制执行完整提交闸门，"
                    "不依赖调用方是否已调用 preflight()",
        }
    try:
        rec = mark_submitted(store, digest, expected_status=USER_CONFIRMED, now=now)
    except ConcurrentTransition as e:
        return {"ok": False, "toctou": True, "reason": str(e),
                "reconcile": reconcile_plan(store, digest),
                "note": "校验通过后状态被改动，已中止提交（不得重试）"}
    return {"ok": True, "status": rec["status"], "attempts": rec["attempts"],
            "note": "已登记 submitted；调用后必须立即 finish_* 落地结果"}


def finish_unknown(store: StateStore, digest: str, reason: str) -> dict:
    rec = mark_unknown(store, digest, reason)
    plan = reconcile_plan(store, digest)
    return {"ok": True, "status": rec["status"],
            "message": "结果未知，已禁止自动重试", "reconcile": plan}


def finish_confirmed(store: StateStore, digest: str, order_id: str) -> dict:
    rec = mark_confirmed(store, digest, order_id)
    return {"ok": True, "status": rec["status"], "order_id": order_id}


def finish_failed(store: StateStore, digest: str, message: str) -> dict:
    rec = mark_failed(store, digest, message)
    return {"ok": True, "status": rec["status"], "message": message,
            "note": "已确认为失败；重新提交需用户新的明确授权（authorize_retry）"}


def authorize_retry(store: StateStore, digest: str, note: str = "") -> dict:
    rec = authorize_resubmit(store, digest, note)
    return {"ok": True, "resubmit_authorized": rec["resubmit_authorized"],
            "note": "已授权一次重新提交（调用方仍须先排除重复订单风险）"}


def reconcile(store: StateStore, digest: str) -> dict:
    return reconcile_plan(store, digest)


# ---- 结果解析 --------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def parse_order_result(raw: dict) -> dict:
    """解析 create-order / query-order 的返回，提取可确认的字段。

    找不到的字段一律标 unknown，**不虚构**。
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    d = data if isinstance(data, dict) else (raw if isinstance(raw, dict) else {})

    order_id = d.get("orderId") or d.get("orderNo") or None

    # 支付入口：尽力在返回里找 http(s) 链接；找不到就如实说未知
    pay_urls: list[str] = []
    for k, v in d.items():
        if isinstance(v, str) and _URL_RE.search(v):
            lk = k.lower()
            if "pay" in lk or "url" in lk or "qr" in lk or "link" in lk:
                pay_urls.append(_URL_RE.search(v).group(0))
    if not pay_urls:
        blob = _dumps(d)
        found = _URL_RE.findall(blob)
        pay_urls = [u for u in found if any(t in u.lower() for t in ("pay", "cashier", "qr", "order"))][:3]

    status = interpret_order_status(raw)
    deadline = payment_deadline(raw)
    node = _status_node(raw)

    return {
        "success": bool(raw.get("success", order_id is not None)),
        "order_id": order_id,
        "pay_urls": pay_urls,
        "pay_entry_known": bool(pay_urls),
        "pay_entry_note": (PAY_ENTRY_VERIFIED_NOTE if pay_urls else PAY_ENTRY_FALLBACK_NOTE),
        "status": status,
        "payment_deadline": deadline,
        "unknown_fields": ([k for k in ("orderId",) if not d.get(k)]
                           + [k for k in ("orderStatus", "expirePayTime") if not node.get(k)]),
        "raw_keys": sorted(d.keys()),
    }


def _dumps(x) -> str:
    import json
    try:
        return json.dumps(x, ensure_ascii=False)
    except Exception:
        return str(x)
