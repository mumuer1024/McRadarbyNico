"""订单防重与异常恢复（P0-C）。

短时效本地状态记录，至少包含：
- 请求指纹 digest
- 提交时间
- 提交状态：prepared / submitted / unknown / confirmed / failed
- 已知订单 ID
- 订单查询所需的最小关联信息（门店 + 商品编码 + 数量，不含敏感信息）

硬约束：
- 出现 unknown → **不自动再次调用 create-order**
  → 先用 order-list / query-order 核对
  → 无法确定则如实报告不确定性
  → 只有排除重复风险 + 用户新的明确授权，才允许再次提交

⚠️ 本地指纹只在同一设备的同一数据目录内有效，
**不提供跨设备 / 跨进程的全局幂等保证**，不得对外声称绝对防重。
"""

from __future__ import annotations

import time
from typing import Any

from .state import StateStore, iso_ts, scrub
from .tool_risk import reconcile_tool_for

__all__ = [
    "PREPARED", "AWAITING_CONFIRM", "USER_CONFIRMED",
    "SUBMITTED", "UNKNOWN", "CONFIRMED", "FAILED",
    "TRUST_BOUNDARY", "ConcurrentTransition",
    "record_key",
    "get_record",
    "prepare",
    "confirm",
    "revoke_confirmation",
    "confirmation_status",
    "may_submit",
    "mark_submitted",
    "mark_unknown",
    "mark_confirmed",
    "mark_failed",
    "authorize_resubmit",
    "reconcile_plan",
    "gc",
    "DISCLAIMER",
]

PREPARED = "awaiting_confirm"  # 已准备，**等待用户最终确认**（不可提交）
AWAITING_CONFIRM = PREPARED    # 可读性别名
USER_CONFIRMED = "user_confirmed"  # 用户已明确确认 → 才允许提交
SUBMITTED = "submitted"      # 已提交（等待结果）
UNKNOWN = "unknown"          # 结果未知
CONFIRMED = "confirmed"      # 已确认创建
FAILED = "failed"            # 已确认失败

# 信任边界声明（必须随状态机一起对外说明，不得省略）
TRUST_BOUNDARY = (
    "⚠️ 信任边界：Python 状态机只能记录「宿主声明已收到用户确认」这一事实，"
    "**无法独立证明用户的真实意图**。用户确认必须由宿主（Skill）在收到用户明确的"
    "下单授权后调用 confirm() 写入；禁止由模型推断、推测或任何自动流程代替。"
    "本模块的作用是让「未确认」这件事可验证、可拦截，而不是证明意图本身。"
)

DISCLAIMER = (
    "本地指纹仅在同一设备、同一数据目录内有效，不提供跨设备/跨进程的全局幂等保证，"
    "不能声称绝对防重。真实防重以服务端 order-list / query-order 核对为准。"
)


class ConcurrentTransition(RuntimeError):
    """状态在「校验之后、写入之前」发生了变化。

    TOCTOU：完整闸门（preflight / begin_submit）通过之后，记录仍可能被另一进程改动。
    因此状态转换必须在**同一次 read-modify-write** 里复校验期望状态，不符则抛出本异常，
    绝不带着过期的判断写入 submitted。
    """

DEFAULT_TTL = 30 * 60      # 防重时间窗 30 分钟
RESOLVED_TTL = 6 * 60 * 60  # 已解决记录保留 6 小时后清理
CONFIRM_TTL = 15 * 60      # 用户确认有效期：超时需重新展示摘要并重新确认


def record_key(digest: str) -> str:
    return f"order_{digest}"


def _mk(digest: str, request: dict, now: float | None = None) -> dict:
    now = now or time.time()
    items = request.get("items") or []
    return {
        "digest": digest,
        "status": PREPARED,
        "created_at": now,
        "updated_at": now,
        "submitted_at": None,
        "order_id": None,
        "attempts": 0,
        "resubmit_authorized": False,
        # 用户确认（P0：确认必须是可验证的状态，而不只是一句提示词）
        "confirmed_at": None,
        "confirm_note": None,
        "store_code": request.get("storeCode"),
        "be_type": request.get("beType"),
        "order_type": request.get("orderType"),
        # 核对所需最小关联信息（不含金额以外的敏感数据）
        "item_codes": sorted(
            f"{i.get('productCode')}x{i.get('quantity', 1)}" for i in items
        ),
        "est_payable_fen": request.get("_est_payable_fen"),
        "last_error": None,
        "history": [],
    }


def get_record(store: StateStore, digest: str) -> dict | None:
    return store.read(record_key(digest))


def prepare(store: StateStore, digest: str, request: dict) -> dict:
    """登记一次待确认的订单请求。若已有记录则保留历史，重置为 prepared（仅当未提交过）。"""
    rec = get_record(store, digest)
    if rec and rec.get("status") in (SUBMITTED, UNKNOWN, CONFIRMED):
        # 已经进入提交流程，不允许悄悄重置
        return rec
    if rec:
        rec["status"] = PREPARED
        rec["updated_at"] = time.time()
        rec["resubmit_authorized"] = False
        rec["confirmed_at"] = None
        rec["confirm_note"] = None
        rec["last_error"] = None
        store.write(record_key(digest), scrub(rec))
        return rec
    new = _mk(digest, request)
    store.write(record_key(digest), scrub(new))
    return new


# ---- 用户确认（P0-2）-------------------------------------------------------

def confirm(
    store: StateStore,
    digest: str,
    request: dict,
    *,
    user_confirmed: bool,
    note: str = "",
    now: float | None = None,
) -> dict:
    """记录「用户已明确确认」这一状态。

    只有宿主在收到用户明确的下单授权后才能以 user_confirmed=True 调用。
    确认绑定当前订单指纹：请求内容变化 → digest 变化 → 是另一条记录，
    原确认不会误作用于新请求；反之这里也校验传入 request 确实对应该 digest。
    """
    now = now or time.time()
    rec = get_record(store, digest)
    if rec is None:
        return {"ok": False, "reason": "没有对应的订单记录，请先展示摘要并准备订单",
                "status": None, "note": TRUST_BOUNDARY}
    if not user_confirmed:
        return {"ok": False, "reason": "未收到用户明确确认，不得进入可提交状态",
                "status": rec.get("status"), "note": TRUST_BOUNDARY}

    status = rec.get("status")
    if status in (SUBMITTED, UNKNOWN, CONFIRMED):
        return {"ok": False,
                "reason": f"当前状态为 {status}，禁止在此期间重新确认/提交",
                "status": status, "reconcile": list(reconcile_tool_for("create-order")),
                "note": TRUST_BOUNDARY}

    # 确认必须绑定当前指纹：请求与 digest 不符 → 拒绝
    actual = _digest_of(request)
    if actual != digest:
        return {"ok": False,
                "reason": f"确认内容与订单指纹不一致（digest 不匹配），"
                          f"必须重新展示摘要并重新确认",
                "status": status, "expected": digest, "actual": actual,
                "note": TRUST_BOUNDARY}

    rec["status"] = USER_CONFIRMED
    rec["confirmed_at"] = now
    rec["confirm_note"] = note or "宿主声明已收到用户明确确认"
    rec["updated_at"] = now
    rec["resubmit_authorized"] = True
    rec["last_error"] = None
    store.write(record_key(digest), scrub(rec))
    return {"ok": True, "status": USER_CONFIRMED, "confirmed_at": now,
            "record": rec, "note": TRUST_BOUNDARY}


def revoke_confirmation(store: StateStore, digest: str) -> dict:
    """订单内容发生变化 → 撤销既有确认（调用方需重新展示摘要）。"""
    rec = get_record(store, digest)
    if rec is None:
        return {"ok": False, "reason": "记录不存在"}
    if rec.get("status") == USER_CONFIRMED:
        rec["status"] = PREPARED
        rec["confirmed_at"] = None
        rec["confirm_note"] = None
        rec["resubmit_authorized"] = False
        rec["updated_at"] = time.time()
        store.write(record_key(digest), scrub(rec))
    return {"ok": True, "status": rec.get("status")}


def confirmation_status(store: StateStore, digest: str,
                        ttl: float = CONFIRM_TTL, now: float | None = None) -> dict:
    """确认状态查询：是否已确认、是否过期。"""
    now = now or time.time()
    rec = get_record(store, digest)
    if rec is None:
        return {"confirmed": False, "status": None, "reason": "记录不存在"}
    status = rec.get("status")
    if status != USER_CONFIRMED:
        return {"confirmed": False, "status": status,
                "reason": f"当前状态 {status} 不是已确认状态"}
    age = now - (rec.get("confirmed_at") or 0)
    if age > ttl:
        return {"confirmed": False, "status": status, "expired": True,
                "reason": f"确认已超过 {int(ttl)}s，需重新展示摘要并重新确认"}
    return {"confirmed": True, "status": status, "expired": False,
            "age_seconds": int(age), "confirmed_at": rec.get("confirmed_at")}


def _digest_of(request: dict) -> str:
    from .summary import request_digest
    return request_digest(request)


def may_submit(store: StateStore, digest: str, ttl: float = DEFAULT_TTL,
               confirm_ttl: float = CONFIRM_TTL, now: float | None = None) -> dict:
    """提交前必问：现在能不能提交？（状态 + 用户确认 + 防重三重校验）"""
    now = now or time.time()
    rec = get_record(store, digest)
    if rec is None:
        return {"allowed": False, "reason": "没有对应的 prepared 记录，请先走摘要确认流程",
                "status": None, "reconcile": []}

    status = rec.get("status")

    if status == PREPARED:
        return {"allowed": False,
                "reason": "订单已准备但**尚未收到用户最终确认**，禁止提交。"
                          "必须先展示完整摘要并取得用户明确确认（confirm）",
                "status": status, "reconcile": []}

    if status == USER_CONFIRMED:
        age = now - (rec.get("confirmed_at") or 0)
        if age > confirm_ttl:
            return {"allowed": False,
                    "reason": f"用户确认已超过 {int(confirm_ttl)}s，需重新展示摘要并重新确认",
                    "status": status, "reconcile": []}
        return {"allowed": True, "reason": "用户已确认，可以提交（仅此一次）",
                "status": status, "reconcile": []}

    if status == SUBMITTED:
        age = now - (rec.get("submitted_at") or now)
        if age < ttl:
            return {"allowed": False, "reason": f"已在 {age:.0f}s 前提交过，禁止重复提交",
                    "status": status, "reconcile": list(reconcile_tool_for("create-order"))}
        return {"allowed": False, "reason": "已提交但超过防重时间窗且结果未确认，必须先核对",
                "status": status, "reconcile": list(reconcile_tool_for("create-order"))}

    if status == UNKNOWN:
        return {"allowed": False,
                "reason": "上一次提交结果未知，禁止自动重试。必须先核对订单列表/详情，"
                          "并由用户再次明确授权后才可提交",
                "status": status,
                "reconcile": list(reconcile_tool_for("create-order"))}

    if status == CONFIRMED:
        return {"allowed": False,
                "reason": f"订单已确认创建（orderId={rec.get('order_id')}），禁止重复提交",
                "status": status, "reconcile": list(reconcile_tool_for("create-order"))}

    if status == FAILED:
        if rec.get("resubmit_authorized"):
            return {"allowed": True, "reason": "已确认为失败，且用户已授权重新提交",
                    "status": status, "reconcile": []}
        return {"allowed": False,
                "reason": "上次提交已确认为失败；重新提交需要用户新的明确授权",
                "status": status, "reconcile": list(reconcile_tool_for("create-order"))}

    return {"allowed": False, "reason": f"未知状态 {status}，禁止提交", "status": status, "reconcile": []}


def _mutate(store: StateStore, digest: str, **fields) -> dict:
    rec = get_record(store, digest)
    if rec is None:
        raise KeyError(f"记录不存在：{digest}")
    rec.update(fields)
    rec["updated_at"] = time.time()
    rec.setdefault("history", []).append({
        "at": iso_ts(rec["updated_at"]),
        "to": rec.get("status"),
        "error": rec.get("last_error"),
    })
    store.write(record_key(digest), scrub(rec))
    return rec


def mark_submitted(store: StateStore, digest: str, *,
                   expected_status: str | None = USER_CONFIRMED,
                   now: float | None = None) -> dict:
    """登记 submitted。

    默认**只接受** USER_CONFIRMED → SUBMITTED，且在**同一次 read-modify-write**
    内复校验状态（TOCTOU 防护）：校验通过后若状态已被改动，抛 ConcurrentTransition
    而不是照写 submitted。
    """
    now = now or time.time()

    def fn(cur):
        if cur is None:
            raise KeyError(f"记录不存在：{digest}")
        status = cur.get("status")
        if expected_status is not None and status != expected_status:
            raise ConcurrentTransition(
                f"校验通过后状态已变化：期望 {expected_status}，实际 {status}；"
                f"未写入 submitted")
        cur["status"] = SUBMITTED
        cur["submitted_at"] = now
        cur["updated_at"] = now
        cur["attempts"] = int(cur.get("attempts", 0)) + 1
        cur.setdefault("history", []).append(
            {"at": iso_ts(now), "to": SUBMITTED, "error": cur.get("last_error")})
        return scrub(cur)

    return store.update(record_key(digest), fn)


def mark_unknown(store: StateStore, digest: str, reason: str) -> dict:
    """超时 / 断连 / 5xx / 响应不明 → 标记 unknown，禁止自动重试。"""
    return _mutate(store, digest, status=UNKNOWN, last_error=reason)


def mark_confirmed(store: StateStore, digest: str, order_id: str) -> dict:
    return _mutate(store, digest, status=CONFIRMED, order_id=order_id, last_error=None)


def mark_failed(store: StateStore, digest: str, message: str) -> dict:
    return _mutate(store, digest, status=FAILED, last_error=message, resubmit_authorized=False)


def authorize_resubmit(store: StateStore, digest: str, note: str = "",
                       now: float | None = None) -> dict:
    """用户在**已排除重复订单风险后**明确授权再次提交。

    再次提交等同一次新的确认：状态回到 USER_CONFIRMED 并记录新的确认时间。
    """
    now = now or time.time()
    rec = get_record(store, digest)
    if rec is None:
        raise KeyError(f"记录不存在：{digest}")
    if rec.get("status") not in (FAILED,):
        return {"status": rec.get("status"), "resubmit_authorized": False,
                "reason": f"仅 FAILED 状态可授权重新提交，当前为 {rec.get('status')}"}
    rec["status"] = USER_CONFIRMED
    rec["resubmit_authorized"] = True
    rec["confirmed_at"] = now
    rec["confirm_note"] = note or "用户授权重新提交"
    rec["last_error"] = note or "用户授权重新提交"
    rec["updated_at"] = now
    rec.setdefault("history", []).append(
        {"at": iso_ts(now), "to": USER_CONFIRMED, "error": rec["last_error"]})
    store.write(record_key(digest), scrub(rec))
    return rec


def reconcile_plan(store: StateStore, digest: str) -> dict:
    """结果未知时的核对方案：告诉调用方该查什么、怎么判断。"""
    rec = get_record(store, digest)
    if rec is None:
        return {"error": "记录不存在"}
    return {
        "status": rec.get("status"),
        "steps": [
            "1) 调用 order-list，按 createTime 与商品编码核对是否已出现新订单",
            "2) 若找到疑似订单，用 query-order 确认商品与状态",
            "3) 若确认已创建 → mark_confirmed",
            "4) 若确认未创建（且服务端明确报错）→ mark_failed",
            "5) 若仍无法确定 → 保持 unknown，如实告知用户，不得再次提交",
        ],
        "match_hints": {
            "store_code": rec.get("store_code"),
            "item_codes": rec.get("item_codes"),
            "submitted_at": iso_ts(rec["submitted_at"]) if rec.get("submitted_at") else None,
        },
        "disclaimer": DISCLAIMER,
    }


def gc(store: StateStore, ttl: float = RESOLVED_TTL) -> int:
    """清理过期记录。返回清理数量。"""
    import re
    removed = 0
    now = time.time()
    try:
        files = list(store.dir.glob("order_*.json"))
    except OSError:
        return 0
    for p in files:
        try:
            import json
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = rec.get("status")
        age = now - (rec.get("updated_at") or now)
        keep = status in (SUBMITTED, UNKNOWN) or age < ttl
        if not keep:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
