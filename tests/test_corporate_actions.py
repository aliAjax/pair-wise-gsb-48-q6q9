import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied
from src.rules import DomainRules


def dividend_data(reference_announcement="CA-2026-001", ratio=0.5):
    return {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0,
            'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'dividend',
            'action_ratio': ratio, 'announcement_ref': reference_announcement}


def split_data(reference_announcement="CA-2026-002", ratio=2.0):
    data = dividend_data(reference_announcement, ratio)
    data['corporate_action'] = 'split'
    return data


OFFICER = Actor("officer-1", "settlement_officer")
SPECIALIST = Actor("ca-1", "corporate_actions")
SPECIALIST_2 = Actor("ca-2", "corporate_actions")
TRADER = Actor("trader-1", "trader")


def settle_dividend(service, record, delivered=1000, cash=12518.0):
    record = service.act(SPECIALIST, record["id"], record["version"], "apply_corporate", {})
    record = service.act(OFFICER, record["id"], record["version"], "approve", {})
    return service.act(OFFICER, record["id"], record["version"], "settle",
                       {"delivered_quantity": delivered, "cash_paid": cash})


class CorporateActionRatioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, reference, data=None):
        return self.service.create(TRADER, reference, data or dividend_data())

    def adjust(self, actor, announcement, version, ratio, request_id):
        return self.service.adjust_announcement_ratio(
            actor, announcement, version, {"ratio": ratio, "request_id": request_id})

    def test_ratio_stored_with_instruction_and_announcement_registered(self):
        record = self.create("TRD-3001")
        self.assertEqual(record["payload"]["action_ratio"], 0.5)
        self.assertEqual(record["payload"]["cash_entitlement"], 500.0)
        announcement = self.service.get_announcement(SPECIALIST, "CA-2026-001")
        self.assertEqual(announcement["version"], 0)
        self.assertEqual(announcement["status"], "pending")
        self.assertEqual(announcement["ratio"], 0.5)

    def test_adjust_recalculates_unsettled_and_freezes_settled(self):
        unsettled = self.create("TRD-3101")
        second_data = dividend_data()
        second_data["settlement_day"] = 3
        settled = settle_dividend(self.service, self.service.create(TRADER, "TRD-3102", second_data))
        self.assertEqual(settled["payload"]["settled_action_ratio"], 0.5)
        self.assertEqual(settled["payload"]["settled_cash_entitlement"], 500.0)

        result = self.adjust(SPECIALIST, "CA-2026-001", 0, 0.8, "REQ-1")
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["affected"], 1)
        self.assertEqual(result["skipped"], 1)

        unsettled = self.service.get_record(TRADER, unsettled["id"])
        self.assertEqual(unsettled["payload"]["action_ratio"], 0.8)
        self.assertEqual(unsettled["payload"]["cash_entitlement"], 800.0)
        self.assertEqual(unsettled["version"], 2)

        settled = self.service.get_record(TRADER, settled["id"])
        self.assertEqual(settled["payload"]["action_ratio"], 0.5)
        self.assertEqual(settled["payload"]["settled_action_ratio"], 0.5)

        announcement = self.service.get_announcement(SPECIALIST, "CA-2026-001")
        self.assertEqual(announcement["version"], 1)
        self.assertEqual(announcement["ratio"], 0.8)
        self.assertEqual(announcement["status"], "confirmed")

        timeline = self.service.timeline(TRADER, unsettled["id"])
        self.assertEqual(timeline[-1]["action"], "ratio_adjusted")
        self.assertEqual(timeline[-1]["details"]["new_ratio"], 0.8)
        timeline_settled = self.service.timeline(TRADER, settled["id"])
        self.assertTrue(all(e["action"] != "ratio_adjusted" for e in timeline_settled))

    def test_adjusted_ratio_takes_effect_when_applying_corporate_action(self):
        record = self.service.create(TRADER, "TRD-3201", split_data())
        self.assertEqual(record["payload"]["adjusted_quantity"], 2000)
        self.adjust(SPECIALIST, "CA-2026-002", 0, 3.0, "REQ-S1")
        record = self.service.get_record(TRADER, record["id"])
        record = self.service.act(SPECIALIST, record["id"], record["version"], "apply_corporate", {})
        self.assertEqual(record["payload"]["effective_quantity"], 3000)
        self.assertAlmostEqual(record["payload"]["effective_price"], 12.5 / 3.0, places=4)

    def test_reverse_dividend_recoups_cash_at_original_ratio(self):
        record = settle_dividend(self.service, self.create("TRD-3301"))
        # 到账前比例被改成0.8，但已交收指令冻结原比例0.5
        self.adjust(SPECIALIST, "CA-2026-001", 0, 0.8, "REQ-2")
        reversed_record = self.service.act(
            OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "客户撤诉"})
        self.assertEqual(reversed_record["state"], "reversed")
        self.assertEqual(reversed_record["payload"]["recouped_cash"], 500.0)
        self.assertEqual(reversed_record["payload"]["reversed_action_ratio"], 0.5)
        self.assertEqual(reversed_record["payload"]["settled_action_ratio"], 0.5)

    def test_reverse_after_adjustment_recoups_at_settlement_ratio(self):
        record = self.create("TRD-3302")
        self.adjust(SPECIALIST, "CA-2026-001", 0, 0.8, "REQ-3")
        record = self.service.get_record(TRADER, record["id"])
        record = settle_dividend(self.service, record)
        self.assertEqual(record["payload"]["settled_action_ratio"], 0.8)
        reversed_record = self.service.act(
            OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "差错"})
        self.assertEqual(reversed_record["payload"]["recouped_cash"], 800.0)

    def test_reverse_split_has_no_cash_diff(self):
        record = self.service.create(TRADER, "TRD-3303", split_data())
        record = self.service.act(SPECIALIST, record["id"], record["version"], "apply_corporate", {})
        record = self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        record = self.service.act(OFFICER, record["id"], record["version"], "settle",
                                  {"delivered_quantity": 2000, "cash_paid": 12518.0})
        reversed_record = self.service.act(
            OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "拆并撤销"})
        self.assertEqual(reversed_record["payload"]["recouped_cash"], 0.0)

    def test_reversed_instructions_keep_ratio_on_later_adjustment(self):
        record = settle_dividend(self.service, self.create("TRD-3401"))
        self.service.act(OFFICER, record["id"], record["version"], "reverse", {"reverse_reason": "差错"})
        result = self.adjust(SPECIALIST, "CA-2026-001", 0, 0.9, "REQ-4")
        self.assertEqual(result["affected"], 0)
        self.assertEqual(result["skipped"], 1)
        record = self.service.get_record(TRADER, record["id"])
        self.assertEqual(record["payload"]["action_ratio"], 0.5)

    def test_concurrent_different_ratios_only_one_wins(self):
        self.create("TRD-3501")
        outcomes = []

        def confirm(actor, ratio, request_id):
            try:
                outcomes.append(("ok", ratio, self.adjust(actor, "CA-2026-001", 0, ratio, request_id)))
            except Conflict as exc:
                outcomes.append(("conflict", ratio, str(exc)))

        t1 = threading.Thread(target=confirm, args=(SPECIALIST, 0.6, "REQ-C1"))
        t2 = threading.Thread(target=confirm, args=(SPECIALIST_2, 0.7, "REQ-C2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = sorted(outcome[0] for outcome in outcomes)
        self.assertEqual(statuses, ["conflict", "ok"])
        announcement = self.service.get_announcement(SPECIALIST, "CA-2026-001")
        self.assertEqual(announcement["version"], 1)
        winning_ratio = next(outcome[1] for outcome in outcomes if outcome[0] == "ok")
        self.assertAlmostEqual(announcement["ratio"], winning_ratio, places=6)

        # 后到者刷新版本后可以确认更新的比例
        retry = self.adjust(SPECIALIST_2, "CA-2026-001", 1, 0.7, "REQ-C3")
        self.assertEqual(retry["announcement"]["version"], 2)
        self.assertEqual(retry["announcement"]["ratio"], 0.7)

    def test_stale_version_conflict(self):
        self.create("TRD-3601")
        self.adjust(SPECIALIST, "CA-2026-001", 0, 0.6, "REQ-V1")
        with self.assertRaises(Conflict):
            self.adjust(SPECIALIST_2, "CA-2026-001", 0, 0.7, "REQ-V2")

    def test_same_announcement_retry_is_idempotent(self):
        record = self.create("TRD-3701")
        first = self.adjust(SPECIALIST, "CA-2026-001", 0, 0.66, "REQ-IDEM")
        self.assertFalse(first["idempotent"])
        # 同公告同request_id重试：即使携带旧版本号也不重复调整
        retry = self.adjust(SPECIALIST, "CA-2026-001", 0, 0.66, "REQ-IDEM")
        self.assertTrue(retry["idempotent"])
        self.assertEqual(retry["new_ratio"], 0.66)
        record = self.service.get_record(TRADER, record["id"])
        self.assertEqual(record["version"], 2)  # 仅首次调整产生一次版本递增
        self.assertEqual(record["payload"]["cash_entitlement"], 660.0)
        timeline = self.service.timeline(TRADER, record["id"])
        self.assertEqual(len([e for e in timeline if e["action"] == "ratio_adjusted"]), 1)

    def test_unknown_announcement_is_not_found(self):
        with self.assertRaises(NotFound):
            self.adjust(SPECIALIST, "CA-NOPE", 0, 0.5, "REQ-X")
        with self.assertRaises(NotFound):
            self.service.get_announcement(SPECIALIST, "CA-NOPE")

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.adjust(TRADER, "CA-2026-001", 0, 0.9, "REQ-P1")
        record = settle_dividend(self.service, self.create("TRD-3801"))
        # 冲正只能由结算主管执行，公司行动专员无权
        with self.assertRaises(PermissionDenied):
            self.service.act(SPECIALIST, record["id"], record["version"], "reverse",
                             {"reverse_reason": "无权"})

    def test_legacy_database_and_instructions(self):
        # 模拟升级前的旧库：只有records/audit_events表，旧payload无公告编号与冻结字段
        legacy_db = str(Path(self.temp.name) / "legacy.db")
        legacy_payload = {
            'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0,
            'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'dividend',
            'action_ratio': 0.25, 'gross_amount': 12500.0, 'net_amount': 12518.0,
            'adjusted_quantity': 1000, 'adjusted_price': 12.5, 'corporate_applied': True,
            'effective_quantity': 1000, 'effective_price': 12.5, 'approved_amount': 12518.0,
            'delivered_quantity': 1000, 'cash_paid': 12518.0,
        }
        connection = sqlite3.connect(legacy_db)
        connection.executescript(
            """
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,
                created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER NOT NULL,
                action TEXT NOT NULL, actor_id TEXT NOT NULL, version INTEGER NOT NULL,
                details TEXT NOT NULL, created_at TEXT NOT NULL);
            """
        )
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
            " VALUES('LEGACY-1','settled',5,?,?,?,?,?)",
            (json.dumps(legacy_payload), "LEGACY", "LEGACY",
             "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        # 仓储初始化在旧库上自动补建新表，旧指令按原参数读出
        service = build_service(legacy_db)
        record = service.get_record(OFFICER, 1)
        self.assertEqual(record["payload"]["action_ratio"], 0.25)
        self.assertNotIn("settled_action_ratio", record["payload"])
        self.assertEqual(service.list_announcements(OFFICER), [])

        # 旧指令冲正按入库原比例0.25追回现金差额
        reversed_record = service.act(
            OFFICER, 1, record["version"], "reverse", {"reverse_reason": "历史差错"})
        self.assertEqual(reversed_record["state"], "reversed")
        self.assertEqual(reversed_record["payload"]["recouped_cash"], 250.0)
        self.assertEqual(reversed_record["payload"]["reversed_action_ratio"], 0.25)


class RulesUnitTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_build_adjustment_split(self):
        prepared = self.rules.prepare_create(split_data())
        prepared["corporate_applied"] = True
        prepared["effective_quantity"] = prepared["adjusted_quantity"]
        record = {"state": "adjusted", "payload": prepared}
        payload, changes = self.rules.build_adjustment(record, 3.0)
        self.assertEqual(changes["adjusted_quantity"], 3000)
        self.assertEqual(payload["effective_quantity"], 3000)

    def test_build_adjustment_dividend(self):
        record = {"state": "captured", "payload": self.rules.prepare_create(dividend_data())}
        payload, changes = self.rules.build_adjustment(record, 0.3)
        self.assertEqual(changes["cash_entitlement"], 300.0)
        self.assertNotIn("effective_quantity", changes)

    def test_frozen_states_not_eligible(self):
        for state in ("settled", "reversed"):
            self.assertFalse(self.rules.is_unsettled({"state": state}))
        for state in ("captured", "adjusted", "approved", "failed"):
            self.assertTrue(self.rules.is_unsettled({"state": state}))


if __name__ == "__main__":
    unittest.main()
