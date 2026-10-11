"""T0 上线前预演发现的缺陷 —— 回归测试。

本轮（T0，2026-10-10）用**真实 MCP 只读返回**做全流程预演时发现并修复了一个真实缺陷：

🔴 `normalize_my_coupons` 误解析 `query-my-coupons` 的真实 Markdown：
   真实返回是 `## 券名` + `- **优惠**: ...` / `- **有效期**: ...` 结构，
   旧实现把「每个 bullet 当一张券」，把 12 张真实券解析成 **48 条垃圾条目**
   （标题变成「优惠**: ¥29.9」…），会让券池盘点、到期提醒、领券对比全部失真。

修复：识别 `## ` 标题的**结构化**格式；无标题时保留旧的 bullet 兜底行为。

本文件的用例锁定修复后的行为，防止回退。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from _harness import ROOT  # noqa: F401  确保 scripts 在 sys.path

from mcdradar.coupon import normalize_my_coupons, normalize_store_coupons
from mcdradar.order_flow import verify_pricing_covers_items

FIX = Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    p = FIX / name
    return json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" else p.read_text(encoding="utf-8")


class TestMyCouponsRealMarkdown(unittest.TestCase):
    """真实 query-my-coupons Markdown（2026-10-10 抓取，12 张券）。"""

    def setUp(self):
        self.cs, self.warns = normalize_my_coupons(load("real_my_coupons.md"))

    def test_exactly_twelve_coupons(self):
        """🔴 修复前为 48 条；真实券数应为 12。"""
        self.assertEqual(len(self.cs), 12)

    def test_no_field_names_leak_into_title(self):
        """标题不得包含字段名/加粗标记（修复前的典型垃圾形如「优惠**: ¥29.9」）。"""
        for c in self.cs:
            self.assertNotIn("**", c.title)
            self.assertNotIn("：", c.title)
            self.assertNotIn("优惠", c.title)
            self.assertNotIn("有效期", c.title)
            self.assertNotIn("标签", c.title)

    def test_titles_are_real_coupon_names(self):
        titles = {c.title for c in self.cs}
        self.assertTrue(titles.issubset({
            "巧克力味厚松饼猪柳蛋套餐", "火腿扒堡早餐两件套", "芝香脆鸡扒堡早餐两件套",
            "高达芝士蛋堡早餐两件套", "大脆鸡扒堡早餐两件套",
        }), f"出现非券名标题：{titles}")

    def test_duplicate_titles_have_unique_uid(self):
        """同名券存在多个批次（有效期不同）→ uid 必须唯一。"""
        dup = [c for c in self.cs if c.title == "巧克力味厚松饼猪柳蛋套餐"]
        self.assertGreaterEqual(len(dup), 2)
        self.assertEqual(len({c.uid for c in dup}), len(dup))

    def test_validity_parsed(self):
        c = self.cs[0]
        self.assertEqual(c.valid_from, "2026-10-10 05:00")
        self.assertEqual(c.valid_to, "2026-10-10 10:29")

    def test_labels_and_today_expiring(self):
        """「今日到期」标签必须被解析出来（到期提醒依赖它）。"""
        todo = [c for c in self.cs if "今日到期" in (c.labels or [])]
        self.assertEqual(len(todo), 1)
        self.assertEqual(todo[0].valid_from, "2026-10-10 05:00")

    def test_status_is_held(self):
        self.assertTrue(all(c.status == "已持有" for c in self.cs))

    def test_warns_about_no_coupon_id(self):
        """必须如实提示：无 couponId，不能驱动算价。"""
        self.assertTrue(any("couponId" in w for w in self.warns))


class TestMyCouponsLegacyFallback(unittest.TestCase):
    """无 `## ` 标题时保留旧行为，避免破坏其它形态。"""

    def test_bullet_fallback(self):
        text = "- 麦旋风买一送一\n- 免费脆薯饼\n"
        cs, _ = normalize_my_coupons(text)
        self.assertEqual([c.title for c in cs], ["麦旋风买一送一", "免费脆薯饼"])
        self.assertEqual(len({c.uid for c in cs}), 2)

    def test_empty_input(self):
        cs, warns = normalize_my_coupons("")
        self.assertEqual(cs, [])
        self.assertTrue(warns)


class TestUnavailableItemHardError(unittest.TestCase):
    """真实观测：不可售商品返回**硬报错 60006**，不是静默丢弃；不得误判为成功。"""

    def test_hard_error_is_fail_closed(self):
        raw = load("real_calculate_price_unavailable_60006.json")
        self.assertFalse(raw["success"])
        self.assertEqual(raw["code"], 60006)
        chk = verify_pricing_covers_items(raw["_request"]["items"], raw)
        self.assertFalse(chk["ok"])
        self.assertFalse(chk["structure_known"])
        self.assertFalse(chk["reliable"])

    def test_error_response_never_used_as_pricing_evidence(self):
        """报错响应不得被当成「覆盖完整」而下单。"""
        raw = load("real_calculate_price_unavailable_60006.json")
        chk = verify_pricing_covers_items([{"productCode": "1450", "quantity": 1}], raw)
        self.assertFalse(chk["ok"])


class TestStoreCouponsEmptyReal(unittest.TestCase):
    """真实观测：当前门店+到店场景 query-store-coupons 为空。"""

    def test_empty_is_zero_not_fabricated(self):
        self.assertEqual(normalize_store_coupons(load("real_store_coupons_empty.json")), [])


if __name__ == "__main__":
    unittest.main()
