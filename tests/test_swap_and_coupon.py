"""套餐子项负差价替换 + 优惠券单券试算与同名重复批次处理。"""

from __future__ import annotations

import unittest

from _harness import ROOT  # noqa: F401

from mcdradar.swap import apply_swap, parse_rounds, plan_swaps
from mcdradar.coupon import (
    CouponPool, build_single_coupon_trials, diff_coupon_pools,
    match_coupons_to_items, normalize_available_coupons, normalize_store_coupons,
    pick_best_trial,
)
from mcdradar.models import CouponInfo

# 实测结构：麦辣鸡腿汉堡三件套（¥33.5）
MEAL_DETAIL = {
    "data": {
        "code": "9900005456",
        "name": "麦辣鸡腿汉堡三件套",
        "rounds": [
            {"id": "1", "name": "麦辣鸡腿汉堡",
             "choices": [{"code": "1440", "name": "麦辣鸡腿汉堡", "isDefault": True, "diffPrice": "+ ¥0"}]},
            {"id": "2", "name": "选择套餐内小食",
             "choices": [
                 {"code": "4810", "name": "中薯条", "isDefault": True, "diffPrice": "+ ¥0"},
                 {"code": "4811", "name": "大薯条", "isDefault": False, "diffPrice": "+ ¥2.5"},
                 {"code": "9901", "name": "小杯玉米杯", "isDefault": False, "diffPrice": "- ¥0.5"},
             ]},
            {"id": "3", "name": "选择套餐内饮料",
             "choices": [
                 {"code": "3050", "name": "可乐中杯", "isDefault": True, "diffPrice": "+ ¥0"},
                 {"code": "3099", "name": "冰美式", "isDefault": False, "diffPrice": "+ ¥9"},
             ]},
        ],
    }
}


class TestSwap(unittest.TestCase):
    def test_parse_negative_diff(self):
        rounds = parse_rounds(MEAL_DETAIL)
        c = [x for x in rounds[1]["choices"] if x["code"] == "9901"][0]
        self.assertEqual(c["diffFen"], -50)

    def test_no_swap_needed(self):
        plan = plan_swaps(parse_rounds(MEAL_DETAIL), 0)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.options, [])

    def test_negative_swap_covers_shortfall(self):
        """超预算 50 分 → 换小杯玉米杯（-50 分）即可压进预算。"""
        plan = plan_swaps(parse_rounds(MEAL_DETAIL), 50)
        self.assertTrue(plan.feasible)
        self.assertIsNotNone(plan.best)
        self.assertEqual(plan.best.to_code, "9901")
        self.assertEqual(plan.best.delta_fen, -50)

    def test_swap_insufficient(self):
        """超预算 500 分，最省只 -50 分 → 不可行，要如实说明还差多少。"""
        plan = plan_swaps(parse_rounds(MEAL_DETAIL), 500)
        self.assertFalse(plan.feasible)
        self.assertIn("仍差", plan.note)

    def test_options_sorted_by_saving(self):
        plan = plan_swaps(parse_rounds(MEAL_DETAIL), 1000)
        deltas = [o.delta_fen for o in plan.options]
        self.assertEqual(deltas, sorted(deltas))
        self.assertTrue(all(d < 0 for d in deltas))

    def test_apply_swap(self):
        round_list = [{"round": "2", "comboItemList": [{"code": "4810", "quantity": 1}]}]
        plan = plan_swaps(parse_rounds(MEAL_DETAIL), 50)
        new = apply_swap(round_list, plan.best)
        self.assertEqual(new[0]["comboItemList"][0]["code"], "9901")


# ---- 优惠券 ---------------------------------------------------------------

STORE_COUPONS_RAW = {
    "success": True,
    "data": {"list": [
        {"title": "麦旋风任选", "couponId": "AAA111", "couponCode": "MCDEXAMPLECODE000000001",
         "tradeDateTime": "2026-10-05 10:30:00-2026-10-09 23:59:59",
         "products": [{"productCode": "9900014239", "productName": "麦旋风"}]},
        {"title": "薯薯任选", "couponId": "BBB222", "couponCode": "MCDEXAMPLECODE000000002",
         "tradeDateTime": "2026-10-01 00:00:00-2026-10-31 23:59:59",
         "products": [{"productCode": "4810", "productName": "中薯条"}]},
    ]},
}

AVAILABLE_RAW_MD = """### 麦麦省优惠券列表：
- 优惠券标题：巧克力味厚松饼猪柳蛋套餐 \\
  状态：已领取 \\
  优惠券图片：\\
    <img src="https://img.mcd.cn/a.png" alt="x">
- 优惠券标题：免费脆薯饼 \\
  状态：可领取 \\
  优惠券图片：\\
    <img src="https://img.mcd.cn/b.png" alt="x">
- 优惠券标题：人气麦旋风买一送一 \\
  状态：可领取 \\
  优惠券图片：\\
    <img src="https://img.mcd.cn/c.png" alt="x">
"""


class TestCouponNormalize(unittest.TestCase):
    def test_store_coupons_have_ids_and_products(self):
        cs = normalize_store_coupons(STORE_COUPONS_RAW)
        self.assertEqual(len(cs), 2)
        self.assertEqual(cs[0].coupon_id, "AAA111")
        self.assertEqual(cs[0].valid_to, "2026-10-09 23:59:59")
        self.assertIn("9900014239", cs[0].product_codes)

    def test_available_coupons_parsed(self):
        cs, warns = normalize_available_coupons(AVAILABLE_RAW_MD)
        titles = [c.title for c in cs]
        self.assertIn("免费脆薯饼", titles)
        claimable = [c for c in cs if c.status == "可领取"]
        self.assertEqual(len(claimable), 2)

    def test_matching(self):
        cs = normalize_store_coupons(STORE_COUPONS_RAW)
        m = match_coupons_to_items(cs, ["4810", "3050"])
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].coupon_id, "BBB222")

    def test_no_match(self):
        cs = normalize_store_coupons(STORE_COUPONS_RAW)
        self.assertEqual(match_coupons_to_items(cs, ["9999"]), [])


class TestSingleCoupon(unittest.TestCase):
    BASE = {"storeCode": "1000001", "orderType": 1, "beType": 1,
            "items": [{"productCode": "9900014239", "quantity": 1}]}

    def test_trials_have_exactly_one_coupon_each(self):
        cs = normalize_store_coupons(STORE_COUPONS_RAW)
        trials = build_single_coupon_trials(self.BASE, cs)
        self.assertEqual(len(trials), 2)
        for t in trials:
            used = [i for i in t["request"]["items"] if i.get("couponId")]
            self.assertEqual(len(used), 1)

    def test_coupon_without_id_skipped(self):
        no_id = [CouponInfo(title="无ID券")]
        self.assertEqual(build_single_coupon_trials(self.BASE, no_id), [])

    def test_pick_best_lowest_payable(self):
        entries = [
            {"coupon": {"title": "A"}, "result": {"success": True, "data": {"price": 990, "discount": 410}}},
            {"coupon": {"title": "B"}, "result": {"success": True, "data": {"price": 1200, "discount": 200}}},
        ]
        out = pick_best_trial(entries)
        self.assertEqual(out["best"]["payable_fen"], 990)
        self.assertEqual(out["best"]["coupon"]["title"], "A")
        self.assertIn("不可叠加", out["note"])

    def test_failed_trial_not_selected(self):
        entries = [
            {"coupon": {"title": "A"}, "result": {"success": False, "message": "暂不支持多张券使用"}},
            {"coupon": {"title": "B"}, "result": {"success": True, "data": {"price": 1200}}},
        ]
        out = pick_best_trial(entries)
        self.assertEqual(out["best"]["coupon"]["title"], "B")
        self.assertEqual(out["best"]["payable_fen"], 1200)


class TestDuplicateCouponNames(unittest.TestCase):
    def test_same_title_different_uid(self):
        """同名不同批次（图片 URL 不同）必须产生不同 uid。"""
        a = CouponInfo(title="巧克力味厚松饼猪柳蛋套餐", source="available",
                       status="可领取", uid="")
        from mcdradar.coupon import _mk_uid
        u1 = _mk_uid("avail", a.title, "https://img/1.png")
        u2 = _mk_uid("avail", a.title, "https://img/2.png")
        self.assertNotEqual(u1, u2)

    def test_diff_reports_low_confidence_without_uid(self):
        """没有可靠唯一标识时，不得声称精确领取数量。"""
        before = CouponPool(available=[
            CouponInfo(title="同名券", status="可领取", uid="u1"),
            CouponInfo(title="同名券", status="可领取", uid="u2"),
            CouponInfo(title="同名券", status="可领取", uid="u3"),
        ])
        after = CouponPool(available=[
            CouponInfo(title="同名券", status="已领取", uid="u1"),
        ])
        d = diff_coupon_pools(before, after)
        self.assertEqual(d["before_claimable"], 3)
        self.assertEqual(d["after_claimable"], 0)
        self.assertTrue(d["reliable_uid"])
        self.assertIn("3", d["conclusion"])

    def test_diff_low_confidence_note(self):
        before = CouponPool(available=[CouponInfo(title="X", status="可领取", uid="")])
        after = CouponPool(available=[CouponInfo(title="X", status="可领取", uid="")])
        d = diff_coupon_pools(before, after)
        self.assertFalse(d["reliable_uid"])
        self.assertEqual(d["confidence"], "low")
        self.assertIn("不声称精确领取数量", d["conclusion"])


if __name__ == "__main__":
    unittest.main()
