"""组合生成、去重、预算不足、辣度过滤、随单购排除测试。"""

from __future__ import annotations

import unittest

from _harness import ROOT  # noqa: F401

from mcdradar.combo import BlindBoxRequest, bucketize, generate_candidates
from mcdradar.menu import normalize_menu
from mcdradar.prefs import NOT_SPICY, SPICY, UNKNOWN, Preferences, filter_items, spiciness
from mcdradar.models import MenuItem

RAW_MENU = {
    "data": {
        "categories": [
            {"name": "鸡肉汉堡/卷", "meals": [{"code": "1450"}, {"code": "1440"}, {"code": "1100"}]},
            {"name": "小食甜品/其他", "meals": [{"code": "4810"}, {"code": "1401"}]},
            {"name": "饮品", "meals": [{"code": "3050"}, {"code": "3060"}]},
        ],
        "meals": {
            "1450": {"name": "麦香鸡", "currentPrice": "17", "originalPrice": "17", "tags": ["单品"]},
            "1440": {"name": "麦辣鸡腿汉堡", "currentPrice": "21.5", "originalPrice": "21.5",
                     "tags": ["麦辣", "川香风味"]},
            "1100": {"name": "巨无霸", "currentPrice": "25", "originalPrice": "25", "tags": ["牛"]},
            "4810": {"name": "中薯条", "currentPrice": "12", "originalPrice": "12", "tags": ["小食"]},
            "1401": {"name": "麦乐鸡5块", "currentPrice": "11", "originalPrice": "11", "tags": ["小食"]},
            "3050": {"name": "可乐中杯", "currentPrice": "9.5", "originalPrice": "9.5", "tags": ["冷饮"]},
            "3060": {"name": "冰美式", "currentPrice": "15", "originalPrice": "15", "tags": ["咖啡"]},
            "9900016309": {"name": "龙焰鸡腿堡四件套", "currentPrice": "29", "originalPrice": "57",
                           "canWithOrder": True, "discountType": "随单购麦金卡优惠"},
        },
    }
}


def _snap():
    return normalize_menu(RAW_MENU)


class TestBuckets(unittest.TestCase):
    def test_drink_and_side_not_misplaced_by_category(self):
        """实测分类「小食甜品/其他」会把薯条误判成甜品 —— 必须以商品名优先。"""
        b = bucketize(_snap().items)
        codes = lambda xs: {i.code for i in xs}
        self.assertIn("3050", codes(b.drink))
        self.assertIn("4810", codes(b.side))
        self.assertNotIn("4810", codes(b.dessert))

    def test_main_bucket_non_empty(self):
        b = bucketize(_snap().items)
        self.assertTrue(b.main)


class TestBudgetHardConstraint(unittest.TestCase):
    def test_no_candidate_over_budget(self):
        req = BlindBoxRequest(budget_fen=3000, count=5, seed=1)
        res = generate_candidates(_snap().items, req)
        for c in res.candidates:
            self.assertLessEqual(c.est_total_fen, 3000)

    def test_low_budget_exhausted_honestly(self):
        """预算 5 元组不出任何东西 → 必须 exhausted 且给出最便宜单项，不编造。"""
        req = BlindBoxRequest(budget_fen=500, count=3, seed=1)
        res = generate_candidates(_snap().items, req)
        self.assertTrue(res.exhausted)
        self.assertIn("最便宜", res.reason)
        self.assertEqual(res.candidates, [])

    def test_with_order_excluded_by_default(self):
        """canWithOrder 商品默认不进候选池。"""
        req = BlindBoxRequest(budget_fen=3000, count=20, seed=3)
        res = generate_candidates(_snap().items, req)
        for c in res.candidates:
            for it in c.items:
                self.assertNotEqual(it.code, "9900016309")

    def test_with_order_can_be_allowed(self):
        req = BlindBoxRequest(budget_fen=3000, count=20, seed=3, allow_conditional_price=True)
        res = generate_candidates(_snap().items, req)
        found = any(i.code == "9900016309" for c in res.candidates for i in c.items)
        self.assertTrue(found)


class TestDedupe(unittest.TestCase):
    def test_candidates_unique_within_round(self):
        req = BlindBoxRequest(budget_fen=4000, count=10, seed=42)
        res = generate_candidates(_snap().items, req)
        fps = [c.fingerprint for c in res.candidates]
        self.assertEqual(len(fps), len(set(fps)))

    def test_exclude_fingerprints_respected(self):
        req1 = BlindBoxRequest(budget_fen=4000, count=10, seed=42)
        r1 = generate_candidates(_snap().items, req1)
        banned = {c.fingerprint for c in r1.candidates[:2]}
        req2 = BlindBoxRequest(budget_fen=4000, count=10, seed=42, exclude_fingerprints=banned)
        r2 = generate_candidates(_snap().items, req2)
        got = {c.fingerprint for c in r2.candidates}
        self.assertFalse(got & banned)

    def test_resample_never_returns_same_first_result(self):
        """连续"换一个"不应抽到同一组合。"""
        seen = set()
        for i in range(3):
            req = BlindBoxRequest(budget_fen=4000, count=1, seed=100 + i, exclude_fingerprints=seen)
            r = generate_candidates(_snap().items, req)
            self.assertTrue(r.candidates)
            fp = r.candidates[0].fingerprint
            self.assertNotIn(fp, seen)
            seen.add(fp)


class TestSpicy(unittest.TestCase):
    def test_spicy_detected(self):
        it = MenuItem(code="1440", name="麦辣鸡腿汉堡", tags=["川香风味"])
        self.assertEqual(spiciness(it), SPICY)

    def test_unknown_spicy(self):
        it = MenuItem(code="1450", name="麦香鸡", tags=["单品"])
        self.assertEqual(spiciness(it), UNKNOWN)

    def test_unknown_kept_but_flagged(self):
        """未知辣度默认保留，但必须被标记（输出时要说明无法保证完全不辣）。"""
        prefs = Preferences(no_spicy=True)
        kept, diag = filter_items(_snap().items, prefs)
        names = {i.name for i in kept}
        self.assertIn("麦香鸡", names)                 # 未知 → 保留
        self.assertNotIn("麦辣鸡腿汉堡", names)        # 已知辣 → 排除
        self.assertTrue(diag["spicy_unknown_note"])
        self.assertIn("麦香鸡", diag["kept_unknown_spicy"])

    def test_unknown_can_be_strictly_excluded(self):
        prefs = Preferences(no_spicy=True, allow_unknown_spicy=False)
        kept, diag = filter_items(_snap().items, prefs)
        names = {i.name for i in kept}
        self.assertNotIn("麦香鸡", names)


class TestDislikes(unittest.TestCase):
    def test_keyword_filter(self):
        prefs = Preferences(exclude_keywords=["咖啡"])
        kept, _ = filter_items(_snap().items, prefs)
        self.assertNotIn("冰美式", {i.name for i in kept})

    def test_code_filter(self):
        prefs = Preferences(dislike_codes=["1100"])
        kept, _ = filter_items(_snap().items, prefs)
        self.assertNotIn("巨无霸", {i.name for i in kept})

    def test_category_filter(self):
        prefs = Preferences(exclude_categories=["饮品"])
        kept, _ = filter_items(_snap().items, prefs)
        self.assertFalse([i for i in kept if i.category == "饮品"])


class TestComboModes(unittest.TestCase):
    def test_full_mode_prefers_complete_meals(self):
        req = BlindBoxRequest(budget_fen=6000, count=5, mode="full", seed=5)
        res = generate_candidates(_snap().items, req)
        self.assertTrue(res.candidates)
        self.assertEqual(res.candidates[0].template, "complete")

    def test_value_mode_prefers_coupon_products(self):
        """优惠优先：prefer_codes 中的商品应被优先选中。"""
        req = BlindBoxRequest(budget_fen=6000, count=8, mode="value", seed=9,
                              prefer_codes=["1100"])
        res = generate_candidates(_snap().items, req)
        hit = any(i.code == "1100" for c in res.candidates for i in c.items)
        self.assertTrue(hit)

    def test_count_respected(self):
        req = BlindBoxRequest(budget_fen=6000, count=3, seed=11)
        res = generate_candidates(_snap().items, req)
        self.assertLessEqual(len(res.candidates), 3)


if __name__ == "__main__":
    unittest.main()
