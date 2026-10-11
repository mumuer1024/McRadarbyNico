"""统一订单闭环测试：摘要一致性、下单闸门、防重、不重试、支付期限、状态解读。"""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone

from _harness import (  # noqa: F401
    MockMCP, ROOT, host_submit, host_submit_retry_happy, pricing_for,
    prepare_confirm, tmp_store,
)

from mcdradar.order_flow import (
    BE_CODE_REQUIRED, BE_TYPE_ORDER_TYPE, PRICING_CHECK_TTL, TRUST_BOUNDARY,
    build_order_request, interpret_order_status, parse_order_result,
    payment_deadline, prepare_order, confirm_order, confirmation_status,
    record_pricing_check, get_pricing_check, preflight, validate_order_request,
    verify_pricing_covers_items,
    authorize_retry, finish_confirmed, finish_failed, finish_unknown,
    begin_submit, reconcile,
)
from mcdradar.order_guard import (
    CONFIRMED, FAILED, PREPARED, UNKNOWN, USER_CONFIRMED,
    TRUST_BOUNDARY as GUARD_TRUST, get_record, may_submit, revoke_confirmation,
)
from mcdradar.summary import request_digest, verify_confirmation
from mcdradar.summary import digest_input as summary_digest_input
from mcdradar.tool_risk import assert_may_call

BASE_ORDER = {
    "store_code": "1000001", "order_type": 1, "be_type": 1,
    "items": [{"productCode": "1450", "quantity": 1},
              {"productCode": "3050", "quantity": 1}],
    "take_way_code": "locker-out",
}


class TestRequestBuild(unittest.TestCase):
    def test_to_store_request(self):
        r = build_order_request(**BASE_ORDER)
        self.assertEqual(r["storeCode"], "1000001")
        self.assertEqual(r["takeWayCode"], "locker-out")
        self.assertEqual(len(r["items"]), 2)
        self.assertNotIn("beCode", r)          # beType=1 不传 beCode

    def test_be_code_required_for_delivery(self):
        r = build_order_request(store_code="1", order_type=2, be_type=2, be_code="B1",
                                items=[{"productCode": "x"}], address_id="A1")
        self.assertEqual(r["beCode"], "B1")
        self.assertEqual(r["addressId"], "A1")

    def test_validate_missing_takeway(self):
        r = build_order_request(store_code="1", order_type=1, be_type=1,
                                items=[{"productCode": "x"}])
        errs = validate_order_request(r)
        self.assertTrue(any("takeWayCode" in e for e in errs))

    def test_validate_delivery_needs_address(self):
        r = build_order_request(store_code="1", order_type=2, be_type=2, be_code="B",
                                items=[{"productCode": "x"}])
        errs = validate_order_request(r)
        self.assertTrue(any("addressId" in e for e in errs))

    def test_validate_be_type1_must_not_have_becode(self):
        """beType=1（到店自取）不传 beCode；传了要报错。"""
        r = build_order_request(**BASE_ORDER)
        r["beCode"] = "SHOULD_NOT"
        errs = validate_order_request(r)
        self.assertTrue(any("不应传 beCode" in e for e in errs))

    def test_validate_be_code_matrix(self):
        """参数化：beCode 规则必须按 **beType** 判定，不能按 orderType 一刀切。

        🔴 修复前：代码写的是 `orderType=1 → 不许有 beCode`，
        导致 beType=5 得来速（orderType=1 但**必传** beCode）被误判为非法。
        """
        cases = [
            # (beType, orderType, beCode, 是否应通过)
            (1, 1, "", True),        # 到店自取：不传 ✅
            (1, 1, "X", False),      # 到店自取：传了 ❌
            (5, 1, "DT01", True),    # 得来速：必传 ✅
            (5, 1, "", False),       # 得来速：不传 ❌
            (2, 2, "DL01", True),    # 麦乐送：必传 ✅
            (2, 2, "", False),       # 麦乐送：不传 ❌
            (6, 2, "GM01", True),    # 团餐：必传 ✅
            (6, 2, "", False),       # 团餐：不传 ❌
        ]
        for be_type, order_type, be_code, should_pass in cases:
            with self.subTest(beType=be_type, orderType=order_type, beCode=be_code):
                kw = dict(store_code="1", order_type=order_type, be_type=be_type,
                          be_code=be_code, items=[{"productCode": "x", "quantity": 1}])
                if order_type == 1:
                    kw["take_way_code"] = "locker-out"
                else:
                    kw["address_id"] = "A1"
                req = build_order_request(**kw)
                # 校验器要对"请求里是否带 beCode"负责，不依赖构造器的取舍
                if be_code:
                    req["beCode"] = be_code
                else:
                    req.pop("beCode", None)
                errs = validate_order_request(req)
                if should_pass:
                    self.assertEqual(errs, [], f"beType={be_type} 应通过，实际错误：{errs}")
                else:
                    self.assertTrue(errs, f"beType={be_type} 应被拦截，却通过了")

    def test_builder_drops_becode_for_be_type1(self):
        """构造器对 beType=1 主动丢弃 beCode（与官方"beType=1 不传"一致）。"""
        req = build_order_request(store_code="1", order_type=1, be_type=1, be_code="X",
                                  items=[{"productCode": "x"}], take_way_code="t")
        self.assertNotIn("beCode", req)

    def test_be_code_rule_constants_match_audit(self):
        """beCode 规则常量必须与 docs/03 的审计表一致。"""
        self.assertEqual(BE_TYPE_ORDER_TYPE, {1: 1, 5: 1, 2: 2, 6: 2})
        self.assertEqual(BE_CODE_REQUIRED, {1: False, 5: True, 2: True, 6: True})

    def test_drive_through_is_order_type_1_with_becode(self):
        """得来速 = orderType 1 + beType 5 + beCode 必传，必须能构造出合法请求。"""
        req = build_order_request(store_code="1", order_type=1, be_type=5,
                                  be_code="DT01", take_way_code="drive-through",
                                  items=[{"productCode": "1450", "quantity": 1}])
        self.assertEqual(req["orderType"], 1)
        self.assertEqual(req["beType"], 5)
        self.assertEqual(req["beCode"], "DT01")
        self.assertEqual(validate_order_request(req), [])

    def test_be_type_order_type_mismatch(self):
        """beType 与 orderType 不匹配 → 报错。"""
        req = build_order_request(store_code="1", order_type=2, be_type=5,
                                  be_code="DT01", address_id="A1",
                                  items=[{"productCode": "x", "quantity": 1}])
        errs = validate_order_request(req)
        self.assertTrue(any("应对应 orderType=1" in e for e in errs))

    def test_validate_bad_quantity(self):
        r = build_order_request(store_code="1", order_type=1, be_type=1,
                                items=[{"productCode": "x", "quantity": 0}],
                                take_way_code="t")
        errs = validate_order_request(r)
        self.assertTrue(any("quantity" in e for e in errs))


class TestDigestConsistency(unittest.TestCase):
    def test_same_request_same_digest(self):
        a = build_order_request(**BASE_ORDER)
        b = build_order_request(**BASE_ORDER)
        self.assertEqual(request_digest(a), request_digest(b))

    def test_store_change_invalidates_confirmation(self):
        """门店变了 → 旧确认失效，必须重新确认。"""
        a = build_order_request(**BASE_ORDER)
        changed = dict(BASE_ORDER, store_code="9999999")
        b = build_order_request(**changed)
        v = verify_confirmation(request_digest(a), b)
        self.assertFalse(v["ok"])
        self.assertIn("重新", v["message"])

    def test_item_change_invalidates_confirmation(self):
        a = build_order_request(**BASE_ORDER)
        changed = dict(BASE_ORDER, items=[{"productCode": "1450", "quantity": 1}])
        b = build_order_request(**changed)
        self.assertFalse(verify_confirmation(request_digest(a), b)["ok"])

    def test_coupon_change_invalidates_confirmation(self):
        a = build_order_request(**BASE_ORDER)
        changed = dict(BASE_ORDER)
        changed["items"] = [{"productCode": "1450", "quantity": 1, "couponId": "C1"}]
        b = build_order_request(**changed)
        self.assertFalse(verify_confirmation(request_digest(a), b)["ok"])

    def test_irrelevant_field_does_not_change_digest(self):
        """不参与订单内容的字段不应影响指纹（避免误判）。"""
        a = build_order_request(**BASE_ORDER)
        b = dict(a)
        b["_est_payable_fen"] = 9999
        self.assertEqual(request_digest(a), request_digest(b))

    # ---- P0-1 回归：指纹必须覆盖全部影响订单内容的字段 --------------------
    def test_remark_participates_in_digest(self):
        """外送备注会随订单提交给门店 → 必须参与指纹。"""
        a = build_order_request(store_code="1", order_type=2, be_type=2, be_code="B",
                                address_id="A1", items=[{"productCode": "x"}],
                                remark="不要辣")
        b = build_order_request(store_code="1", order_type=2, be_type=2, be_code="B",
                                address_id="A1", items=[{"productCode": "x"}],
                                remark="多加辣")
        self.assertNotEqual(request_digest(a), request_digest(b))

    def test_with_order_participates_in_digest(self):
        """随单购权益直接影响商品与成交价 → 必须参与指纹。"""
        kw = dict(store_code="1", order_type=1, be_type=1,
                  items=[{"productCode": "x"}], take_way_code="t")
        a = build_order_request(**kw)
        b = build_order_request(**kw, with_order={"cardType": "1"})
        self.assertNotEqual(request_digest(a), request_digest(b))

    def test_gm_service_code_participates_in_digest(self):
        """团餐服务编码决定履约方式 → 必须参与指纹。"""
        kw = dict(store_code="1", order_type=2, be_type=6, be_code="G",
                  address_id="A1", items=[{"productCode": "x"}])
        a = build_order_request(**kw)
        b = build_order_request(**kw, gm_service_code="GM9")
        self.assertNotEqual(request_digest(a), request_digest(b))

    def test_internal_tracking_fields_excluded(self):
        """内部跟踪字段不参与指纹（避免无谓失效）。"""
        a = build_order_request(**BASE_ORDER)
        b = dict(a)
        b.update({"_est_payable_fen": 1, "clientRequestId": "abc",
                  "traceId": "t1", "timestamp": 123})
        self.assertEqual(request_digest(a), request_digest(b))

    def test_digest_covers_every_non_internal_key(self):
        """白名单回归防线：请求里任何非内部字段都必须出现在指纹输入中。

        这条测试的意义是**防止将来有人重新引入白名单**。
        """
        req = build_order_request(store_code="1", order_type=2, be_type=6, be_code="G",
                                  address_id="A1", items=[{"productCode": "x"}],
                                  remark="R", need_tableware=True,
                                  reservation_date="2026-10-11", gm_service_code="GM9")
        picked = summary_digest_input(req)
        for k in ("storeCode", "orderType", "beType", "beCode", "addressId",
                  "remark", "needTableware", "reservationDate", "gmServiceCode", "items"):
            self.assertIn(k, picked, f"{k} 必须参与指纹")
        self.assertNotIn("_est_payable_fen", picked)

    def test_confirmed_then_modified_field_invalidates(self):
        """P0-1 + P0-2：确认后修改任一关键字段 → 原确认失效。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        self.assertTrue(confirmation_status(st, digest)["confirmed"])

        for mutate in (
            lambda r: r.update(remark="改备注"),
            lambda r: r.update(withOrder={"cardType": "1"}),
            lambda r: r.update(gmServiceCode="GM9"),
            lambda r: r.update(needTableware=True),
            lambda r: r.update(reservationDate="2026-10-12"),
            lambda r: r.update(storeCode="9999999"),
        ):
            with self.subTest(mutate=mutate):
                changed = dict(req)
                mutate(changed)
                v = verify_confirmation(digest, changed)
                self.assertFalse(v["ok"], "关键字段被修改后原确认必须失效")
                pf = preflight(st, digest, changed)
                self.assertFalse(pf["allowed"])


class TestConfirmGate(unittest.TestCase):
    def test_no_record_never_allowed(self):
        """没有走完摘要确认（无记录）→ 绝不调用 create-order。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = request_digest(req)
        pf = preflight(st, digest, req)
        self.assertFalse(pf["allowed"])
        mock = MockMCP()
        out = host_submit(st, digest, req, mock)
        self.assertFalse(out["called"])
        self.assertEqual(mock.call_count, 0)

    # ---- P0-2 回归：确认必须是可验证的状态 --------------------------------
    def test_prepared_but_not_confirmed_is_blocked(self):
        """只 prepare 未确认 → awaiting_confirm，**不得提交**。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        r = prepare_order(st, req)
        digest = r["digest"]
        self.assertEqual(r["status"], PREPARED)
        self.assertEqual(get_record(st, digest)["status"], PREPARED)
        gate = may_submit(st, digest)
        self.assertFalse(gate["allowed"])
        self.assertIn("尚未收到用户最终确认", gate["reason"])
        mock = MockMCP()
        out = host_submit(st, digest, req, mock)
        self.assertFalse(out["called"])
        self.assertEqual(mock.call_count, 0)

    def test_confirm_without_user_flag_blocked(self):
        """user_confirmed=False 的确认不得生效。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_order(st, req)["digest"]
        c = confirm_order(st, digest, req, user_confirmed=False)
        self.assertFalse(c["ok"])
        self.assertFalse(confirmation_status(st, digest)["confirmed"])
        self.assertFalse(may_submit(st, digest)["allowed"])

    def test_confirm_mismatched_digest_blocked(self):
        """确认内容必须与订单指纹一致。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_order(st, req)["digest"]
        other = build_order_request(**dict(BASE_ORDER, store_code="9999999"))
        c = confirm_order(st, digest, other, user_confirmed=True)
        self.assertFalse(c["ok"])
        self.assertIn("不一致", c["reason"])

    def test_confirm_then_submit_once(self):
        """确认后 → 允许提交，且只提交一次。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        self.assertEqual(get_record(st, digest)["status"], USER_CONFIRMED)
        self.assertTrue(confirmation_status(st, digest)["confirmed"])
        mock = MockMCP()
        out = host_submit(st, digest, req, mock, behavior="success")
        self.assertTrue(out["called"])
        self.assertEqual(mock.counts("create-order"), 1)

    def test_double_confirm_does_not_allow_double_submit(self):
        """重复确认不能绕过防重（提交后状态即 SUBMITTED/CONFIRMED）。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        confirm_order(st, digest, req, user_confirmed=True)   # 再确认一次
        mock = MockMCP()
        host_submit(st, digest, req, mock, behavior="success")
        again = host_submit(st, digest, req, mock, behavior="success")
        self.assertFalse(again["called"])
        self.assertEqual(mock.counts("create-order"), 1)

    def test_revoke_confirmation_blocks_submit(self):
        """撤销确认 → 回到 awaiting_confirm → 不得提交。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        revoke_confirmation(st, digest)
        self.assertFalse(may_submit(st, digest)["allowed"])
        self.assertFalse(confirmation_status(st, digest)["confirmed"])

    def test_expired_confirmation_blocks_submit(self):
        """确认超过有效期 → 需重新展示摘要并重新确认。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        far = time.time() + 3600     # 1 小时后
        self.assertFalse(may_submit(st, digest, now=far)["allowed"])
        self.assertTrue(confirmation_status(st, digest, now=far)["expired"])

    def test_trust_boundary_declared(self):
        """信任边界必须显式声明：Python 不能证明用户真实意图。"""
        self.assertIn("无法独立证明", TRUST_BOUNDARY)
        self.assertIn("无法独立证明", GUARD_TRUST)

    def test_real_call_blocked_without_authorization(self):
        """本轮未授权：任何 real=True 的 create-order 都必须被硬拦截。"""
        with self.assertRaises(PermissionError):
            assert_may_call("create-order", real=True)

    def test_happy_path_submits_exactly_once(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req, est_payable_fen=2650)["digest"]
        mock = MockMCP()
        out = host_submit(st, digest, req, mock, behavior="success")
        self.assertTrue(out["called"])
        self.assertEqual(out["outcome"], "confirmed")
        self.assertEqual(mock.counts("create-order"), 1)
        self.assertEqual(get_record(st, digest)["status"], CONFIRMED)
        self.assertEqual(get_record(st, digest)["order_id"], "MOCK_ORDER_0001")

    def test_tampered_request_blocked(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        tampered = dict(req)
        tampered["items"] = [{"productCode": "9999", "quantity": 1}]
        mock = MockMCP()
        out = host_submit(st, digest, tampered, mock)
        self.assertFalse(out["called"])
        self.assertEqual(mock.call_count, 0)

    def test_bypass_confirm_preflight_blocked(self):
        """绕过 confirm_order 直接 preflight → 必须被拒。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_order(st, req)["digest"]
        record_pricing_check(st, digest, request=req, pricing=pricing_for(req))
        pf = preflight(st, digest, req)
        self.assertFalse(pf["allowed"])
        self.assertIn("尚未收到用户最终确认", pf["guard"]["reason"])


class TestPricingGate(unittest.TestCase):
    """P0-5：算价覆盖校验必须成为 preflight 的强制条件。"""

    def test_no_pricing_check_blocks_submit(self):
        """没有算价覆盖校验记录 → 禁止下单（即使已确认）。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_order(st, req)["digest"]
        confirm_order(st, digest, req, user_confirmed=True)
        pf = preflight(st, digest, req)
        self.assertFalse(pf["allowed"])
        self.assertIn("缺少算价覆盖校验", pf["pricing_check"]["reason"])

    def test_failed_pricing_check_blocks_submit(self):
        """服务端静默丢了商品 → 禁止下单。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_order(st, req)["digest"]
        confirm_order(st, digest, req, user_confirmed=True)
        # 模拟实测现象：productList 只回麦香鸡，可乐被丢掉
        bad = pricing_for(req, drop=("3050",))
        record_pricing_check(st, digest, request=req, pricing=bad)
        pf = preflight(st, digest, req)
        self.assertFalse(pf["allowed"])
        self.assertIn("覆盖校验未通过", pf["pricing_check"]["reason"])
        mock = MockMCP()
        out = host_submit(st, digest, req, mock)
        self.assertFalse(out["called"])
        self.assertEqual(mock.call_count, 0)

    def test_stale_pricing_check_blocks_submit(self):
        """核价结果过期 → 必须重新 calculate-price。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        far = time.time() + PRICING_CHECK_TTL + 60
        pf = preflight(st, digest, req, now=far)
        self.assertFalse(pf["allowed"])
        self.assertIn("已过", pf["pricing_check"]["reason"])

    def test_pricing_check_ok_allows(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = prepare_confirm(st, req)["digest"]
        pf = preflight(st, digest, req)
        self.assertTrue(pf["allowed"])

    def test_request_changed_requires_new_pricing_check(self):
        """请求变了 → 新指纹 → 旧算价校验不能复用。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        prepare_confirm(st, req)
        changed = build_order_request(**dict(BASE_ORDER, store_code="9999999"))
        d2 = prepare_order(st, changed)["digest"]
        confirm_order(st, d2, changed, user_confirmed=True)
        pf = preflight(st, d2, changed)
        self.assertFalse(pf["allowed"])
        self.assertIn("缺少算价覆盖校验", pf["pricing_check"]["reason"])

    # ---- 兼容性：不要误判 ------------------------------------------------
    def test_combo_expansion_not_misjudged(self):
        """套餐被展开成子项返回时，不得误判为「商品被丢弃」。"""
        items = [{"productCode": "9900005456", "quantity": 1}]
        pricing = {"data": {"price": 2650, "productList": [
            {"productCode": "9900005456", "quantity": 1, "comboItemList": [
                {"productCode": "1450", "quantity": 1},
                {"productCode": "3050", "quantity": 1},
            ]}]}}
        r = verify_pricing_covers_items(items, pricing)
        self.assertTrue(r["ok"])

    def test_nested_child_satisfies_request(self):
        """请求的子项编码出现在套餐子项里 → 判为已覆盖。"""
        items = [{"productCode": "1450", "quantity": 1}]
        pricing = {"data": {"productList": [
            {"productCode": "COMBO1", "quantity": 1, "comboItemList": [
                {"productCode": "1450", "quantity": 1}]}]}}
        self.assertTrue(verify_pricing_covers_items(items, pricing)["ok"])

    def test_missing_product_list_is_fail_closed(self):
        """响应里没有 productList → 无法核对 → 安全失败，禁止下单。"""
        items = [{"productCode": "1450", "quantity": 1}]
        r = verify_pricing_covers_items(items, {"data": {"price": 1700}})
        self.assertFalse(r["ok"])
        self.assertFalse(r["reliable"])
        self.assertIn("无法可靠核对", r["message"])

    def test_missing_item_still_blocked(self):
        """真的缺商品 → 必须拦下（这是本校验存在的理由）。"""
        items = [{"productCode": "1450", "quantity": 1},
                 {"productCode": "3050", "quantity": 1}]
        r = verify_pricing_covers_items(items, pricing_for(
            {"items": items}, drop=("3050",)))
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], {"3050": 1})


class TestNoAutoRetry(unittest.TestCase):
    def _digest(self, st, req):
        return prepare_confirm(st, req)["digest"]

    def test_timeout_never_retried(self):
        """超时 → 结果未知 → 即使调用方想重试 3 次，create-order 只被调用 1 次。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = self._digest(st, req)
        mock = MockMCP()
        r = host_submit_retry_happy(st, digest, req, mock, behavior="timeout", attempts=3)
        self.assertEqual(r["actual_calls"], 1)
        self.assertEqual(get_record(st, digest)["status"], UNKNOWN)

    def test_5xx_never_retried(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = self._digest(st, req)
        mock = MockMCP()
        r = host_submit_retry_happy(st, digest, req, mock, behavior="5xx", attempts=3)
        self.assertEqual(r["actual_calls"], 1)
        self.assertEqual(get_record(st, digest)["status"], UNKNOWN)

    def test_unclear_response_never_retried(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = self._digest(st, req)
        mock = MockMCP()
        r = host_submit_retry_happy(st, digest, req, mock, behavior="unclear", attempts=3)
        self.assertEqual(r["actual_calls"], 1)
        self.assertEqual(get_record(st, digest)["status"], UNKNOWN)

    def test_unknown_reports_uncertainty(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = self._digest(st, req)
        out = finish_unknown(st, digest, "超时")
        self.assertIn("禁止自动重试", out["message"])
        plan = reconcile(st, digest)
        self.assertIn("order-list", plan["steps"][0])
        self.assertIn("不得再次提交", plan["steps"][4])
        self.assertIn("不提供跨设备", plan["disclaimer"])

    def test_unknown_status_cannot_be_reconfirmed(self):
        """结果未知期间不得重新确认/重新提交（必须先核对）。"""
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = self._digest(st, req)
        finish_unknown(st, digest, "超时")
        c = confirm_order(st, digest, req, user_confirmed=True)
        self.assertFalse(c["ok"])
        self.assertIn("禁止在此期间重新确认", c["reason"])

    def test_confirmed_order_never_resubmitted(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = self._digest(st, req)
        mock = MockMCP()
        host_submit(st, digest, req, mock, behavior="success")
        again = host_submit(st, digest, req, mock, behavior="success")
        self.assertFalse(again["called"])
        self.assertEqual(mock.counts("create-order"), 1)

    def test_failed_requires_new_authorization(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        digest = self._digest(st, req)
        finish_failed(st, digest, "库存不足")
        self.assertFalse(may_submit(st, digest)["allowed"])
        authorize_retry(st, digest, "用户确认后重试")
        self.assertTrue(may_submit(st, digest)["allowed"])
        # 授权只生效一次
        begin_submit(st, digest, req)
        finish_failed(st, digest, "再次失败")
        self.assertFalse(may_submit(st, digest)["allowed"])


class TestPaymentDeadline(unittest.TestCase):
    # MCP 时间字段是北京时间（GMT+08:00）；now 必须用同一时区构造，
    # 否则在非 +08:00 的宿主机上会把 15 分钟算成 900+ 分钟（2026-10-11 实测缺陷）。
    TZ = timezone(timedelta(hours=8))

    def _now(self):
        return datetime.strptime("2026-10-10 12:00:00",
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=self.TZ).timestamp()

    def test_known_deadline(self):
        d = payment_deadline({"data": {"expirePayTime": "2026-10-10 12:15:00"}}, now=self._now())
        self.assertTrue(d["known"])
        self.assertEqual(d["remain_seconds"], 900)

    def test_deadline_nested_under_orderDetail(self):
        """🔴 实测：create-order 把 expirePayTime 放在 data.orderDetail 下。"""
        raw = {"success": True, "data": {
            "orderId": "O1",
            "orderDetail": {"orderId": "O1", "orderStatus": "待支付",
                            "expirePayTime": "2026-10-10 12:15:00"}}}
        d = payment_deadline(raw, now=self._now())
        self.assertTrue(d["known"])
        self.assertEqual(d["remain_seconds"], 900)

    def test_real_order_deadline_not_hours(self):
        """真实订单：createTime 13:20:51 / expirePayTime 13:35:51 → 恰好 15 分钟。"""
        raw = {"data": {"orderDetail": {"orderStatus": "待支付",
                                        "expirePayTime": "2026-10-11 13:35:51"}}}
        now = datetime.strptime("2026-10-11 13:20:51",
                                "%Y-%m-%d %H:%M:%S").replace(tzinfo=self.TZ).timestamp()
        d = payment_deadline(raw, now=now)
        self.assertEqual(d["remain_seconds"], 900)

    def test_missing_deadline_not_invented(self):
        """没有 expirePayTime → 绝不虚构倒计时。"""
        d = payment_deadline({"data": {"orderId": "X"}}, now=self._now())
        self.assertFalse(d["known"])
        self.assertIsNone(d["remain_seconds"])
        self.assertIn("不显示支付倒计时", d["note"])

    def test_unparsable_deadline(self):
        now = self._now()
        d = payment_deadline({"data": {"expirePayTime": "明天下午"}}, now=now)
        self.assertFalse(d["known"])

    def test_historical_15_minutes_not_hardcoded(self):
        """历史订单实测 15 分钟，但代码不得写死，必须按订单实际值算。"""
        d = payment_deadline({"data": {"expirePayTime": "2026-10-10 12:30:00"}}, now=self._now())
        self.assertEqual(d["remain_seconds"], 1800)   # 30 分钟而非 15


class TestOrderStatus(unittest.TestCase):
    def test_known_status_texts(self):
        self.assertEqual(interpret_order_status({"data": {"orderStatus": "待支付"}})["state"], "unpaid")
        self.assertEqual(interpret_order_status({"data": {"orderStatus": "订单已完成"}})["state"], "paid")
        self.assertEqual(interpret_order_status({"data": {"orderStatus": "已取消"}})["state"], "cancelled")

    def test_unknown_status_not_misjudged(self):
        """未识别的状态文本 → unknown，绝不推断成功或失败。"""
        r = interpret_order_status({"data": {"orderStatus": "订单审核中", "status": "99"}})
        self.assertEqual(r["state"], "unknown")
        self.assertIn("不做成功", r["note"])

    def test_official_status_text_recognized(self):
        """官方枚举里的文本要能识别（含组合形态）。"""
        r = interpret_order_status({"data": {"orderStatus": "配餐中-餐厅确认配餐中", "status": "10"}})
        self.assertEqual(r["state"], "paid")
        # 但实测 status 数字码（如 40）与官方枚举不符，代码不得依赖它
        self.assertNotEqual(r["code"], "40")

    def test_numeric_code_ignored(self):
        """实测 status='40' 与官方枚举不符，代码不得依赖数字码。"""
        r = interpret_order_status({"data": {"status": "40"}})
        self.assertEqual(r["state"], "unknown")

    def test_missing_status(self):
        r = interpret_order_status({"data": {}})
        self.assertEqual(r["state"], "unknown")

    def test_status_nested_under_orderDetail(self):
        """🔴 实测：create-order 把 orderStatus 放在 data.orderDetail 下。"""
        raw = {"success": True, "data": {"orderId": "O1",
               "orderDetail": {"orderStatus": "待支付", "status": "10"}}}
        self.assertEqual(interpret_order_status(raw)["state"], "unpaid")

    def test_orderDetail_without_status_falls_back_to_data(self):
        """orderDetail 不含状态字段时，回退到 data 顶层（query-order 形态）。"""
        raw = {"data": {"orderStatus": "订单已完成", "orderDetail": {"storeName": "X"}}}
        self.assertEqual(interpret_order_status(raw)["state"], "paid")


class TestPayEntryParsing(unittest.TestCase):
    def test_pay_url_found(self):
        p = parse_order_result({"success": True, "data": {
            "orderId": "O1", "payUrl": "https://m.mcd.cn/pay/O1", "orderStatus": "待支付"}})
        self.assertTrue(p["pay_entry_known"])
        self.assertIn("https://m.mcd.cn/pay/O1", p["pay_urls"])

    def test_pay_entry_note_states_verified_flow(self):
        """2026-10-11 首次真实下单实测确认：浏览器打开→扫码→跳 App；不扫码也可在 App 内找到订单。"""
        p = parse_order_result({"success": True, "data": {
            "orderId": "O1", "payUrl": "https://m.mcd.cn/pay/O1", "orderStatus": "待支付"}})
        note = p["pay_entry_note"]
        self.assertIn("浏览器", note)
        self.assertIn("扫码", note)
        self.assertIn("麦当劳 App", note)
        self.assertNotIn("待真实验证", note)

    def test_missing_pay_url_note_offers_app_fallback(self):
        """无链接时必须给出已实测可用的降级路径（App / 微信小程序内找到订单）。"""
        p = parse_order_result({"success": True, "data": {"orderId": "O1", "orderStatus": "待支付"}})
        self.assertIn("麦当劳 App", p["pay_entry_note"])
        self.assertIn("小程序", p["pay_entry_note"])

    def test_render_pay_entry_shows_verified_how_to_pay(self):
        """渲染层要给出可执行的付款指引，且不虚构未验证信息。"""
        from mcdradar.render import render_pay_entry
        p = parse_order_result({"success": True, "data": {
            "orderId": "O1", "payUrl": "https://m.mcd.cn/mcp/scanToPay?orderId=O1",
            "orderStatus": "待支付", "expirePayTime": "2026-10-11 13:35:51"}})
        md = render_pay_entry(p)
        self.assertIn("浏览器", md)
        self.assertIn("扫码", md)
        self.assertIn("不扫码", md)
        self.assertIn("本 Skill 不会、也无法代付", md)
        self.assertNotIn("待真实验证", md)

    def test_pay_url_missing_degrades(self):
        """没有支付链接 → 降级提示，不虚构。"""
        p = parse_order_result({"success": True, "data": {"orderId": "O1", "orderStatus": "待支付"}})
        self.assertFalse(p["pay_entry_known"])
        self.assertIn("未发现支付链接", p["pay_entry_note"])
        self.assertIn("麦当劳 App", p["pay_entry_note"])

    def test_created_not_equal_paid(self):
        p = parse_order_result({"success": True, "data": {
            "orderId": "O1", "orderStatus": "待支付", "payUrl": "https://x"}})
        self.assertEqual(p["status"]["state"], "unpaid")
        self.assertTrue(p["success"])

    def test_unknown_fields_reported(self):
        p = parse_order_result({"success": True, "data": {"orderId": "O1"}})
        self.assertIn("orderStatus", p["unknown_fields"])
        self.assertIn("expirePayTime", p["unknown_fields"])

    def test_real_create_order_shape_fully_parsed(self):
        """🔴 真实 create-order 形态：状态/期限/支付链接都在，不应误报未知。
        （样本已脱敏：订单号与链接中的 orderId 为占位符，字段形态保持真实。）"""
        raw = {"success": True, "data": {
            "orderId": "1000000000000000000000000001",
            "payH5Url": "https://m.mcd.cn/mcp/scanToPay?orderId=1000000000000000000000000001",
            "orderDetail": {"orderId": "1000000000000000000000000001",
                            "orderStatus": "待支付", "status": "10",
                            "expirePayTime": "2026-10-11 13:35:51"}}}
        p = parse_order_result(raw)
        self.assertEqual(p["status"]["state"], "unpaid")
        self.assertTrue(p["payment_deadline"]["known"])
        self.assertTrue(p["pay_entry_known"])
        self.assertEqual(p["unknown_fields"], [])


class TestSensitiveDataNotPersisted(unittest.TestCase):
    def test_record_has_no_address_or_token(self):
        st = tmp_store()
        req = build_order_request(**BASE_ORDER)
        req["addressId"] = "A1"
        digest = prepare_order(st, req)["digest"]
        rec = get_record(st, digest)
        blob = str(rec)
        self.assertNotIn("A1", blob)          # addressId 不进状态
        self.assertIn("1450x1", str(rec["item_codes"]))


if __name__ == "__main__":
    unittest.main()
