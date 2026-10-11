"""测试公共工具：临时数据目录、Mock MCP、宿主行为模拟。

Mock MCP 的目的：**在不触碰真实账户的前提下**，验证写操作编排是否
真的被闸门挡住（尤其是"绝不自动重试"）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))            # mcdradar 包

from mcdradar import coupon_bind, order_flow  # noqa: E402
from mcdradar.state import StateStore  # noqa: E402
from mcdradar.tool_risk import assert_may_call  # noqa: E402


def tmp_store() -> StateStore:
    d = tempfile.mkdtemp(prefix="mcdradar-test-")
    return StateStore(d)


# --------------------------------------------------------------------------
# Mock MCP：只模拟写操作，记录调用次数
# --------------------------------------------------------------------------

class MockMCP:
    """模拟麦当劳 MCP 的写操作。

    behavior:
      - "success"  → 返回成功（含 orderId）
      - "5xx"      → 服务端错误
      - "timeout"  → 超时（抛 TimeoutError）
      - "unclear"  → 响应不明（success 缺失、无 orderId）
    """

    def __init__(self):
        self.call_log: list[dict] = []

    def _record(self, tool: str, payload: dict) -> None:
        self.call_log.append({"tool": tool, "payload": payload})

    @property
    def call_count(self) -> int:
        return len(self.call_log)

    def counts(self, tool: str) -> int:
        return sum(1 for c in self.call_log if c["tool"] == tool)

    # ---- create-order ----
    def create_order(self, request: dict, behavior: str = "success"):
        self._record("create-order", request)
        if behavior == "timeout":
            raise TimeoutError("connection timed out")
        if behavior == "5xx":
            return {"success": False, "code": 500, "message": "internal server error"}
        if behavior == "unclear":
            return {"code": 200, "message": "ok"}     # 无 success、无 data
        return {
            "success": True, "code": 200,
            "data": {
                "orderId": "MOCK_ORDER_0001",
                "orderStatus": "待支付",
                "status": "1",
                "expirePayTime": "2026-10-10 12:15:00",
                "realTotalAmount": "26.5",
            },
        }

    # ---- auto-bind-coupons ----
    def auto_bind_coupons(self, behavior: str = "success"):
        self._record("auto-bind-coupons", {})
        if behavior == "timeout":
            raise TimeoutError("connection timed out")
        if behavior == "unclear":
            return {"code": 200}
        if behavior == "fail":
            return {"success": False, "message": "领取失败"}
        return {"success": True, "message": "领取成功"}


# --------------------------------------------------------------------------
# 算价覆盖校验样本（Mock，仅用于驱动闸门）
# --------------------------------------------------------------------------

def pricing_for(request: dict, price_fen: int = 2650, *, drop: tuple[str, ...] = ()) -> dict:
    """构造一份"覆盖 request 全部商品"的 calculate-price 返回**样本**。

    ⚠️ 这是 Mock 数据，不是真实 MCP 返回；真实集成验证见 tests/fixtures/。
    drop 可用于模拟服务端静默丢弃某些商品。
    """
    product_list = [
        {"productCode": i["productCode"], "quantity": int(i.get("quantity", 1))}
        for i in request.get("items", [])
        if str(i["productCode"]) not in drop
    ]
    return {
        "success": True, "code": 200,
        "data": {
            "price": price_fen, "originalPrice": price_fen, "discount": 0,
            "productList": product_list,
            "takeWayList": [{"code": "locker-out", "title": "外带·店外取餐柜"}],
        },
    }


def prepare_confirm(store: StateStore, request: dict, *,
                    est_payable_fen: int | None = 2650,
                    pricing: dict | None = None,
                    user_confirmed: bool = True) -> dict:
    """测试用：走完 准备 → 算价覆盖校验 → 用户确认 三步，返回 {digest, ...}。"""
    r = order_flow.prepare_order(store, request, est_payable_fen)
    if not r.get("ok"):
        return r
    digest = r["digest"]
    check = order_flow.record_pricing_check(
        store, digest, request=request,
        pricing=pricing if pricing is not None else pricing_for(request))
    conf = order_flow.confirm_order(store, digest, request, user_confirmed=user_confirmed)
    return {"ok": True, "digest": digest, "check": check, "confirm": conf}


# --------------------------------------------------------------------------
# 宿主行为模拟：正确实现 vs 有重试倾向的错误实现
# --------------------------------------------------------------------------

def host_submit(store: StateStore, digest: str, request: dict, mock: MockMCP,
                behavior: str = "success", real: bool = False) -> dict:
    """宿主**正确**实现：preflight → begin_submit → 调用一次 → 落地结果。"""
    pf = order_flow.preflight(store, digest, request)
    if not pf["allowed"]:
        return {"called": False, "preflight": pf}

    b = order_flow.begin_submit(store, digest, request)
    if not b.get("ok"):
        return {"called": False, "preflight": b}
    if real:
        assert_may_call("create-order", real=True)      # 未授权会抛 PermissionError
    try:
        res = mock.create_order(request, behavior=behavior)
    except TimeoutError as e:
        out = order_flow.finish_unknown(store, digest, str(e))
        return {"called": True, "outcome": "unknown", "detail": out}

    parsed = order_flow.parse_order_result(res)

    # 只有**服务端明确返回 success=false** 才算「已确认失败」。
    # success 字段缺失（响应不明）必须判 unknown，不能当成失败 ——
    # 否则会诱导上层"既然失败了就再试一次"，这正是 P0-A 要禁止的行为。
    explicit_failure = res.get("success") is False

    if parsed["success"] and parsed["order_id"]:
        out = order_flow.finish_confirmed(store, digest, parsed["order_id"])
        return {"called": True, "outcome": "confirmed", "detail": out, "parsed": parsed}
    if res.get("code", 200) >= 500:
        out = order_flow.finish_unknown(store, digest, f"服务端错误 {res.get('code')}")
        return {"called": True, "outcome": "unknown", "detail": out}
    if explicit_failure and res.get("message"):
        out = order_flow.finish_failed(store, digest, res["message"])
        return {"called": True, "outcome": "failed", "detail": out}
    out = order_flow.finish_unknown(store, digest, "响应不明：无 success / 无 orderId")
    return {"called": True, "outcome": "unknown", "detail": out}


def host_submit_retry_happy(store: StateStore, digest: str, request: dict, mock: MockMCP,
                            behavior: str = "timeout", attempts: int = 3) -> dict:
    """宿主**错误**实现：遇到失败就想重试 attempts 次。

    用来验证：即使调用方想重试，闸门也必须拦住 —— create_order 只会被调用 1 次。
    """
    outcomes = []
    for _ in range(attempts):
        r = host_submit(store, digest, request, mock, behavior=behavior)
        outcomes.append(r.get("outcome", "blocked"))
        if not r["called"]:
            break
    return {"attempts_requested": attempts,
            "actual_calls": mock.counts("create-order"),
            "outcomes": outcomes}


def host_bind(store: StateStore, mock: MockMCP, *, pool=None,
              user_confirmed: bool = True, nonce: str | None = None,
              reauthorize: bool = False, behavior: str = "success",
              real: bool = False) -> dict:
    """宿主**正确**实现：confirm_bind（用户确认）→ preflight → begin → 调用一次。"""
    if nonce is None:
        c = coupon_bind.confirm_bind(store, pool, user_confirmed=user_confirmed,
                                     reauthorize=reauthorize)
        if not c.get("ok"):
            return {"called": False, "preflight": c}
        nonce = c["nonce"]

    pf = coupon_bind.preflight_bind(store, nonce=nonce, user_confirmed=user_confirmed)
    if not pf["allowed"]:
        return {"called": False, "preflight": pf}
    b = coupon_bind.begin_bind(store, nonce=nonce)
    if not b.get("ok"):
        return {"called": False, "preflight": b}
    if real:
        assert_may_call("auto-bind-coupons", real=True)
    try:
        res = mock.auto_bind_coupons(behavior=behavior)
    except TimeoutError as e:
        return {"called": True, "outcome": "unknown",
                "detail": coupon_bind.finish_unknown(store, str(e))}
    if res.get("success") is True:
        return {"called": True, "outcome": "done", "detail": res}
    if behavior == "fail":
        return {"called": True, "outcome": "failed", "detail": res}
    return {"called": True, "outcome": "unknown",
            "detail": coupon_bind.finish_unknown(store, "响应不明")}


def host_bind_retry_happy(store: StateStore, mock: MockMCP, attempts: int = 3,
                          behavior: str = "timeout", pool=None) -> dict:
    """宿主**错误**实现：想重试 attempts 次，闸门必须拦住。"""
    outcomes = []
    for _ in range(attempts):
        # 注意：这里**故意不传 reauthorize** —— 结果未知后的再次领取必须来自
        # 用户新的明确授权，自动重试拿不到它。
        r = host_bind(store, mock, pool=pool, user_confirmed=True, behavior=behavior)
        outcomes.append(r.get("outcome", "blocked"))
        if not r["called"]:
            break
    return {"attempts_requested": attempts,
            "actual_calls": mock.counts("auto-bind-coupons"),
            "outcomes": outcomes}


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=2, default=str)
