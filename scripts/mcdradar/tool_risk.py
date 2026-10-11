"""MCP 工具风险分类与重试策略（P0-A）。

核心原则：
- 只读工具：允许受控重试（网络抖动、5xx）
- 写入工具：默认**不自动重试**
- create-order：超时 / 断连 / 5xx / 响应不明 → **一律不得再次提交**
- auto-bind-coupons：响应不明 → 先重查券池，**不得自动再次领取**

这一层是代码级约束，不依赖自然语言提示。
所有实际 MCP 调用方（Skill / 未来的客户端）都必须先经过
`retry_policy(tool)` 或 `assert_may_call(tool, phase)`。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ToolRisk",
    "classify",
    "retry_policy",
    "assert_may_call",
    "reconcile_tool_for",
    "WRITE_TOOLS",
    "NO_RETRY_TOOLS",
    "FORBIDDEN_THIS_ROUND",
]

READ = "read"
WRITE = "write"
FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class ToolRisk:
    level: str
    max_auto_retries: int
    on_unknown_result: str
    reconcile_with: tuple[str, ...]


# 只读工具（实测或 Schema 确认为只读）
READ_TOOLS = frozenset({
    "now-time-info",
    "query-nearby-stores",
    "delivery-query-stores",
    "delivery-query-addresses",
    "query-meals",
    "query-meal-detail",
    "calculate-price",          # 实测幂等、无副作用
    "query-store-coupons",
    "query-my-coupons",
    "available-coupons",
    "campaign-calendar",
    "query-my-account",
    "list-nutrition-foods",
    "query-order",
    "order-list",
    "mall-order-list",
    "mall-order-detail",
    "query-promotions",
    "query-lottery-info",
    "query-my-prizes",
    "query-survey-coupon",
    "query-meal-assistance",
    "query-party-city",
    "query-party-store",
    "query-party-store-date",
    "query-party-store-session",
    "mall-points-products",
    "mall-product-detail",
})

# 写入工具：会改变账户 / 订单 / 权益
WRITE_TOOLS = frozenset({
    "create-order",
    "auto-bind-coupons",
    "cancel-order",
    "draw-lottery",
    "mall-create-order",
    "delivery-create-address",
    "party-order-create",
})

# 明确禁止自动重试的工具（响应不明时绝对不能再打一次）
NO_RETRY_TOOLS = frozenset({
    "create-order",
    "auto-bind-coupons",
})

# 本轮（MVP 第一轮开发）禁止真实调用的写操作 —— Mock 验证专用
FORBIDDEN_THIS_ROUND = frozenset({
    "create-order",
    "auto-bind-coupons",
    "cancel-order",
    "draw-lottery",
    "mall-create-order",
    "delivery-create-address",
    "party-order-create",
})

# 默认策略（未在表中登记的工具按最保守处理）
_UNKNOWN_RISK = ToolRisk(level=WRITE, max_auto_retries=0,
                         on_unknown_result="未知工具：不得重试，需人工确认",
                         reconcile_with=())

_POLICIES: dict[str, ToolRisk] = {}


def _register(names, risk: ToolRisk) -> None:
    for n in names:
        _POLICIES[n] = risk


_register(READ_TOOLS, ToolRisk(
    level=READ,
    max_auto_retries=2,
    on_unknown_result="可受控重试（退避）；仍失败则明确报错，不猜测结果",
    reconcile_with=(),
))

_register(["create-order"], ToolRisk(
    level=WRITE,
    max_auto_retries=0,
    on_unknown_result="禁止再次提交。改用 order-list / query-order 核对；无法确定则如实报告不确定性",
    reconcile_with=("order-list", "query-order"),
))

_register(["auto-bind-coupons"], ToolRisk(
    level=WRITE,
    max_auto_retries=0,
    on_unknown_result="禁止再次领取。先重查 available-coupons / query-my-coupons 判断实际结果",
    reconcile_with=("available-coupons", "query-my-coupons"),
))

_register(["cancel-order"], ToolRisk(
    level=WRITE,
    max_auto_retries=0,
    on_unknown_result="禁止重试。改用 query-order 核对状态",
    reconcile_with=("query-order",),
))

_register(["draw-lottery", "mall-create-order"], ToolRisk(
    level=WRITE,
    max_auto_retries=0,
    on_unknown_result="禁止重试（消耗积分）。改用 query-my-prizes / mall-order-list 核对",
    reconcile_with=("query-my-prizes", "mall-order-list"),
))

_register(["delivery-create-address"], ToolRisk(
    level=WRITE,
    max_auto_retries=0,
    on_unknown_result="禁止重试。改用 delivery-query-addresses 核对",
    reconcile_with=("delivery-query-addresses",),
))

_register(["party-order-create"], ToolRisk(
    level=WRITE,
    max_auto_retries=0,
    on_unknown_result="禁止重试，需人工确认",
    reconcile_with=(),
))


def classify(tool: str) -> str:
    """返回 read / write / forbidden。"""
    if tool in FORBIDDEN_THIS_ROUND:
        # 注意：调用是否真的发生由调用方（Skill）决定；
        # 这里只标记"本轮授权范围内不得真实调用"。
        return WRITE if tool in WRITE_TOOLS else READ
    if tool in WRITE_TOOLS:
        return WRITE
    if tool in READ_TOOLS:
        return READ
    return WRITE  # 未知工具按写处理（最保守）


def retry_policy(tool: str) -> ToolRisk:
    return _POLICIES.get(tool, _UNKNOWN_RISK)


def assert_may_call(tool: str, *, real: bool) -> None:
    """真实调用前的硬校验。real=True 表示要打真实 MCP。

    - 本轮禁止真实调用的写工具 → 抛 PermissionError
    - 其余写工具 → 必须在调用层已获得用户明确确认（由调用方保证），此处放行
    """
    if real and tool in FORBIDDEN_THIS_ROUND:
        raise PermissionError(
            f"本轮未授权真实调用写操作工具：{tool}。"
            f"如需真实调用，必须由用户单独授权（见 docs/06-next-steps.md 的 G6/G8）。"
        )


def reconcile_tool_for(tool: str) -> tuple[str, ...]:
    """响应不明时应该用哪些只读工具去核对。"""
    return retry_policy(tool).reconcile_with
