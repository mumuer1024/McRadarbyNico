#!/usr/bin/env python3
"""麦麦雷达 McRadar —— 命令行入口。

用法：
    python scripts/mcd_radar.py <command> [--file input.json | --json '{...}']
                                [--data-dir DIR] [--md]

所有命令均**不发起 MCP 网络调用**。
MCP 返回由宿主（WorkBuddy Skill）通过 stdin / 文件喂入。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcdradar import (  # noqa: E402
    campaign, combo, coupon, coupon_bind, menu, money, order_flow,
    order_guard, prefs, render, store as store_mod, summary,
)
from mcdradar.models import CouponInfo, StoreInfo  # noqa: E402
from mcdradar.state import StateStore, iso_ts  # noqa: E402


def _load(args) -> dict:
    if args.json:
        return json.loads(args.json)
    if args.file:
        return json.loads(Path(args.file).read_text(encoding="utf-8"))
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def _out(obj, as_md=False, md_key="markdown"):
    if as_md and isinstance(obj, dict) and md_key in obj:
        print(obj[md_key])
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ------------------------------ S2 命令 -------------------------------------

def cmd_normalize_menu(a):
    d = _load(a)
    snap = menu.normalize_menu(d.get("menu") or d)
    return {
        "items": [i.to_dict() for i in snap.items],
        "categories": snap.categories,
        "size": snap.size(),
        "digest": menu.menu_digest(snap),
        "conditional_price_items": [i.name for i in snap.items if i.price_is_conditional],
    }


def cmd_plan_combos(a):
    d = _load(a)
    snap = menu.normalize_menu(d.get("menu") or {})
    p = prefs.Preferences.from_dict(d.get("prefs") or {})
    kept, diag = prefs.filter_items(snap.items, p)
    req = combo.BlindBoxRequest(
        budget_fen=money.yuan_to_fen(d.get("budget_yuan", d.get("budget", 0))),
        count=int(d.get("count", 3)),
        mode=d.get("mode", "random"),
        seed=d.get("seed"),
        exclude_fingerprints=set(d.get("exclude_fingerprints") or []),
        prefer_codes=list(d.get("prefer_codes") or []),
        allow_conditional_price=bool(d.get("allow_conditional_price", False)),
    )
    res = combo.generate_candidates(kept, req)
    out = res.to_dict()
    out["filter_diagnostics"] = diag
    out["budget_fen"] = req.budget_fen
    out["menu_digest"] = menu.menu_digest(snap)
    if not res.exhausted:
        out["markdown"] = render.render_combos(out["candidates"], req.budget_fen)
    return out


def cmd_parse_rounds(a):
    return {"rounds": combo_rounds(_load(a))}


def combo_rounds(d):
    from mcdradar.swap import parse_rounds
    return parse_rounds(d.get("detail") or d)


def cmd_plan_swaps(a):
    d = _load(a)
    from mcdradar.swap import parse_rounds, plan_swaps
    rounds = parse_rounds(d.get("detail") or d)
    over = int(d.get("over_fen", 0))
    plan = plan_swaps(rounds, over)
    return plan.to_dict()


# ------------------------------ S3 命令 -------------------------------------

def cmd_normalize_coupons(a):
    d = _load(a)
    pool = coupon.CouponPool()
    if d.get("store"):
        pool.store = coupon.normalize_store_coupons(d["store"])
    if d.get("my") is not None:
        pool.my, w = coupon.normalize_my_coupons(d["my"])
        pool.parse_warnings += w
    if d.get("available") is not None:
        pool.available, w = coupon.normalize_available_coupons(d["available"])
        pool.parse_warnings += w
    out = pool.to_dict()
    out["claimable_count"] = len(pool.claimable())
    out["markdown"] = render.render_coupon_pool(out)
    return out


def cmd_match_coupons(a):
    d = _load(a)
    coupons = [CouponInfo(**c) for c in d.get("coupons") or []]
    matched = coupon.match_coupons_to_items(coupons, d.get("item_codes") or [])
    return {"matched": [c.to_dict() for c in matched]}


def cmd_build_trials(a):
    d = _load(a)
    coupons = [CouponInfo(**c) for c in d.get("coupons") or []]
    trials = coupon.build_single_coupon_trials(d.get("base_request") or {}, coupons,
                                               int(d.get("target_index", 0)))
    return {"trials": trials, "note": "每个 trial 只含 1 张券（官方 600022：不支持多张券）"}


def cmd_pick_best(a):
    d = _load(a)
    return coupon.pick_best_trial(d.get("entries") or [])


def cmd_campaign(a):
    d = _load(a)
    entries = campaign.parse_campaign_calendar(d.get("calendar") or d)
    st = StateStore(a.data_dir)
    action = d.get("action", "show")
    if action == "save":
        snap = campaign.save_snapshot(st, entries)
        return {"saved": True, "captured_at": snap["captured_at"], "count": len(entries)}
    prev = campaign.load_snapshot(st)
    if action == "diff":
        diff = campaign.diff_snapshot(prev, entries)
        return {"diff": diff, "entries": [e.to_dict() for e in entries]}
    return {"entries": [e.to_dict() for e in entries],
            "markdown": campaign.render_entries(entries)}


# ------------------------------ S4 订单 -------------------------------------

def cmd_build_order(a):
    d = _load(a)
    req = order_flow.build_order_request(**d)
    errs = order_flow.validate_order_request(req)
    return {"request": req, "errors": errs, "valid": not errs}


def cmd_prepare_order(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    req = order_flow.build_order_request(**(d.get("order") or {}))
    res = order_flow.prepare_order(st, req, d.get("est_payable_fen"))
    if res.get("ok"):
        res["markdown"] = "已登记订单请求（awaiting_confirm），**尚未确认**，展示摘要后等待用户确认。"
    return res


def cmd_confirm_order(a):
    """P0-2：把用户明确确认写成可验证状态。只能由宿主在收到明确授权后调用。"""
    d = _load(a)
    st = StateStore(a.data_dir)
    req = d.get("request") or {}
    return order_flow.confirm_order(
        st, d["digest"], req,
        user_confirmed=bool(d.get("user_confirmed")),
        note=d.get("note", ""),
    )


def cmd_confirmation_status(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.confirmation_status(st, d["digest"])


def cmd_record_pricing_check(a):
    """登记算价覆盖校验结果（下单前必做）。

    🔴 不接受外部直接传入的 ok：必须给 request（当前订单请求）+ pricing
    （宿主调用 calculate-price 得到的**原始响应**），由脚本现场计算。
    """
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.record_pricing_check(
        st, d["digest"], request=d.get("request") or {}, pricing=d.get("pricing") or {})


def cmd_preflight(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.preflight(st, d["digest"], d["request"],
                                require_pricing_check=bool(d.get("require_pricing_check", True)))


def cmd_begin_submit(a):
    """🔒 内部强制执行完整提交闸门（指纹 + 确认 + 状态 + 算价覆盖）。"""
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.begin_submit(
        st, d["digest"], d.get("request"),
        require_pricing_check=bool(d.get("require_pricing_check", True)),
        now=d.get("now"),
    )


def _finish(a, fn):
    d = _load(a)
    st = StateStore(a.data_dir)
    return fn(st, d["digest"], *[d[k] for k in d.get("_args", [])])


def cmd_finish_unknown(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.finish_unknown(st, d["digest"], d.get("reason", "响应不明"))


def cmd_finish_confirmed(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.finish_confirmed(st, d["digest"], d["order_id"])


def cmd_finish_failed(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.finish_failed(st, d["digest"], d.get("message", "提交失败"))


def cmd_authorize_retry(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.authorize_retry(st, d["digest"], d.get("note", ""))


def cmd_reconcile(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return order_flow.reconcile(st, d["digest"])


def cmd_parse_order(a):
    d = _load(a)
    parsed = order_flow.parse_order_result(d.get("result") or d)
    parsed["markdown"] = render.render_pay_entry(parsed)
    return parsed


def cmd_check_pricing(a):
    d = _load(a)
    return order_flow.verify_pricing_covers_items(d.get("items") or [], d.get("pricing") or {})


def cmd_order_status(a):
    d = _load(a)
    st = order_flow.interpret_order_status(d.get("result") or d)
    dl = order_flow.payment_deadline(d.get("result") or d)
    st["markdown"] = render.render_order_status(st, dl)
    st["payment_deadline"] = dl
    return st


def cmd_gc(a):
    st = StateStore(a.data_dir)
    return {"removed": order_guard.gc(st)}


# ------------------------------ S4 领券 -------------------------------------

def _pool(d: dict) -> coupon.CouponPool:
    p = coupon.CouponPool()
    for c in d.get("my") or []:
        p.my.append(CouponInfo(**c))
    for c in d.get("store") or []:
        p.store.append(CouponInfo(**c))
    for c in d.get("available") or []:
        p.available.append(CouponInfo(**c))
    p.parse_warnings = list(d.get("parse_warnings") or [])
    return p


def cmd_plan_bind(a):
    d = _load(a)
    return coupon_bind.plan_bind(_pool(d.get("pool") or {}))


def cmd_prepare_bind(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return coupon_bind.prepare_bind(st, _pool(d.get("pool") or {}))


def cmd_confirm_bind(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return coupon_bind.confirm_bind(
        st, _pool(d.get("pool") or {}),
        user_confirmed=bool(d.get("user_confirmed")),
        reauthorize=bool(d.get("reauthorize")),
        note=d.get("note", ""),
    )


def cmd_preflight_bind(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return coupon_bind.preflight_bind(st, nonce=d.get("nonce"),
                                      user_confirmed=bool(d.get("user_confirmed", True)))


def cmd_begin_bind(a):
    d = _load(a) if a.json or a.file else {}
    st = StateStore(a.data_dir)
    return coupon_bind.begin_bind(st, nonce=d.get("nonce"),
                                  user_confirmed=bool(d.get("user_confirmed", True)))


def cmd_finish_bind_unknown(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return coupon_bind.finish_unknown(st, d.get("reason", "响应不明"))


def cmd_finish_bind_done(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    return coupon_bind.finish_done(st, _pool(d.get("pool") or {}))


# ------------------------------ 门店 ----------------------------------------

def cmd_stores(a):
    d = _load(a)
    opts = store_mod.normalize_stores(d.get("stores") or d)
    return {"stores": [o.to_dict() for o in opts]}


def cmd_save_store(a):
    d = _load(a)
    st = StateStore(a.data_dir)
    info = StoreInfo(**{k: d[k] for k in ("store_code", "store_name", "be_type", "be_code", "order_type") if k in d})
    store_mod.save_default_store(st, info)
    return {"saved": info.to_dict()}


def cmd_load_store(a):
    st = StateStore(a.data_dir)
    info = store_mod.load_default_store(st)
    return {"store": info.to_dict() if info else None}


# ------------------------------ 工具 ----------------------------------------

def cmd_money(a):
    d = _load(a)
    return {
        "yuan_to_fen": money.yuan_to_fen(d["value"]),
        "fen_to_yuan": money.fen_to_yuan_str(money.yuan_to_fen(d["value"])),
    }


def cmd_selftest(a):
    return {"ok": True, "now": iso_ts(), "version": __import__("mcdradar").__version__}


COMMANDS = {
    # S2
    "normalize-menu": cmd_normalize_menu,
    "plan-combos": cmd_plan_combos,
    "parse-rounds": cmd_parse_rounds,
    "plan-swaps": cmd_plan_swaps,
    # S3
    "normalize-coupons": cmd_normalize_coupons,
    "match-coupons": cmd_match_coupons,
    "build-trials": cmd_build_trials,
    "pick-best": cmd_pick_best,
    "campaign": cmd_campaign,
    # S4 订单
    "build-order": cmd_build_order,
    "prepare-order": cmd_prepare_order,
    "confirm-order": cmd_confirm_order,
    "confirmation-status": cmd_confirmation_status,
    "record-pricing-check": cmd_record_pricing_check,
    "preflight": cmd_preflight,
    "begin-submit": cmd_begin_submit,
    "finish-unknown": cmd_finish_unknown,
    "finish-confirmed": cmd_finish_confirmed,
    "finish-failed": cmd_finish_failed,
    "authorize-retry": cmd_authorize_retry,
    "reconcile": cmd_reconcile,
    "parse-order": cmd_parse_order,
    "check-pricing": cmd_check_pricing,
    "order-status": cmd_order_status,
    "gc-orders": cmd_gc,
    # S4 领券
    "plan-bind": cmd_plan_bind,
    "prepare-bind": cmd_prepare_bind,
    "confirm-bind": cmd_confirm_bind,
    "preflight-bind": cmd_preflight_bind,
    "begin-bind": cmd_begin_bind,
    "finish-bind-unknown": cmd_finish_bind_unknown,
    "finish-bind-done": cmd_finish_bind_done,
    # 门店
    "stores": cmd_stores,
    "save-store": cmd_save_store,
    "load-store": cmd_load_store,
    # 工具
    "money": cmd_money,
    "selftest": cmd_selftest,
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="麦麦雷达 McRadar CLI")
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--file", help="输入 JSON 文件")
    p.add_argument("--json", help="输入 JSON 字符串")
    p.add_argument("--data-dir", default=None, help="本地状态目录（默认项目 data/）")
    p.add_argument("--md", action="store_true", help="输出 Markdown 而非 JSON")
    a = p.parse_args(argv)

    try:
        res = COMMANDS[a.command](a)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    _out(res, as_md=a.md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
