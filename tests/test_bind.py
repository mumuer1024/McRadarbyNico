"""一键领券闸门测试：未确认绝不调用、响应不明绝不重试、批量说明、前后对比。"""

from __future__ import annotations

import unittest

import time

from _harness import MockMCP, ROOT, host_bind, host_bind_retry_happy, tmp_store  # noqa: F401

from mcdradar.coupon_bind import (
    BATCH_NOTICE, CONFIRM_TTL, TRUST_BOUNDARY_BIND, begin_bind, compare_pools,
    confirm_bind, finish_unknown as coupon_bind_finish_unknown, finish_done,
    plan_bind, prepare_bind,
    preflight_bind, revoke_bind_confirmation, scope_digest,
)
from mcdradar.coupon import CouponPool
from mcdradar.models import CouponInfo
from mcdradar.tool_risk import assert_may_call
from mcdradar.state import StateStore


def pool(n_claimable: int = 2, uid_prefix: str = "u") -> CouponPool:
    p = CouponPool()
    for i in range(n_claimable):
        p.available.append(CouponInfo(title=f"可领券{i}", status="可领取", uid=f"{uid_prefix}{i}"))
    p.available.append(CouponInfo(title="已领券", status="已领取", uid="old"))
    return p


class TestPlan(unittest.TestCase):
    def test_plan_lists_claimable(self):
        out = plan_bind(pool(2))
        self.assertEqual(out["count"], 2)
        self.assertTrue(out["can_proceed"])
        self.assertEqual(len(out["claimable"]), 2)

    def test_batch_notice_present(self):
        """必须明确告知是批量领取全部，且没有单张领取接口。"""
        out = plan_bind(pool(2))
        self.assertEqual(out["notice"], BATCH_NOTICE)
        self.assertIn("批量领取", out["notice"])
        self.assertIn("没有提供单张领取接口", out["notice"])

    def test_nothing_to_claim(self):
        out = plan_bind(pool(0))
        self.assertFalse(out["can_proceed"])
        self.assertIn("无需领取", out["confirm_prompt"])


class TestConfirmGate(unittest.TestCase):
    def test_no_record_blocked(self):
        st = tmp_store()
        mock = MockMCP()
        out = host_bind(st, mock, pool=pool(), user_confirmed=True)
        self.assertFalse(out["called"])
        self.assertEqual(mock.counts("auto-bind-coupons"), 0)

    def test_user_not_confirmed_blocked(self):
        """未确认 → 绝不调用 auto-bind-coupons。"""
        st = tmp_store()
        prepare_bind(st, pool())
        mock = MockMCP()
        out = host_bind(st, mock, pool=pool(), user_confirmed=False)
        self.assertFalse(out["called"])
        self.assertEqual(mock.counts("auto-bind-coupons"), 0)
        self.assertIn("未收到用户明确确认", out["preflight"]["reason"])

    def test_real_call_blocked_without_authorization(self):
        with self.assertRaises(PermissionError):
            assert_may_call("auto-bind-coupons", real=True)

    def test_confirmed_calls_once(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        mock = MockMCP()
        out = host_bind(st, mock, pool=p, user_confirmed=True, behavior="success")
        self.assertTrue(out["called"])
        self.assertEqual(mock.counts("auto-bind-coupons"), 1)

    # ---- P0-3 回归：不得跳过确认直接进入 submitted ------------------------
    def test_begin_bind_directly_blocked(self):
        """🔴 修复前：begin_bind(store) 会无条件把状态改成 submitted。
        现在必须持确认凭证，否则拒绝。"""
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        b = begin_bind(st)
        self.assertFalse(b["ok"])
        self.assertEqual(st.read("coupon_bind")["status"], "prepared")
        self.assertIn("只有经过 confirm_bind", b["reason"])

    def test_begin_bind_without_nonce_blocked(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        confirm_bind(st, p, user_confirmed=True)
        b = begin_bind(st)                # 不传 nonce
        self.assertFalse(b["ok"])
        self.assertIn("确认凭证", b["reason"])

    def test_begin_bind_with_wrong_nonce_blocked(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        confirm_bind(st, p, user_confirmed=True)
        self.assertFalse(begin_bind(st, nonce="WRONG")["ok"])

    def test_prepared_but_not_confirmed_preflight_blocked(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        pf = preflight_bind(st, nonce="anything", user_confirmed=True)
        self.assertFalse(pf["allowed"])
        self.assertIn("只有经过 confirm_bind", pf["reason"])

    def test_confirm_bind_without_user_flag_blocked(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=False)
        self.assertFalse(c["ok"])
        self.assertEqual(st.read("coupon_bind")["status"], "prepared")

    def test_expired_confirmation_blocked(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        far = time.time() + CONFIRM_TTL + 60
        pf = preflight_bind(st, nonce=c["nonce"], now=far)
        self.assertFalse(pf["allowed"])
        self.assertIn("重新确认", pf["reason"])

    def test_scope_change_invalidates_and_can_reconfirm(self):
        """券池范围变化 → 旧确认**立即作废**，必须重新展示后重新确认。"""
        st = tmp_store()
        before = pool(2)
        prepare_bind(st, before)
        c1 = confirm_bind(st, before, user_confirmed=True)
        self.assertFalse(c1["scope_changed"])

        after = pool(3, uid_prefix="v")     # 券池变了
        c2 = confirm_bind(st, after, user_confirmed=True)

        # 🔴 新行为：不得沿用新快照静默续期，直接拒绝并作废旧确认
        self.assertTrue(c2["scope_changed"])
        self.assertFalse(c2["ok"])
        self.assertTrue(c2["requires_redisplay"])
        self.assertIsNone(c2["nonce"])
        self.assertEqual(st.read("coupon_bind")["status"], "prepared")

        # 旧凭证同时失效
        self.assertFalse(preflight_bind(st, nonce=c1["nonce"])["allowed"])
        # 未重新登记前，无法用任何凭证提交
        self.assertFalse(begin_bind(st, nonce=c1["nonce"])["ok"])

        # 正确恢复路径：重新登记（= 重新展示范围）→ 用户重新确认 → 新凭证可用
        self.assertTrue(prepare_bind(st, after)["ok"])
        c3 = confirm_bind(st, after, user_confirmed=True)
        self.assertTrue(c3["ok"])
        self.assertFalse(c3["scope_changed"])
        self.assertTrue(preflight_bind(st, nonce=c3["nonce"])["allowed"])

    def test_scope_shrink_also_invalidates(self):
        """券池**减少**同样必须作废旧确认（不只是增加）。"""
        st = tmp_store()
        prepare_bind(st, pool(3))
        confirm_bind(st, pool(3), user_confirmed=True)
        c = confirm_bind(st, pool(1), user_confirmed=True)
        self.assertFalse(c["ok"])
        self.assertTrue(c["scope_changed"])
        self.assertFalse(begin_bind(st, nonce=None)["ok"])

    def test_scope_digest_differs_by_uid_not_title(self):
        """同名重复批次：uid 不同 → 范围指纹必须不同。"""
        a = CouponPool(available=[
            CouponInfo(title="同名券", status="可领取", uid="a1"),
            CouponInfo(title="同名券", status="可领取", uid="a2")])
        b = CouponPool(available=[
            CouponInfo(title="同名券", status="可领取", uid="a1"),
            CouponInfo(title="同名券", status="可领取", uid="a9")])
        self.assertNotEqual(scope_digest(a), scope_digest(b))

    def test_revoke_bind_confirmation(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        revoke_bind_confirmation(st)
        self.assertFalse(preflight_bind(st, nonce=c["nonce"])["allowed"])

    def test_nonce_is_one_shot(self):
        """凭证一次性：提交后立即作废，不能用于第二次。"""
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        self.assertTrue(begin_bind(st, nonce=c["nonce"])["ok"])
        self.assertFalse(begin_bind(st, nonce=c["nonce"])["ok"])

    def test_trust_boundary_declared(self):
        self.assertIn("无法独立证明", TRUST_BOUNDARY_BIND)


class TestNoAutoRetry(unittest.TestCase):
    def test_timeout_never_retried(self):
        """领券超时 → 结果未知 → 想重试 3 次，实际只调用 1 次。"""
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        mock = MockMCP()
        r = host_bind_retry_happy(st, mock, attempts=3, behavior="timeout", pool=p)
        self.assertEqual(r["actual_calls"], 1)

    def test_unclear_response_never_retried(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        mock = MockMCP()
        r = host_bind_retry_happy(st, mock, attempts=3, behavior="unclear", pool=p)
        self.assertEqual(r["actual_calls"], 1)
        self.assertEqual(st.read("coupon_bind")["status"], "unknown")

    def test_unknown_reconcile_uses_pool_query(self):
        st = tmp_store()
        prepare_bind(st, pool())
        out = coupon_bind_finish_unknown(st, "超时")
        self.assertIn("禁止自动再次领取", out["message"])
        tools = out["reconcile"]["tools"]
        self.assertIn("available-coupons", tools)
        self.assertIn("query-my-coupons", tools)

    def test_unknown_requires_reauthorize(self):
        """结果未知 → 再次领取必须 reauthorize=True（用户新的明确授权）。"""
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        coupon_bind_finish_unknown(st, "超时")
        c = confirm_bind(st, p, user_confirmed=True)
        self.assertFalse(c["ok"])
        self.assertIn("再次领取需用户新的明确授权", c["reason"])
        c2 = confirm_bind(st, p, user_confirmed=True, reauthorize=True)
        self.assertTrue(c2["ok"])

    def test_unknown_blocked_even_with_stale_nonce(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        coupon_bind_finish_unknown(st, "超时")
        self.assertFalse(preflight_bind(st, nonce=c["nonce"])["allowed"])

    def test_already_submitted_blocked(self):
        st = tmp_store()
        p = pool()
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        begin_bind(st, nonce=c["nonce"])
        mock = MockMCP()
        out = host_bind(st, mock, pool=p, user_confirmed=True)
        self.assertFalse(out["called"])
        self.assertEqual(mock.counts("auto-bind-coupons"), 0)


class TestPoolDiff(unittest.TestCase):
    def test_diff_after_bind(self):
        st = tmp_store()
        before = pool(3)
        prepare_bind(st, before)
        after = pool(0, uid_prefix="x")
        out = finish_done(st, after)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["diff"]["before_claimable"], 3)
        self.assertEqual(out["diff"]["after_claimable"], 0)

    def test_duplicate_titles_not_collapsed(self):
        """同名重复批次：uid 不同，不能按券名当成同一张。"""
        before = CouponPool(available=[
            CouponInfo(title="同名券", status="可领取", uid="a1"),
            CouponInfo(title="同名券", status="可领取", uid="a2"),
        ])
        after = CouponPool(available=[])
        d = compare_pools(before, after)
        self.assertEqual(d["before_claimable"], 2)
        self.assertEqual(d["confirmed_no_longer_claimable"], 2)


if __name__ == "__main__":
    unittest.main()
