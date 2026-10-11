"""Markdown 渲染：盲盒结果、券池、活动、支付入口。"""

from __future__ import annotations

from .money import Fen, fen_to_yuan_str

__all__ = [
    "render_combos",
    "render_coupon_pool",
    "render_pay_entry",
    "render_order_status",
    "DISCLAIMER",
]

DISCLAIMER = (
    "> ⚠️ 价格、库存与优惠以麦当劳官方渠道实时结果为准。"
    "本工具展示的应付金额来自官方 MCP 实时算价；"
    "忌口过滤基于分类与名称关键词，**不是过敏原识别，不承诺过敏安全**。"
)


def render_combos(candidates: list[dict], budget_fen: Fen, *, priced: dict | None = None) -> str:
    """priced: {fingerprint: {payable_fen, discount_fen, note}} 实时算价结果。"""
    if not candidates:
        return "没有可用的组合。"
    lines = [f"### 🎲 预算 ¥{fen_to_yuan_str(budget_fen)} 的候选", ""]
    lines.append("| # | 组合 | 估算 | 实时算价 | 优惠 | 应付 |")
    lines.append("|---|---|---|---|---|---|")
    priced = priced or {}
    for idx, c in enumerate(candidates, 1):
        names = " + ".join(i["name"] for i in c["items"])
        p = priced.get(c["fingerprint"]) or {}
        pay = f"¥{fen_to_yuan_str(p['payable_fen'])}" if p.get("payable_fen") is not None else "待算"
        disc = f"-¥{fen_to_yuan_str(p['discount_fen'])}" if p.get("discount_fen") else "—"
        lines.append(f"| {idx} | {names} | ¥{fen_to_yuan_str(c['est_total_fen'])} | "
                     f"{'✅' if p.get('payable_fen') is not None else '—'} | {disc} | **{pay}** |")
    lines.append("")
    lines.append("> 估算列来自菜单价，仅用于筛选；**应付金额以实时算价为准**。")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_coupon_pool(pool: dict) -> str:
    lines = ["### 🎟️ 优惠券盘点", ""]
    for label, key in (("门店可用（能算价）", "store"), ("我的卡包", "my"), ("可领取", "available")):
        rows = pool.get(key) or []
        lines.append(f"**{label}：{len(rows)} 张**")
        if not rows:
            lines.append("")
            continue
        lines.append("| 券名 | 有效期 | 适用商品 | 状态 |")
        lines.append("|---|---|---|---|")
        for c in rows[:20]:
            valid = f"{c.get('valid_from') or '?'} → {c.get('valid_to') or '?'}" if (c.get("valid_from") or c.get("valid_to")) else "未知"
            prods = "、".join(str(x) for x in (c.get("product_codes") or [])[:3]) or "—"
            lines.append(f"| {c.get('title')} | {valid} | {prods} | {c.get('status') or '—'} |")
        lines.append("")
    if pool.get("parse_warnings"):
        for w in pool["parse_warnings"]:
            lines.append(f"> ⚠️ {w}")
    return "\n".join(lines)


def render_pay_entry(parsed: dict) -> str:
    """支付入口渲染。没有链接时降级，不虚构。"""
    lines = ["### 💳 支付", ""]
    if parsed.get("order_id"):
        lines.append(f"- **订单号**：`{parsed['order_id']}`")
    status = parsed.get("status") or {}
    if status.get("text"):
        lines.append(f"- **订单状态**：{status['text']}"
                     + ("" if status.get("state") != "unknown" else f"（{status.get('note')}）"))
    dl = parsed.get("payment_deadline") or {}
    if dl.get("known"):
        lines.append(f"- **支付截止**：{dl['expire_at']}（{dl['note']}）")
    else:
        lines.append(f"- **支付截止**：未知（{dl.get('note')}）")

    if parsed.get("pay_entry_known"):
        lines.append("")
        for u in parsed["pay_urls"]:
            lines.append(f"- 支付链接：`{u}`")
        lines.append("")
        lines.append("**怎么付**（已实测确认）：在**浏览器**打开上面的链接 → 出现**扫码画面** → "
                     "用**麦当劳 App 或微信**扫码 → 跳转到麦当劳 App（没装会跳应用商店）。"
                     "App 没登录过需要登录，登录过免登录。")
        lines.append("")
        lines.append("> 💡 也可以**不扫码**：打开**麦当劳 App** 或**麦当劳微信小程序**，"
                     "在订单里直接找到这笔未支付订单付款。")
    else:
        lines.append("")
        lines.append("> ⚠️ 本次返回未发现支付链接字段。请打开**麦当劳 App**或**麦当劳微信小程序**，"
                     "在订单列表里找到这笔未支付订单完成支付。")

    dl_note = parsed.get("payment_deadline") or {}
    if dl_note.get("known"):
        lines.append("")
        lines.append("> ⏱️ 支付期限以服务端 `expirePayTime` 为准（本单 15 分钟为服务端实际值，非固定常数）；"
                     "在麦当劳 App 查看该订单时也能看到倒计时。")

    lines.append("")
    lines.append("**支付需由你本人完成 —— 本 Skill 不会、也无法代付。**")
    lines.append("")
    lines.append("> ⚠️ 订单创建成功 **不等于** 已支付。")
    return "\n".join(lines)


def render_order_status(interpreted: dict, deadline: dict | None = None) -> str:
    state = interpreted.get("state")
    text = interpreted.get("text") or "（无状态文本）"
    mapping = {
        "paid": "✅ 已支付或已进入制作/配送",
        "unpaid": "⏳ 待支付",
        "cancelled": "❌ 已取消",
        "unknown": "❓ 状态未知，不做成功/失败推断",
    }
    lines = [f"- **订单状态**：{text} → {mapping.get(state, '❓ 未知')}",
             f"- 判定依据：{interpreted.get('note')}"]
    if deadline:
        if deadline.get("known"):
            lines.append(f"- **支付期限**：{deadline['expire_at']}（{deadline['note']}）")
        else:
            lines.append(f"- **支付期限**：未知（{deadline.get('note')}）")
    return "\n".join(lines)
