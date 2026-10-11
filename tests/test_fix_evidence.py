"""修复证据测试（P0/P1 交叉审查专项）。

本文件的目的不是重复覆盖，而是**用代码复现修复前的旧逻辑**，
证明审查指出的 5 项问题真实存在，且新实现确实改变了行为方向。

每条测试包含两段：
  1. `*_old_*` —— 修复前的实现（内联复现，仅供对照，不参与生产代码路径）
  2. 断言：旧逻辑不安全 / 新逻辑安全

这样即使将来有人不小心回退改动，这里会立刻失败。
"""

from __future__ import annotations

import hashlib
import json
import time
import unittest

from _harness import MockMCP, pricing_for, prepare_confirm, tmp_store  # noqa: F401

from mcdradar.coupon import CouponPool
from mcdradar.coupon_bind import (
    begin_bind, confirm_bind, prepare_bind, preflight_bind, scope_digest,
)
from mcdradar.models import CouponInfo
from mcdradar.order_flow import (
    TRUST_BOUNDARY, build_order_request, confirm_order, prepare_order,
    preflight, validate_order_request, verify_pricing_covers_items,
)
from mcdradar.order_guard import may_submit, PREPARED
from mcdradar.summary import canonical_json, request_digest


# ===========================================================================
# 1. 订单摘要指纹覆盖不完整（P0）
# ===========================================================================

# 修复前的白名单（这就是被指出的漏洞来源）
_OLD_DIGEST_FIELDS = (
    "storeCode", "orderType", "beType", "beCode", "takeWayCode",
    "addressId", "reservationDate", "needTableware", "items",
)


def old_request_digest(request: dict) -> str:
    picked = {k: request.get(k) for k in _OLD_DIGEST_FIELDS
              if request.get(k) not in (None, "", [], {})}
    return hashlib.sha256(canonical_json(picked).encode("utf-8")).hexdigest()


class TestDigestCoverageFix(unittest.TestCase):
    """证明：旧白名单漏掉 remark / withOrder / gmServiceCode。"""

    def _req(self, **kw):
        base = dict(store_code="1", order_type=2, be_type=6, be_code="G",
                    address_id="A1", items=[{"productCode": "x", "quantity": 1}])
        base.update(kw)
        return build_order_request(**base)

    def test_old_ignores_remark_new_does_not(self):
        a = self._req(remark="不要辣")
        b = self._req(remark="多加辣")
        self.assertEqual(old_request_digest(a), old_request_digest(b))   # 旧：漏
        self.assertNotEqual(request_digest(a), request_digest(b))          # 新：覆盖

    def test_old_ignores_with_order_new_does_not(self):
        a = self._req()
        b = self._req(with_order={"cardType": "1"})
        self.assertEqual(old_request_digest(a), old_request_digest(b))
        self.assertNotEqual(request_digest(a), request_digest(b))

    def test_old_ignores_gm_service_code_new_does_not(self):
        a = self._req()
        b = self._req(gm_service_code="GM9")
        self.assertEqual(old_request_digest(a), old_request_digest(b))
        self.assertNotEqual(request_digest(a), request_digest(b))

    def test_new_still_excludes_internal_metadata(self):
        a = self._req()
        b = dict(a, _est_payable_fen=1, clientRequestId="x", traceId="y")
        self.assertEqual(request_digest(a), request_digest(b))


# ===========================================================================
# 2. 用户确认必须是可验证状态（P0）
# ===========================================================================

class TestConfirmationStateFix(unittest.TestCase):
    """证明：修复前 prepared 状态即可提交，现在必须 user_confirmed。"""

    def test_old_rule_would_allow_unconfirmed_submit(self):
        """旧规则：status == prepared → allowed=True。"""
        st = tmp_store()
        req = build_order_request(store_code="1000001", order_type=1, be_type=1,
                                  items=[{"productCode": "1450", "quantity": 1}],
                                  take_way_code="locker-out")
        digest = prepare_order(st, req)["digest"]

        self.assertEqual(may_submit(st, digest)["status"], PREPARED)
        # 新规则：prepared 一律拒绝
        self.assertFalse(may_submit(st, digest)["allowed"])

    def test_unconfirmed_preflight_reports_missing_confirmation(self):
        st = tmp_store()
        req = build_order_request(store_code="1000001", order_type=1, be_type=1,
                                  items=[{"productCode": "1450", "quantity": 1}],
                                  take_way_code="locker-out")
        digest = prepare_order(st, req)["digest"]
        pf = preflight(st, digest, req)
        self.assertFalse(pf["allowed"])
        self.assertIn("尚未收到用户最终确认", pf["guard"]["reason"])

    def test_trust_boundary_is_declared_not_claimed(self):
        """不得声称纯 Python 能证明用户真实意图。"""
        self.assertIn("无法独立证明", TRUST_BOUNDARY)
        self.assertIn("禁止由模型推断", TRUST_BOUNDARY)


# ===========================================================================
# 3. 领券状态机前置校验（P0）
# ===========================================================================

class TestBindPreflightFix(unittest.TestCase):
    """证明：修复前 begin_bind() 可无条件进入 submitted。"""

    def _pool(self, n=2):
        p = CouponPool()
        for i in range(n):
            p.available.append(CouponInfo(title=f"券{i}", status="可领取", uid=f"u{i}"))
        return p

    def test_old_begin_bind_would_skip_gate(self):
        """旧实现等价物：直接把状态写成 submitted，不做任何校验。"""
        st = tmp_store()
        p = self._pool()
        prepare_bind(st, p)

        # 旧行为复现
        rec = st.read("coupon_bind") or {}
        rec["status"] = "submitted"
        st.write("coupon_bind", rec)
        self.assertEqual(st.read("coupon_bind")["status"], "submitted")  # 旧：被绕过

        # 新实现：拒绝
        st2 = tmp_store()
        prepare_bind(st2, p)
        self.assertFalse(begin_bind(st2)["ok"])
        self.assertEqual(st2.read("coupon_bind")["status"], "prepared")

    def test_confirmation_bound_to_scope_snapshot(self):
        st = tmp_store()
        p = self._pool(2)
        prepare_bind(st, p)
        c = confirm_bind(st, p, user_confirmed=True)
        self.assertTrue(c["ok"])
        # 换一个券池 → 范围指纹变化 → 旧凭证不可用
        p2 = self._pool(3)
        self.assertNotEqual(scope_digest(p), scope_digest(p2))
        # 🔴 第三轮加固：券池变化**立即作废**旧确认，
        # 不允许「静默替换快照后沿用原确认」续期签发新凭证
        c2 = confirm_bind(st, p2, user_confirmed=True)
        self.assertTrue(c2["scope_changed"])
        self.assertFalse(c2["ok"])
        self.assertTrue(c2["requires_redisplay"])
        self.assertFalse(preflight_bind(st, nonce=c["nonce"])["allowed"])
        # 重新登记（重新展示范围）+ 重新确认后才拿到可用凭证
        prepare_bind(st, p2)
        c3 = confirm_bind(st, p2, user_confirmed=True)
        self.assertTrue(preflight_bind(st, nonce=c3["nonce"])["allowed"])


# ===========================================================================
# 4. beCode 校验（P1）
# ===========================================================================

def old_validate_becode(req: dict) -> list[str]:
    """修复前的规则：orderType=1 一律不许 beCode。"""
    errs = []
    if req.get("orderType") == 1 and req.get("beCode"):
        errs.append("beType=1（到店自取）不应传 beCode")
    return errs


class TestBeCodeFix(unittest.TestCase):
    def test_old_rule_falsely_rejects_drive_through(self):
        """得来速 = orderType 1 + beType 5 + beCode 必传。旧规则会误报。"""
        req = build_order_request(store_code="1", order_type=1, be_type=5, be_code="DT01",
                                  take_way_code="drive-through",
                                  items=[{"productCode": "1450", "quantity": 1}])
        self.assertTrue(old_validate_becode(req), "旧规则确实会误报（漏洞成立）")
        self.assertEqual(validate_order_request(req), [], "新规则必须放行")

    def test_new_rule_still_rejects_becode_on_be_type1(self):
        req = build_order_request(store_code="1", order_type=1, be_type=1,
                                  items=[{"productCode": "x"}], take_way_code="t")
        req["beCode"] = "X"
        self.assertTrue(any("不应传 beCode" in e for e in validate_order_request(req)))


# ===========================================================================
# 5. 价格覆盖校验接入 preflight（P0）
# ===========================================================================

class TestPricingGateFix(unittest.TestCase):
    """证明：修复前 preflight 不检查算价覆盖，静默丢商品可以一路走到下单。"""

    def _req(self):
        return build_order_request(store_code="1000001", order_type=1, be_type=1,
                                   items=[{"productCode": "1450", "quantity": 1},
                                          {"productCode": "3050", "quantity": 1}],
                                   take_way_code="locker-out")

    def test_silent_drop_detected(self):
        """复现实测现象：请求 2 件，productList 只回 1 件。"""
        req = self._req()
        bad = pricing_for(req, drop=("3050",))
        r = verify_pricing_covers_items(req["items"], bad)
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], {"3050": 1})

    def test_preflight_blocks_without_pricing_check(self):
        """修复前：确认后即可提交；现在缺少校验记录一律拒绝。"""
        st = tmp_store()
        req = self._req()
        digest = prepare_order(st, req)["digest"]
        confirm_order(st, digest, req, user_confirmed=True)
        pf = preflight(st, digest, req)
        self.assertFalse(pf["allowed"])
        self.assertIn("算价", pf["pricing_check"]["reason"])

    def test_pricing_gate_is_part_of_preflight_not_optional(self):
        """只有显式 require_pricing_check=False 才能跳过（仅供测试），默认必查。"""
        st = tmp_store()
        req = self._req()
        digest = prepare_confirm(st, req)["digest"]
        self.assertTrue(preflight(st, digest, req)["allowed"])
        self.assertTrue(preflight(st, digest, req, require_pricing_check=False)["allowed"])

    def test_stale_check_rejected(self):
        st = tmp_store()
        req = self._req()
        digest = prepare_confirm(st, req)["digest"]
        self.assertFalse(preflight(st, digest, req,
                                   now=time.time() + 3600)["allowed"])


if __name__ == "__main__":
    unittest.main()
