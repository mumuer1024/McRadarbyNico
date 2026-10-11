"""金额精度（P0-D）与随单购价格陷阱测试。"""

from __future__ import annotations

import unittest

from _harness import ROOT  # noqa: F401  先导入以把 scripts/ 加入 sys.path

from mcdradar.money import fen_to_yuan_str, parse_diff_price_fen, safe_yuan_to_fen, yuan_to_fen
from mcdradar.menu import normalize_menu


class TestMoney(unittest.TestCase):
    def test_yuan_to_fen_basic(self):
        self.assertEqual(yuan_to_fen("26.5"), 2650)
        self.assertEqual(yuan_to_fen("17"), 1700)
        self.assertEqual(yuan_to_fen("9.5"), 950)
        self.assertEqual(yuan_to_fen("0.1"), 10)

    def test_no_float_error(self):
        """0.1 + 0.2 的经典浮点问题：用整数分必须精确。"""
        a, b = yuan_to_fen("0.1"), yuan_to_fen("0.2")
        self.assertEqual(a + b, 30)
        self.assertEqual(fen_to_yuan_str(a + b), "0.30")

        # 反复累加 0.01 × 100 必须恰好 1.00
        total = sum(yuan_to_fen("0.01") for _ in range(100))
        self.assertEqual(total, 100)
        self.assertEqual(fen_to_yuan_str(total), "1.00")

    def test_currency_symbols_and_spaces(self):
        self.assertEqual(yuan_to_fen("¥26.5"), 2650)
        self.assertEqual(yuan_to_fen("￥26.5"), 2650)
        self.assertEqual(yuan_to_fen(" 26.50 "), 2650)

    def test_rounding_half_up(self):
        self.assertEqual(yuan_to_fen("0.005"), 1)     # 0.5 分 → 进位
        self.assertEqual(yuan_to_fen("0.004"), 0)

    def test_bad_input(self):
        self.assertIsNone(safe_yuan_to_fen(""))
        self.assertIsNone(safe_yuan_to_fen(None))
        self.assertIsNone(safe_yuan_to_fen("免费"))
        with self.assertRaises(ValueError):
            yuan_to_fen(None)

    def test_diff_price_negative(self):
        """diffPrice 实测可为负：'- ¥0.5' → -50 分。"""
        self.assertEqual(parse_diff_price_fen("- ¥0.5"), -50)
        self.assertEqual(parse_diff_price_fen("+ ¥2.5"), 250)
        self.assertEqual(parse_diff_price_fen("+ ¥0"), 0)
        self.assertIsNone(parse_diff_price_fen(""))

    def test_fen_to_yuan_str(self):
        self.assertEqual(fen_to_yuan_str(2650), "26.50")
        self.assertEqual(fen_to_yuan_str(0), "0.00")
        self.assertEqual(fen_to_yuan_str(7), "0.07")


RAW_MENU = {
    "data": {
        "categories": [
            {"name": "鸡肉汉堡/卷", "meals": [{"code": "1450"}, {"code": "1440"}]},
            {"name": "小食甜品/其他", "meals": [{"code": "4810"}]},
            {"name": "饮品", "meals": [{"code": "3050"}]},
        ],
        "meals": {
            "1450": {"name": "麦香鸡", "currentPrice": "17", "originalPrice": "17",
                     "tags": ["单品", "爆款"]},
            "1440": {"name": "麦辣鸡腿汉堡", "currentPrice": "21.5", "originalPrice": "21.5",
                     "tags": ["麦辣", "川香风味"]},
            "4810": {"name": "中薯条", "currentPrice": "12", "originalPrice": "12",
                     "tags": ["小食"]},
            "3050": {"name": "可乐中杯", "currentPrice": "9.5", "originalPrice": "9.5",
                     "tags": ["冷饮"]},
            "9900016309": {"name": "龙焰鸡腿堡四件套", "currentPrice": "29", "originalPrice": "57",
                           "canWithOrder": True, "discountType": "随单购麦金卡优惠"},
        },
    }
}


class TestMenuNormalize(unittest.TestCase):
    def test_units_converted_to_fen(self):
        snap = normalize_menu(RAW_MENU)
        by = {i.code: i for i in snap.items}
        self.assertEqual(by["1450"].current_price_fen, 1700)
        self.assertEqual(by["3050"].current_price_fen, 950)
        self.assertEqual(by["4810"].current_price_fen, 1200)

    def test_with_order_price_trap_flagged(self):
        """canWithOrder 商品必须被打标：价格依赖随单购麦金卡。"""
        snap = normalize_menu(RAW_MENU)
        item = snap.by_code("9900016309")
        self.assertTrue(item.can_with_order)
        self.assertTrue(item.price_is_conditional)
        self.assertEqual(item.current_price_fen, 2900)
        self.assertEqual(item.original_price_fen, 5700)

    def test_normal_item_not_flagged(self):
        snap = normalize_menu(RAW_MENU)
        self.assertFalse(snap.by_code("1450").price_is_conditional)

    def test_menu_digest_changes_when_price_changes(self):
        from mcdradar.menu import menu_digest
        import copy
        a = menu_digest(normalize_menu(RAW_MENU))
        changed = copy.deepcopy(RAW_MENU)
        changed["data"]["meals"]["1450"]["currentPrice"] = "18"
        b = menu_digest(normalize_menu(changed))
        self.assertNotEqual(a, b)

    def test_empty_menu(self):
        snap = normalize_menu({})
        self.assertEqual(snap.size(), 0)


if __name__ == "__main__":
    unittest.main()
