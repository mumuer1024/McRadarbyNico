"""一键领券编排（4.4）。

流程：
1. available-coupons 展示可领清单
2. 明确告知 auto-bind-coupons 会尝试领取**全部**当前可领券，不能指定单张
3. 用户明确确认
4. 调用一次 auto-bind-coupons
5. 重新查询券池
6. 对比前后变化并如实汇报

对比规则：
- 券池存在**同名重复批次**，不能用券名做唯一键
- 若接口没有足够唯一标识，只报告可可靠确认的变化，**不虚构精确领取数量**

⚠️ 本模块不发起真实领券调用；真实调用由宿主在前置闸门通过后执行一次。
"""

from __future__ import annotations

import time

from .coupon import CouponPool, diff_coupon_pools
from .state import StateStore, iso_ts, scrub
from .tool_risk import reconcile_tool_for, retry_policy

__all__ = [
    "BIND_KEY",
    "BATCH_NOTICE",
    "TRUST_BOUNDARY_BIND",
    "CONFIRM_TTL",
    "scope_digest",
    "plan_bind",
    "prepare_bind",
    "confirm_bind",
    "revoke_bind_confirmation",
    "preflight_bind",
    "begin_bind",
    "finish_unknown",
    "finish_done",
    "compare_pools",
]

BIND_KEY = "coupon_bind"

BATCH_NOTICE = (
    "⚠️ `auto-bind-coupons` 是**批量领取**接口：它会尝试领取你当前所有可领取的优惠券，"
    "**麦当劳 MCP 没有提供单张领取接口**，无法只领其中某一张。"
)

TRUST_BOUNDARY_BIND = (
    "⚠️ 信任边界：本模块只能记录「宿主声明已收到用户确认」这一事实，"
    "**无法独立证明用户真实意图**。确认必须由宿主在用户明确同意后调用 confirm_bind() 写入。"
)

# 确认有效期：超时需重新展示可领清单并重新确认
CONFIRM_TTL = 15 * 60


def scope_digest(pool: CouponPool) -> str:
    """当次可领取范围的快照指纹。

    ⚠️ 券池存在**同名重复批次**，不能用 title 做唯一键；
    这里用 uid 集合 + 数量构成范围指纹，券池变化会让确认失效。
    """
    import hashlib

    claimable = pool.claimable()
    ids = sorted(str(c.uid) for c in claimable)
    return hashlib.sha256(("|".join(ids) + f"#n={len(ids)}").encode("utf-8")).hexdigest()


def _new_token(scope: str, now: float) -> str:
    import hashlib
    import secrets

    raw = f"{scope}|{now}|{secrets.token_hex(8)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def plan_bind(pool: CouponPool) -> dict:
    """第 1 步：展示可领清单 + 批量说明。"""
    claimable = pool.claimable()
    return {
        "claimable": [{"title": c.title, "status": c.status, "uid": c.uid} for c in claimable],
        "count": len(claimable),
        "notice": BATCH_NOTICE,
        "confirm_prompt": (f"确认领取全部 {len(claimable)} 张可领券？" if claimable
                           else "当前没有可领取的优惠券，无需领取。"),
        "can_proceed": bool(claimable),
        "warnings": list(pool.parse_warnings),
        "note": "领取数量以服务端实际结果为准，本清单仅为领取前的快照",
    }


def _revoke(rec: dict) -> None:
    """就地作废既有确认（状态回到 prepared，凭证清空）。"""
    rec["status"] = "prepared"
    rec["confirm_nonce"] = None
    rec["confirmed_at"] = None


def prepare_bind(store: StateStore, pool: CouponPool) -> dict:
    """登记一次待确认的领券请求（此时**尚未确认**，不可提交）。

    登记时把当次展示的可领范围快照写进记录；确认必须绑定**这个快照**。
    券池变化后重新登记 = 重新展示范围，旧确认随之作废。
    """
    now = time.time()
    prev = store.read(BIND_KEY)
    if prev and prev.get("status") in ("submitted", "unknown"):
        return {"ok": False, "status": prev.get("status"),
                "reason": f"当前状态为 {prev.get('status')}：结果确认前不得重新登记，"
                          f"必须先重查券池确认上次结果",
                "reconcile": list(reconcile_tool_for("auto-bind-coupons")),
                "notice": BATCH_NOTICE}
    scope = scope_digest(pool)
    rec = {
        "status": "prepared",
        "created_at": now,
        "updated_at": now,
        "attempts": 0,
        "before": pool.to_dict(),
        "scope_digest": scope,
        "confirm_nonce": None,      # ⚠️ 不能叫 *_token：state.scrub 会剔除含 token 的键
        "confirmed_at": None,
        "resubmit_authorized": False,
        "last_error": None,
    }
    store.write(BIND_KEY, scrub(rec))
    return {"ok": True, "record": rec, "notice": BATCH_NOTICE,
            "scope_digest": scope, "status": "prepared",
            "note": "已登记待确认领券请求；必须展示可领清单并取得用户明确确认"}


def confirm_bind(store: StateStore, pool: CouponPool, *, user_confirmed: bool,
                 reauthorize: bool = False, note: str = "",
                 now: float | None = None) -> dict:
    """P0-3：把「用户已明确同意批量领取」变成可验证状态，并签发一次性凭证。

    - 确认绑定当次展示的领取范围（scope_digest）
    - 🔴 券池范围变化 → **拒绝签发凭证并作废旧确认**（返回 ok=False /
      requires_redisplay=True），宿主必须重新展示范围后重新确认，不得静默续期
    - 上次结果 unknown / failed / done → 必须 reauthorize=True（用户新的明确授权）
    """
    now = now or time.time()
    rec = store.read(BIND_KEY)
    policy = retry_policy("auto-bind-coupons")
    if rec is None:
        return {"ok": False, "reason": "没有待确认的领券请求，请先展示可领券清单",
                "notice": TRUST_BOUNDARY_BIND,
                "retry_policy": {"max_auto_retries": policy.max_auto_retries}}
    if not user_confirmed:
        return {"ok": False, "reason": "未收到用户明确确认，禁止调用 auto-bind-coupons",
                "notice": TRUST_BOUNDARY_BIND,
                "retry_policy": {"max_auto_retries": policy.max_auto_retries}}

    status = rec.get("status")
    if status == "submitted":
        return {"ok": False, "reason": "已提交过，禁止重复领取；结果确认前不得再次提交",
                "reconcile": list(reconcile_tool_for("auto-bind-coupons")),
                "retry_policy": {"max_auto_retries": policy.max_auto_retries}}
    if status in ("unknown", "failed", "done") and not reauthorize:
        return {"ok": False,
                "reason": f"当前状态为 {status}，再次领取需用户新的明确授权（reauthorize=True）"
                          f"且必须先重查券池确认上次结果",
                "reconcile": list(reconcile_tool_for("auto-bind-coupons")),
                "retry_policy": {"max_auto_retries": policy.max_auto_retries}}

    scope = scope_digest(pool)
    previous_scope = rec.get("scope_digest")
    scope_changed = previous_scope != scope
    if scope_changed:
        # 🔴 券池范围已变化：旧确认**立即作废**，不得沿用新快照静默续期。
        # 宿主必须先用 prepare_bind 重新登记、向用户重新展示领取范围，
        # 再取得用户新的明确确认，才能签发新的提交凭证。
        _revoke(rec)
        rec["updated_at"] = now
        rec["last_error"] = "可领取范围变化，确认已作废"
        store.write(BIND_KEY, scrub(rec))
        return {
            "ok": False, "status": rec.get("status"), "nonce": None,
            "scope_changed": True, "requires_redisplay": True,
            "previous_scope": previous_scope, "current_scope": scope,
            "reason": ("可领取范围已变化（券池快照不一致），旧确认已作废。"
                       "必须先用 prepare-bind 重新登记、向用户重新展示领取范围，"
                       "再取得用户新的明确确认"),
            "notice": BATCH_NOTICE + "\n" + TRUST_BOUNDARY_BIND,
            "retry_policy": {"max_auto_retries": policy.max_auto_retries},
        }

    rec["status"] = "confirmed"
    rec["confirm_nonce"] = _new_token(scope, now)
    rec["confirmed_at"] = now
    rec["updated_at"] = now
    rec["resubmit_authorized"] = bool(reauthorize)
    rec["last_error"] = None
    store.write(BIND_KEY, scrub(rec))
    return {
        "ok": True, "status": "confirmed", "nonce": rec["confirm_nonce"],
        "scope_digest": scope, "scope_changed": scope_changed,
        "confirmed_at": now, "notice": BATCH_NOTICE + "\n" + TRUST_BOUNDARY_BIND,
        "retry_policy": {"max_auto_retries": policy.max_auto_retries,
                         "on_unknown_result": policy.on_unknown_result},
    }


def revoke_bind_confirmation(store: StateStore) -> dict:
    """券池或状态变化 → 撤销既有确认。"""
    rec = store.read(BIND_KEY)
    if rec is None:
        return {"ok": False, "reason": "记录不存在"}
    if rec.get("status") == "confirmed":
        rec["status"] = "prepared"
        rec["confirm_nonce"] = None
        rec["confirmed_at"] = None
        rec["updated_at"] = time.time()
        store.write(BIND_KEY, scrub(rec))
    return {"ok": True, "status": rec.get("status")}


def preflight_bind(store: StateStore, *, nonce: str | None = None,
                   user_confirmed: bool = True, now: float | None = None) -> dict:
    """提交前的闸门：必须有记录 + 用户已确认 + 凭证匹配 + 确认未过期 + 上次非 unknown。"""
    now = now or time.time()
    rec = store.read(BIND_KEY)
    policy = retry_policy("auto-bind-coupons")
    rp = {"max_auto_retries": policy.max_auto_retries}

    if rec is None:
        return {"allowed": False, "reason": "没有待确认的领券请求，请先展示可领券清单",
                "retry_policy": rp}
    if not user_confirmed:
        return {"allowed": False, "reason": "未收到用户明确确认，禁止调用 auto-bind-coupons",
                "retry_policy": rp, "notice": TRUST_BOUNDARY_BIND}

    status = rec.get("status")
    if status == "unknown":
        return {"allowed": False,
                "reason": "上次领券结果未知，禁止自动重试；需先重查券池确认结果，"
                          "并由用户再次明确授权",
                "reconcile": list(reconcile_tool_for("auto-bind-coupons")),
                "retry_policy": rp}
    if status != "confirmed":
        return {"allowed": False,
                "reason": f"当前状态为 {status}：只有经过 confirm_bind 的确认状态才可提交，"
                          f"不得直接进入 submitted",
                "retry_policy": rp}
    if not nonce or nonce != rec.get("confirm_nonce"):
        return {"allowed": False,
                "reason": "缺少或错误的确认凭证（nonce），禁止提交",
                "retry_policy": rp}
    age = now - (rec.get("confirmed_at") or 0)
    if age > CONFIRM_TTL:
        return {"allowed": False,
                "reason": f"确认已超过 {int(CONFIRM_TTL)}s，需重新展示可领清单并重新确认",
                "retry_policy": rp}
    return {"allowed": True, "reason": "已确认，可以领取（仅此一次）",
            "retry_policy": {"max_auto_retries": policy.max_auto_retries,
                             "on_unknown_result": policy.on_unknown_result}}


def begin_bind(store: StateStore, *, nonce: str | None = None,
               user_confirmed: bool = True, now: float | None = None) -> dict:
    """登记 submitted。**必须先通过 preflight_bind**，不得被直接调用绕过。"""
    pf = preflight_bind(store, nonce=nonce, user_confirmed=user_confirmed, now=now)
    if not pf["allowed"]:
        return {"ok": False, "reason": pf["reason"], "preflight": pf,
                "reconcile": pf.get("reconcile")}
    rec = store.read(BIND_KEY) or {}
    rec["status"] = "submitted"
    rec["submitted_at"] = now or time.time()
    rec["updated_at"] = now or time.time()
    rec["attempts"] = rec.get("attempts", 0) + 1
    rec["confirm_nonce"] = None       # 凭证一次性，提交后立即作废
    store.write(BIND_KEY, scrub(rec))
    return {"ok": True, "status": "submitted", "attempts": rec["attempts"],
            "note": "已登记 submitted；调用后必须立即 finish_* 落地结果"}


def finish_unknown(store: StateStore, reason: str) -> dict:
    rec = store.read(BIND_KEY) or {}
    rec["status"] = "unknown"
    rec["last_error"] = reason
    rec["updated_at"] = time.time()
    store.write(BIND_KEY, scrub(rec))
    return {"ok": True, "status": "unknown",
            "message": "领券结果未知，禁止自动再次领取",
            "reconcile": {
                "steps": [
                    "1) 重新调用 available-coupons 查看当前可领券",
                    "2) 调用 query-my-coupons 查看卡包变化",
                    "3) 若确认已领取成功 → finish_done",
                    "4) 若确认未领取且服务端明确报错 → 由用户重新授权后才可再试",
                    "5) 仍无法确定 → 保持 unknown，如实告知用户",
                ],
                "tools": list(reconcile_tool_for("auto-bind-coupons")),
            }}


def finish_done(store: StateStore, after: CouponPool) -> dict:
    rec = store.read(BIND_KEY) or {}
    rec["status"] = "done"
    rec["updated_at"] = time.time()
    rec["after"] = after.to_dict()
    store.write(BIND_KEY, scrub(rec))
    before = CouponPool(
        my=[_coupon(d) for d in (rec.get("before", {}).get("my") or [])],
        store=[_coupon(d) for d in (rec.get("before", {}).get("store") or [])],
        available=[_coupon(d) for d in (rec.get("before", {}).get("available") or [])],
    )
    return {"ok": True, "status": "done", "diff": compare_pools(before, after)}


def compare_pools(before: CouponPool, after: CouponPool) -> dict:
    return diff_coupon_pools(before, after)


def _coupon(d: dict):
    from .models import CouponInfo
    return CouponInfo(**{k: v for k, v in d.items() if k in CouponInfo.__dataclass_fields__})
