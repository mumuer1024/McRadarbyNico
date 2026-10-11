#!/usr/bin/env python3
"""运行全部单元测试并输出汇总报告。

用法：
    <python> tests/run_tests.py [--verbose] [--json out.json]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SUITES = [
    ("金额精度与随单购价格陷阱", "test_money_and_menu"),
    ("组合生成/去重/预算/辣度/忌口", "test_combo_and_prefs"),
    ("套餐子项负差价 + 单券试算 + 同名券", "test_swap_and_coupon"),
    ("活动解析与快照 diff", "test_campaign"),
    ("订单闭环：一致性/闸门/防重/支付期限/状态", "test_order"),
    ("一键领券：确认/不重试/券池对比", "test_bind"),
    ("风险分类/鉴权/敏感信息/状态存储", "test_resilience"),
    ("P0/P1 交叉审查修复证据（旧逻辑对照）", "test_fix_evidence"),
    ("真实只读数据集成（fixtures/）", "test_real_fixtures"),
    ("第三轮审计：提交闸门/领券范围/价格证据", "test_round3_gate"),
    ("T0 预演回归：我的券解析/空券池/不可售报错", "test_t0_findings"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", default=None, help="把结果汇总写为 JSON")
    a = ap.parse_args()

    report = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "suites": [],
        "total": 0, "passed": 0, "failed": 0, "errors": 0,
    }
    overall_ok = True

    for title, module in SUITES:
        buf = io.StringIO()
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName(module)
        runner = unittest.TextTestRunner(stream=buf, verbosity=2 if a.verbose else 1)
        result = runner.run(suite)

        n = result.testsRun
        failed = len(result.failures)
        errors = len(result.errors)
        passed = n - failed - errors
        report["suites"].append({
            "title": title, "module": module, "total": n,
            "passed": passed, "failed": failed, "errors": errors,
            "failures": [f"{t}" for t, _ in result.failures],
            "error_list": [f"{t}" for t, _ in result.errors],
        })
        report["total"] += n
        report["passed"] += passed
        report["failed"] += failed
        report["errors"] += errors
        if failed or errors:
            overall_ok = False
            print(buf.getvalue())

    print("=" * 68)
    print(f"测试汇总：{report['passed']}/{report['total']} 通过"
          f"（失败 {report['failed']}，错误 {report['errors']}）")
    print("=" * 68)
    for s in report["suites"]:
        flag = "✅" if (s["failed"] == 0 and s["errors"] == 0) else "❌"
        print(f"{flag} {s['title']:<40} {s['passed']}/{s['total']}")

    if a.json:
        Path(a.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n汇总已写入：{a.json}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
