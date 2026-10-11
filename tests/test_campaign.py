"""活动日历解析与快照 diff。

重点：活动接口无结构化起止日期 —— 不得把文案日期当有效期，不得做倒计时。
"""

from __future__ import annotations

import unittest

from _harness import ROOT, tmp_store  # noqa: F401

from mcdradar.campaign import diff_snapshot, load_snapshot, parse_campaign_calendar, render_entries, save_snapshot

CALENDAR_MD = """### 麦麦活动日历
2026年10月7日 往期回顾
- 麦麦省会员日
  全场 15.9元起，会员专享
  ![img](https://img.mcd.cn/a.png)
2026年10月9日 今日
- 1+1随心配回归
  13.9r 起随心拼
  ![img](https://img.mcd.cn/b.png)
2026年10月10日
- 新品上市
  29.9元起
"""


class TestParse(unittest.TestCase):
    def test_entries_parsed(self):
        es = parse_campaign_calendar(CALENDAR_MD)
        self.assertEqual(len(es), 3)
        self.assertEqual(es[1].day_tag, "今日")
        self.assertEqual(es[1].title, "1+1随心配回归")

    def test_no_structured_dates(self):
        """解析结果里 valid_from / valid_to 必须是 None（接口没给）。"""
        for e in parse_campaign_calendar(CALENDAR_MD):
            self.assertIsNone(e.valid_from)
            self.assertIsNone(e.valid_to)
            self.assertEqual(e.scope, "未知")

    def test_date_group_is_label_not_validity(self):
        es = parse_campaign_calendar(CALENDAR_MD)
        self.assertEqual(es[0].date_group, "2026-10-07")
        # date_group 只是分组标签，不得当作有效期使用
        self.assertIsNone(es[0].valid_to)

    def test_empty(self):
        self.assertEqual(parse_campaign_calendar({}), [])

    def test_render_marks_unknown(self):
        md = render_entries(parse_campaign_calendar(CALENDAR_MD))
        self.assertIn("未知", md)
        self.assertIn("不作为活动有效期", md)


class TestSnapshotDiff(unittest.TestCase):
    def test_first_run(self):
        st = tmp_store()
        es = parse_campaign_calendar(CALENDAR_MD)
        d = diff_snapshot(load_snapshot(st), es)
        self.assertTrue(d["first_run"])

    def test_diff_detects_new_and_gone(self):
        st = tmp_store()
        es1 = parse_campaign_calendar(CALENDAR_MD)
        save_snapshot(st, es1)

        es2 = parse_campaign_calendar(CALENDAR_MD.replace("新品上市", "全新套餐"))
        d = diff_snapshot(load_snapshot(st), es2)
        self.assertFalse(d["first_run"])
        self.assertTrue(any("全新套餐" in k for k in d["new"]))
        self.assertTrue(any("新品上市" in k for k in d["gone"]))
        self.assertTrue(len(d["still"]) >= 2)

    def test_diff_note_warns_no_validity(self):
        st = tmp_store()
        save_snapshot(st, parse_campaign_calendar(CALENDAR_MD))
        d = diff_snapshot(load_snapshot(st), parse_campaign_calendar(CALENDAR_MD))
        self.assertIn("不能据此推断活动有效期", d["note"])


if __name__ == "__main__":
    unittest.main()
