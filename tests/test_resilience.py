"""风险分类、鉴权/网络/限流策略、敏感信息红线测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from _harness import ROOT, tmp_store  # noqa: F401

from mcdradar.state import StateStore, scrub
from mcdradar.tool_risk import (
    FORBIDDEN_THIS_ROUND, NO_RETRY_TOOLS, READ, WRITE, classify,
    reconcile_tool_for, retry_policy, assert_may_call,
)


class TestToolRisk(unittest.TestCase):
    def test_read_tools(self):
        for t in ("query-meals", "calculate-price", "query-order", "order-list",
                  "available-coupons", "campaign-calendar", "query-store-coupons"):
            self.assertEqual(classify(t), READ, t)

    def test_write_tools(self):
        for t in ("create-order", "auto-bind-coupons", "cancel-order",
                  "draw-lottery", "mall-create-order"):
            self.assertEqual(classify(t), WRITE, t)

    def test_unknown_tool_treated_as_write(self):
        """未登记工具按最保守处理，不允许自动重试。"""
        self.assertEqual(classify("some-future-tool"), WRITE)
        self.assertEqual(retry_policy("some-future-tool").max_auto_retries, 0)

    def test_read_can_retry_write_cannot(self):
        self.assertEqual(retry_policy("query-meals").max_auto_retries, 2)
        self.assertEqual(retry_policy("create-order").max_auto_retries, 0)
        self.assertEqual(retry_policy("auto-bind-coupons").max_auto_retries, 0)

    def test_no_retry_set(self):
        self.assertIn("create-order", NO_RETRY_TOOLS)
        self.assertIn("auto-bind-coupons", NO_RETRY_TOOLS)
        # 只读工具绝不能出现在禁止重试集合里
        self.assertNotIn("calculate-price", NO_RETRY_TOOLS)

    def test_reconcile_tools(self):
        self.assertIn("order-list", reconcile_tool_for("create-order"))
        self.assertIn("query-order", reconcile_tool_for("create-order"))
        self.assertIn("available-coupons", reconcile_tool_for("auto-bind-coupons"))
        self.assertIn("query-my-coupons", reconcile_tool_for("auto-bind-coupons"))

    def test_create_order_policy_text(self):
        p = retry_policy("create-order")
        self.assertIn("禁止再次提交", p.on_unknown_result)

    def test_auto_bind_policy_text(self):
        p = retry_policy("auto-bind-coupons")
        self.assertIn("禁止再次领取", p.on_unknown_result)


class TestAuthorization(unittest.TestCase):
    def test_all_write_tools_blocked_when_real(self):
        for t in FORBIDDEN_THIS_ROUND:
            with self.assertRaises(PermissionError, msg=t):
                assert_may_call(t, real=True)

    def test_read_tools_allowed_when_real(self):
        assert_may_call("query-meals", real=True)      # 不抛异常
        assert_may_call("calculate-price", real=True)

    def test_write_tools_not_blocked_for_mock(self):
        """Mock（real=False）不受限，便于本轮演练写操作流程。"""
        assert_may_call("create-order", real=False)
        assert_may_call("auto-bind-coupons", real=False)


class TestSensitiveScrub(unittest.TestCase):
    def test_scrub_removes_sensitive_keys(self):
        data = {
            "orderId": "O1",
            "token": "SECRET",
            "authorization": "Bearer SECRET",
            "mobilePhone": "13800000000",
            "deliveryInfo": {"address": "某某路 1 号", "customerNickname": "陈"},
            "items": [{"code": "1450"}],
        }
        out = scrub(data)
        self.assertNotIn("token", out)
        self.assertNotIn("authorization", out)
        self.assertNotIn("mobilePhone", out)
        self.assertNotIn("address", out["deliveryInfo"])
        self.assertEqual(out["orderId"], "O1")
        self.assertEqual(out["items"][0]["code"], "1450")

    def test_state_write_is_scrubbed(self):
        from mcdradar.order_guard import prepare
        from mcdradar.summary import request_digest
        st = tmp_store()
        req = {"storeCode": "1", "orderType": 1, "beType": 1,
               "items": [{"productCode": "1450", "quantity": 1}],
               "token": "SECRET"}
        digest = request_digest(req)
        prepare(st, digest, req)
        raw = st.path_for(f"order_{digest}").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", raw)


class TestNoTokenInSource(unittest.TestCase):
    def test_scripts_do_not_contain_auth_material(self):
        """脚本层不得出现鉴权头 / Token 处理 —— MCP 调用由宿主负责。"""
        bad = []
        for p in (ROOT / "scripts").rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            for needle in ("Authorization", "Bearer ", "mcp.mcd.cn"):
                if needle in text:
                    bad.append(f"{p.name}:{needle}")
        self.assertEqual(bad, [], f"脚本不应包含鉴权/端点实现: {bad}")

    def test_project_has_no_env_token_requirements(self):
        """本轮零依赖：不应出现 pip 安装要求。"""
        req = ROOT / "requirements.txt"
        if req.exists():
            self.assertEqual(req.read_text(encoding="utf-8").strip(), "",
                             "MVP 应保持零第三方依赖")


class TestStateStore(unittest.TestCase):
    def test_roundtrip(self):
        st = tmp_store()
        st.write("k", {"a": 1})
        self.assertEqual(st.read("k"), {"a": 1})
        self.assertIsNone(st.read("missing"))

    def test_corrupted_file_returns_default(self):
        st = tmp_store()
        st.path_for("broken").write_text("{not json", encoding="utf-8")
        self.assertIsNone(st.read("broken"))

    def test_atomic_write_no_tmp_left(self):
        st = tmp_store()
        st.write("k", {"x": 1})
        tmps = list(st.dir.glob("*.tmp"))
        self.assertEqual(tmps, [])


if __name__ == "__main__":
    unittest.main()
