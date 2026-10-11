"""基于**真实 MCP 只读返回**的集成测试。

fixtures/ 下的文件来自本轮真实调用（见各文件 _source/_captured_at 字段）。
本文件验证：真实数据形态能被正确解析，且真实世界里的"坑"能被代码捕获。

⚠️ 不涉及任何写操作。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _harness import ROOT  # noqa: F401

from mcdradar.coupon import CouponPool, normalize_available_coupons, normalize_store_coupons
from mcdradar.menu import normalize_menu
from mcdradar.money import fen_to_yuan_str
from mcdradar.order_flow import (
    interpret_order_status, parse_order_result, verify_pricing_covers_items,
)
from mcdradar.swap import parse_rounds, plan_swaps

FIX = Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    p = FIX / name
    return json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" else p.read_text(encoding="utf-8")


class TestRealPricingSilentDrop(unittest.TestCase):
    """🔴 真实观测：请求 2 个商品，服务端只回了 1 个。"""

    def test_missing_item_detected(self):
        raw = load("real_calculate_price_1450_3050.json")
        items = raw["_request"]["items"]
        check = verify_pricing_covers_items(items, raw)
        self.assertFalse(check["ok"])
        self.assertIn("3050", check["missing"])
        self.assertIn("必须重新拉取菜单", check["message"])

    def test_full_coverage_passes(self):
        raw = load("real_calculate_price_1450_3050.json")
        check = verify_pricing_covers_items([{"productCode": "1450", "quantity": 1}], raw)
        self.assertTrue(check["ok"])
        self.assertEqual(check["missing"], {})

    def test_real_units_are_fen(self):
        raw = load("real_calculate_price_1450_3050.json")
        self.assertEqual(raw["data"]["price"], 1700)
        self.assertEqual(fen_to_yuan_str(raw["data"]["price"]), "17.00")

    def test_real_take_way_code(self):
        raw = load("real_calculate_price_1450_3050.json")
        codes = [t["code"] for t in raw["data"]["takeWayList"]]
        self.assertIn("locker-out", codes)

    def test_quantity_shortfall_detected(self):
        raw = load("real_calculate_price_1450_3050.json")
        check = verify_pricing_covers_items([{"productCode": "1450", "quantity": 2}], raw)
        self.assertFalse(check["ok"])
        self.assertEqual(check["missing"]["1450"], 2)


class TestRealEmptyCouponPool(unittest.TestCase):
    """真实观测：门店券池可能为空（券过期后）。"""

    def test_empty_pool_handled(self):
        raw = load("real_store_coupons_empty.json")
        cs = normalize_store_coupons(raw)
        self.assertEqual(cs, [])
        pool = CouponPool(store=cs)
        self.assertEqual(pool.claimable(), [])
        self.assertEqual(pool.store, [])


class TestRealMealDetailError(unittest.TestCase):
    """真实观测：商品编码失效会返回 60001，需重新拉全量菜单。"""

    def test_error_response_yields_no_rounds(self):
        raw = load("real_meal_detail_error.json")
        self.assertFalse(raw["success"])
        self.assertEqual(raw["code"], 60001)
        rounds = parse_rounds(raw)
        self.assertEqual(rounds, [])

    def test_swap_plan_graceful_on_dead_product(self):
        """商品失效时，替换方案应给出明确提示而不是崩溃。"""
        raw = load("real_meal_detail_error.json")
        plan = plan_swaps(parse_rounds(raw), 100)
        self.assertFalse(plan.feasible)
        self.assertIn("需换组合或提高预算", plan.note)


class TestRealAvailableCoupons(unittest.TestCase):
    """真实 Markdown 券列表解析 + 同名重复批次。"""

    def test_parsed_statuses(self):
        text = load("real_available_coupons.md")
        cs, warns = normalize_available_coupons(text)
        titles = [c.title for c in cs]
        self.assertIn("免费脆薯饼", titles)
        self.assertIn("9.9元中杯冰美式", titles)
        claimable = [c for c in cs if c.status == "可领取"]
        self.assertEqual(len(claimable), 3)

    def test_duplicate_titles_get_distinct_uid(self):
        text = load("real_available_coupons.md")
        cs, _ = normalize_available_coupons(text)
        dup = [c for c in cs if c.title == "巧克力味厚松饼猪柳蛋套餐"]
        self.assertGreaterEqual(len(dup), 2)
        self.assertEqual(len({c.uid for c in dup}), len(dup))

    def test_no_raw_field_leaks_into_title(self):
        """真实行形如 '- 优惠券标题：麦旋风任选 \\'，标题不得包含字段名。"""
        text = load("real_available_coupons.md")
        cs, _ = normalize_available_coupons(text)
        for c in cs:
            self.assertNotIn("优惠券标题", c.title)
            self.assertNotIn("\\", c.title)


class TestRealOrderResultParsing(unittest.TestCase):
    def test_real_order_shape(self):
        """用真实 query-order 的字段形态验证状态与期限解析（订单号已脱敏为占位符）。"""
        raw = {"success": True, "data": {
            "orderId": "1000000000000000000000000002",
            "orderStatus": "订单已完成",
            "status": "40",
            "realTotalAmount": "7.9",
            "expirePayTime": "2025-12-05 20:23:12",
        }}
        p = parse_order_result(raw)
        self.assertEqual(p["order_id"], "1000000000000000000000000002")
        self.assertEqual(p["status"]["state"], "paid")
        self.assertTrue(p["payment_deadline"]["known"])
        # status 数字码（40）与官方枚举不符，代码不得依赖
        self.assertEqual(p["status"]["code"], "40")
        self.assertEqual(p["status"]["state"], "paid")   # 只按文本判定


class TestRealSilentDropReproducedAgain(unittest.TestCase):
    """第二轮 P0 修复时（2026-10-10 01:03）重新观测：**同一现象可复现**。

    这不是偶发：两次真实调用，请求 2 件商品，服务端都只回 1 件。
    证明「算价覆盖校验」必须成为下单前的硬闸门，而不是可选项。
    """

    def test_reproduced_and_blocked(self):
        raw = load("real_calculate_price_dropped_item_20261010.json")
        items = raw["_request"]["items"]
        check = verify_pricing_covers_items(items, raw)
        self.assertFalse(check["ok"])
        self.assertEqual(check["missing"], {"3050": 1})
        self.assertTrue(check["reliable"])      # 结构可识别，属于"真缺商品"

    def test_single_item_request_passes(self):
        """只请求确实被计价的那件商品 → 校验通过（不误伤）。"""
        raw = load("real_calculate_price_dropped_item_20261010.json")
        check = verify_pricing_covers_items([{"productCode": "1450", "quantity": 1}], raw)
        self.assertTrue(check["ok"])


class TestMenuChangeInvalidates(unittest.TestCase):
    """商品失效 → 菜单指纹变化 → 旧确认必须失效。"""

    def test_digest_changes_when_item_removed(self):
        from mcdradar.menu import menu_digest
        raw = {"data": {"meals": {
            "1450": {"name": "麦香鸡", "currentPrice": "17"},
            "3050": {"name": "可乐中杯", "currentPrice": "9.5"}}}}
        d1 = menu_digest(normalize_menu(raw))
        raw2 = {"data": {"meals": {
            "1450": {"name": "麦香鸡", "currentPrice": "17"}}}}
        d2 = menu_digest(normalize_menu(raw2))
        self.assertNotEqual(d1, d2)


class TestRealPaidOrderStatus(unittest.TestCase):
    """🔴 真实观测：付款后 query-order 返回 orderStatus=「餐厅配餐中」、status=「31」。

    31 不在接口文档给出的枚举（1/2/4/6/7/8/10）中 → 证明**不能依赖数字码**，
    必须按文本关键字判断。本用例锁定该真实样本。
    """

    def test_paid_state_recognized_by_text(self):
        raw = load("real_query_order_paid_after_payment.json")
        st = interpret_order_status(raw)
        self.assertEqual(st["state"], "paid")          # 「配餐中」命中 PAID_LIKE
        self.assertEqual(st["code"], "31")             # 文档枚举里没有 31

    def test_status_code_not_used_for_decision(self):
        """即使把数字码换成任意未登记值，判定仍应只由文本决定。"""
        raw = load("real_query_order_paid_after_payment.json")
        raw = json.loads(json.dumps(raw))
        raw["data"]["status"] = "999"
        self.assertEqual(interpret_order_status(raw)["state"], "paid")

    def test_real_amounts_consistent(self):
        """真实订单：商品价 34.9 + 打包 1.9 + 运费 6 = 实付 42.8。"""
        raw = load("real_query_order_paid_after_payment.json")
        d = raw["data"]
        self.assertEqual(d["productPrice"], "34.9")
        self.assertEqual(d["realPackingFeeTotalPrice"], "1.9")
        self.assertEqual(d["realDeliveryPrice"], "6")
        self.assertEqual(d["realTotalAmount"], "42.8")

    def test_real_combo_items_locked(self):
        """锁定搭配回显：无鱼、无甜品、可乐为无糖。"""
        raw = load("real_query_order_paid_after_payment.json")
        items = [c["itemName"] for c in raw["data"]["orderProductList"][0]["comboItemList"]]
        self.assertEqual(items, ["双层脆鸡堡", "麦乐鸡5块", "中薯条", "无糖可口可乐中杯"])
        self.assertFalse(any("鱼" in i for i in items))
        self.assertIn("无糖", items[3])


if __name__ == "__main__":
    unittest.main()
