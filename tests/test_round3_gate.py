"""第三轮审计回归测试：**只测当前生产函数**，不测内联复制的旧实现。

对照审计项：
1. P0  `begin_submit()` 必须强制执行完整提交闸门（不依赖调用方先跑 preflight）
2. P1  领券范围变化必须撤销旧确认（券池增加/减少/旧 nonce 重放/跳过重新确认）
3. P1  价格校验结果必须绑定原始证据（不接受外部传入的 ok=true）

⚠️ 全程 Mock，未调用任何真实写操作 MCP 工具。
"""

from __future__ import annotations

import time
import unittest

from _harness import MockMCP, pricing_for, tmp_store  # noqa: F401

from mcdradar.coupon import CouponPool
from mcdradar.coupon_bind import (
    begin_bind, confirm_bind, plan_bind, prepare_bind, preflight_bind, scope_digest,
)
from mcdradar.models import CouponInfo
from mcdradar.order_flow import (
    PRICING_CHECK_TTL, PRICING_SCOPE_NOTE, PRICING_TRUST_BOUNDARY,
    begin_submit, build_order_request, confirm_order, get_pricing_check,
    prepare_order, preflight, record_pricing_check, verify_pricing_covers_items,
)
from mcdradar.order_guard import (
    SUBMITTED, USER_CONFIRMED, ConcurrentTransition, get_record, mark_submitted,
)
from mcdradar.summary import request_digest
from mcdradar.state import StateStore

BASE = {
    "store_code": "1000001", "order_type": 1, "be_type": 1,
    "items": [{"productCode": "1450", "quantity": 1},
              {"productCode": "3050", "quantity": 1}],
    "take_way_code": "locker-out",
}


def req(**kw) -> dict:
    o = dict(BASE)
    o.update(kw)
    return build_order_request(**o)


def pricing(drop: tuple[str, ...] = (), price_fen: int = 2650) -> dict:
    return pricing_for(req(), price_fen=price_fen, drop=drop)


def pool(n: int = 2, prefix: str = "u") -> CouponPool:
    p = CouponPool()
    for i in range(n):
        p.available.append(CouponInfo(title=f"可领券{i}", status="可领取", uid=f"{prefix}{i}"))
    return p


# ===========================================================================
# 1. P0：begin_submit 不可绕过的完整提交闸门
# ===========================================================================

class TestBeginSubmitEnforcesFullGate(unittest.TestCase):
    """审计项 1：begin_submit 不得只查 may_submit。

    修复前：begin_submit(store, digest) 只调 may_submit()，
    已确认但**未完成算价覆盖校验**的订单会被直接推进到 submitted。
    """

    def _confirmed(self, st: StateStore, r: dict) -> str:
        digest = prepare_order(st, r)["digest"]
        confirm_order(st, digest, r, user_confirmed=True)
        return digest

    def test_no_pricing_check_blocked(self):
        """① 未做价格校验直接 begin_submit → 拒绝，且状态不推进。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        out = begin_submit(st, digest, r)
        self.assertFalse(out["ok"])
        self.assertIn("缺少算价覆盖校验", out["reason"])
        self.assertEqual(get_record(st, digest)["status"], USER_CONFIRMED)

    def test_no_pricing_check_never_calls_create_order(self):
        """① 续：闸门拒绝后，宿主不得调用 create-order。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        mock = MockMCP()
        out = begin_submit(st, digest, r)
        if out["ok"]:
            mock.create_order(r)
        self.assertEqual(mock.counts("create-order"), 0)

    def test_failed_pricing_check_blocked(self):
        """②a 算价校验失败（服务端静默丢商品）→ 拒绝。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing(drop=("3050",)))
        out = begin_submit(st, digest, r)
        self.assertFalse(out["ok"])
        self.assertIn("覆盖校验未通过", out["reason"])

    def test_missing_pricing_record_blocked(self):
        """②b 校验记录缺失（例如被 gc 清掉）→ 拒绝。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        st.delete(f"pricing_check_{digest}")
        out = begin_submit(st, digest, r)
        self.assertFalse(out["ok"])
        self.assertIn("缺少算价覆盖校验", out["reason"])

    def test_expired_pricing_check_blocked(self):
        """②c 核价结果过期 → 拒绝。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        far = time.time() + PRICING_CHECK_TTL + 60
        out = begin_submit(st, digest, r, now=far)
        self.assertFalse(out["ok"])
        self.assertIn("已过", out["reason"])

    def test_unreliable_pricing_structure_blocked(self):
        """②d 响应结构不可靠（无 productList）→ 安全失败，拒绝。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r,
                             pricing={"success": True, "data": {"price": 2650}})
        # 记录本身被判为不可靠
        self.assertFalse(get_pricing_check(st, digest)["reliable"])
        out = begin_submit(st, digest, r)
        self.assertFalse(out["ok"])
        self.assertIn("禁止下单", out["reason"])

    def test_tampered_items_blocked(self):
        """③a 确认后改商品 → 拒绝。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        tampered = dict(r)
        tampered["items"] = [{"productCode": "9999", "quantity": 1}]
        self.assertFalse(begin_submit(st, digest, tampered)["ok"])

    def test_tampered_quantity_blocked(self):
        """③b 确认后改数量 → 拒绝。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        tampered = dict(r)
        tampered["items"] = [{"productCode": "1450", "quantity": 2},
                             {"productCode": "3050", "quantity": 1}]
        out = begin_submit(st, digest, tampered)
        self.assertFalse(out["ok"])
        self.assertIn("请求已变更", out["reason"])

    def test_tampered_store_and_other_key_fields_blocked(self):
        """③c 确认后改门店 / 取餐方式 / 备注 / 随单购 → 一律拒绝。"""
        mutations = [
            {"storeCode": "9999999"},
            {"takeWayCode": "in-store"},
            {"remark": "不要辣"},
            {"withOrder": {"code": "CARD"}},
            {"gmServiceCode": "GM1"},
            {"beType": 5, "beCode": "DT01"},
        ]
        for m in mutations:
            with self.subTest(mutation=m):
                st = tmp_store()
                r = req()
                digest = self._confirmed(st, r)
                record_pricing_check(st, digest, request=r, pricing=pricing())
                tampered = dict(r)
                tampered.update(m)
                self.assertNotEqual(request_digest(tampered), digest)
                self.assertFalse(begin_submit(st, digest, tampered)["ok"])

    def test_double_submission_blocked(self):
        """④ 已提交订单再次 begin_submit → 拒绝，attempts 不增加。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        first = begin_submit(st, digest, r)
        self.assertTrue(first["ok"])
        self.assertEqual(first["attempts"], 1)
        again = begin_submit(st, digest, r)
        self.assertFalse(again["ok"])
        self.assertEqual(get_record(st, digest)["attempts"], 1)

    def test_direct_call_without_preflight_is_still_checked(self):
        """⑤ 绕过 Skill 编排、不调 preflight 直接 begin_submit → 闸门依然生效。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        # 刻意**从不**调用 preflight()
        self.assertFalse(begin_submit(st, digest, r)["ok"])
        # 补齐校验后才放行
        record_pricing_check(st, digest, request=r, pricing=pricing())
        self.assertTrue(begin_submit(st, digest, r)["ok"])

    def test_missing_request_blocked(self):
        """⑤ 续：连请求都不传 → 拒绝（不能靠 digest 蒙混过关）。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        self.assertFalse(begin_submit(st, digest)["ok"])
        self.assertFalse(begin_submit(st, digest, None)["ok"])

    def test_happy_path_promotes_once(self):
        """正常路径：校验齐全 → 状态推进且只推进一次。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        out = begin_submit(st, digest, r)
        self.assertTrue(out["ok"])
        self.assertEqual(get_record(st, digest)["status"], SUBMITTED)
        self.assertEqual(out["attempts"], 1)

    def test_preflight_and_begin_submit_agree(self):
        """preflight 与 begin_submit 共用同一闸门，结论必须一致。"""
        st = tmp_store()
        r = req()
        digest = self._confirmed(st, r)
        pf = preflight(st, digest, r)
        self.assertFalse(pf["allowed"])
        self.assertFalse(begin_submit(st, digest, r)["ok"])
        record_pricing_check(st, digest, request=r, pricing=pricing())
        self.assertTrue(preflight(st, digest, r)["allowed"])
        self.assertTrue(begin_submit(st, digest, r)["ok"])


class TestSubmitTOCTOU(unittest.TestCase):
    """TOCTOU：闸门通过之后、写入之前状态被改动 → 中止提交。

    两层防护：
      ① 闸门层 —— may_submit 会在 begin_submit 内部重新读取状态并拒绝
      ② 写入层 —— mark_submitted 在同一次 read-modify-write 内复校验期望状态
    """

    def test_gate_layer_rejects_changed_status(self):
        """① 状态在闸门之前就变了 → begin_submit 直接拒绝。"""
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        confirm_order(st, digest, r, user_confirmed=True)
        record_pricing_check(st, digest, request=r, pricing=pricing())

        rec = get_record(st, digest)
        rec["status"] = SUBMITTED
        rec["attempts"] = 3
        st.write(f"order_{digest}", rec)

        out = begin_submit(st, digest, r)
        self.assertFalse(out["ok"])
        after = get_record(st, digest)
        self.assertEqual(after["status"], SUBMITTED)
        self.assertEqual(after["attempts"], 3)

    def test_write_layer_rechecks_expected_status(self):
        """② 直接调底层 mark_submitted 也要复校验，不符则抛异常而非照写。"""
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        confirm_order(st, digest, r, user_confirmed=True)

        rec = get_record(st, digest)
        rec["status"] = SUBMITTED
        rec["attempts"] = 3
        st.write(f"order_{digest}", rec)

        with self.assertRaises(ConcurrentTransition):
            mark_submitted(st, digest, expected_status=USER_CONFIRMED)
        # 未覆盖别人的写入
        self.assertEqual(get_record(st, digest)["attempts"], 3)

    def test_write_layer_allows_matching_status(self):
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        confirm_order(st, digest, r, user_confirmed=True)
        rec = mark_submitted(st, digest, expected_status=USER_CONFIRMED)
        self.assertEqual(rec["status"], SUBMITTED)
        self.assertEqual(rec["attempts"], 1)


# ===========================================================================
# 2. P1：领券范围变化必须撤销旧确认
# ===========================================================================

class TestBindScopeChange(unittest.TestCase):
    """审计项 2：不得静默替换快照后沿用原确认。"""

    def test_scope_increase_revokes_confirmation(self):
        st = tmp_store()
        prepare_bind(st, pool(2))
        c1 = confirm_bind(st, pool(2), user_confirmed=True)
        self.assertTrue(c1["ok"])

        grown = pool(3, prefix="v")
        c2 = confirm_bind(st, grown, user_confirmed=True)
        self.assertFalse(c2["ok"])
        self.assertTrue(c2["scope_changed"])
        self.assertTrue(c2["requires_redisplay"])
        self.assertIsNone(c2["nonce"])
        # 旧确认已作废
        self.assertEqual(st.read("coupon_bind")["status"], "prepared")
        self.assertFalse(preflight_bind(st, nonce=c1["nonce"])["allowed"])

    def test_scope_decrease_revokes_confirmation(self):
        st = tmp_store()
        prepare_bind(st, pool(3))
        confirm_bind(st, pool(3), user_confirmed=True)
        c = confirm_bind(st, pool(1), user_confirmed=True)
        self.assertFalse(c["ok"])
        self.assertTrue(c["requires_redisplay"])

    def test_skip_reconfirm_cannot_submit(self):
        """未重新展示 + 未重新确认 → begin_bind 一律拒绝。"""
        st = tmp_store()
        prepare_bind(st, pool(2))
        c = confirm_bind(st, pool(2), user_confirmed=True)
        grown = pool(3, prefix="v")
        confirm_bind(st, grown, user_confirmed=True)      # 被拒，确认已作废
        self.assertFalse(begin_bind(st, nonce=c["nonce"])["ok"])
        self.assertFalse(begin_bind(st, nonce=None)["ok"])

    def test_redisplay_then_reconfirm_issues_usable_nonce(self):
        """正确恢复路径：prepare（重新展示）→ confirm → 新凭证可用。"""
        st = tmp_store()
        grown = pool(3, prefix="v")
        prepare_bind(st, pool(2))
        confirm_bind(st, pool(2), user_confirmed=True)
        # 宿主重新展示变化后的范围并重新登记
        self.assertTrue(prepare_bind(st, grown)["ok"])
        plan = plan_bind(grown)
        self.assertEqual(plan["count"], 3)          # 重新展示的是**变化后**的范围
        c3 = confirm_bind(st, grown, user_confirmed=True)
        self.assertTrue(c3["ok"])
        self.assertFalse(c3["scope_changed"])
        self.assertTrue(preflight_bind(st, nonce=c3["nonce"])["allowed"])
        self.assertTrue(begin_bind(st, nonce=c3["nonce"])["ok"])

    def test_old_nonce_replay_after_submit(self):
        """nonce 一次性：提交后再用同一 nonce → 拒绝。"""
        st = tmp_store()
        p = pool(2)
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        self.assertTrue(begin_bind(st, nonce=c["nonce"])["ok"])
        self.assertFalse(begin_bind(st, nonce=c["nonce"])["ok"])
        self.assertIsNone(st.read("coupon_bind")["confirm_nonce"])

    def test_prepare_bind_refuses_reset_while_submitted(self):
        """提交后结果未确认前，不允许重新登记绕过。"""
        st = tmp_store()
        p = pool(2)
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        begin_bind(st, nonce=c["nonce"])
        out = prepare_bind(st, pool(3, prefix="v"))
        self.assertFalse(out["ok"])
        self.assertEqual(st.read("coupon_bind")["status"], "submitted")

    def test_scope_digest_tracks_uid_set(self):
        self.assertNotEqual(scope_digest(pool(2)), scope_digest(pool(3)))
        self.assertEqual(scope_digest(pool(2)), scope_digest(pool(2)))


# ===========================================================================
# 3. P1：价格校验结果必须绑定原始证据
# ===========================================================================

class TestPricingEvidenceBinding(unittest.TestCase):
    """审计项 3：不接受外部直接传入的 ok=true。"""

    def test_forged_ok_from_dropped_item_rejected(self):
        """响应丢了商品，调用方无法靠「声称 ok」放行。"""
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        res = record_pricing_check(st, digest, request=r, pricing=pricing(drop=("3050",)))
        self.assertFalse(res["ok"])
        self.assertEqual(res["record"]["missing"], {"3050": 1})
        confirm_order(st, digest, r, user_confirmed=True)
        pf = preflight(st, digest, r)
        self.assertFalse(pf["allowed"])
        self.assertIn("覆盖校验未通过", pf["pricing_check"]["reason"])

    def test_request_digest_mismatch_rejected(self):
        """用 A 的指纹登记 B 的请求 → 拒绝且不落盘。"""
        st = tmp_store()
        r = req()
        other = req(store_code="9999999")
        res = record_pricing_check(st, "deadbeef", request=r, pricing=pricing())
        self.assertFalse(res["ok"])
        self.assertFalse(res["recorded"])
        self.assertIn("指纹不一致", res["reason"])
        self.assertIsNone(st.read(f"pricing_check_deadbeef"))
        self.assertNotEqual(request_digest(r), request_digest(other))

    def test_missing_request_or_pricing_rejected(self):
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        self.assertFalse(record_pricing_check(st, digest, request=r, pricing={})["ok"])
        self.assertFalse(record_pricing_check(st, digest, request={}, pricing=pricing())["ok"])

    def test_response_digest_binds_to_raw_response(self):
        """同一请求的不同响应 → 证据指纹必须不同。"""
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        a = record_pricing_check(st, digest, request=r, pricing=pricing(price_fen=2650))
        b = record_pricing_check(st, digest, request=r, pricing=pricing(price_fen=1999))
        self.assertNotEqual(a["record"]["response_digest"], b["record"]["response_digest"])
        self.assertEqual(b["record"]["payable_fen"], 1999)

    def test_record_without_evidence_is_rejected_by_gate(self):
        """记录里没有 response_digest（伪造/旧格式）→ 闸门按安全失败拒。"""
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        confirm_order(st, digest, r, user_confirmed=True)
        st.write(f"pricing_check_{digest}",
                 {"digest": digest, "request_digest": digest,
                  "ok": True, "reliable": True, "at": time.time()})
        pf = preflight(st, digest, r)
        self.assertFalse(pf["allowed"])
        self.assertIn("缺少原始响应证据", pf["pricing_check"]["reason"])

    def test_unreliable_structure_fails_closed(self):
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        res = record_pricing_check(st, digest, request=r,
                                   pricing={"success": True, "data": {"price": 2650}})
        self.assertFalse(res["ok"])
        self.assertFalse(res["record"]["reliable"])

    def test_scope_note_and_trust_boundary_exposed(self):
        """结论不得被误读为「服务端保证价格不变」。"""
        self.assertIn("不等于服务端承诺最终订单价格不变", PRICING_SCOPE_NOTE)
        self.assertIn("不能独立证明", PRICING_TRUST_BOUNDARY)
        ok_check = verify_pricing_covers_items(req()["items"], pricing())
        self.assertTrue(ok_check["ok"])
        self.assertIn("不等于服务端承诺最终订单价格不变", ok_check["message"])

    def test_expired_record_rejected(self):
        st = tmp_store()
        r = req()
        digest = prepare_order(st, r)["digest"]
        confirm_order(st, digest, r, user_confirmed=True)
        record_pricing_check(st, digest, request=r, pricing=pricing())
        pf = preflight(st, digest, r, now=time.time() + PRICING_CHECK_TTL + 1)
        self.assertFalse(pf["allowed"])
        self.assertFalse(begin_submit(st, digest, r, now=time.time() + PRICING_CHECK_TTL + 1)["ok"])


if __name__ == "__main__":
    unittest.main()
