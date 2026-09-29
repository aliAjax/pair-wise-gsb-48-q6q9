import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.rules import DomainRules


SPLIT = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split', 'action_ratio': 2.0}
DIVIDEND = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 0.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'dividend', 'action_ratio': 0.5}

SPECIALIST = lambda: Actor("ca1", "corporate_actions")
OFFICER = lambda: Actor("so1", "settlement_officer")


def insert_legacy_record(service, reference, state, payload, version=1):
    """直接写入升级前的旧格式指令：缺少original_action_ratio等新字段。"""
    repository = service.repository
    with repository._connect() as connection:
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (reference, state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), "legacy", "legacy"),
        )
        row = connection.execute("SELECT id FROM records WHERE reference=?", (reference,)).fetchone()
        return int(row["id"])


class AnnouncementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_ratio_stored_on_capture_and_announcement_recomputes_open_instructions(self):
        captured = self.service.create(Actor("t", "trader"), "TRD-1", dict(SPLIT))
        data2 = dict(SPLIT, settlement_day=3)
        data3 = dict(SPLIT, settlement_day=4)
        applied = self.service.create(Actor("t", "trader"), "TRD-2", data2)
        settled = self.service.create(Actor("t", "trader"), "TRD-3", data3)
        # 比例随指令入库，原比例留档
        self.assertEqual(captured["payload"]["action_ratio"], 2.0)
        self.assertEqual(captured["payload"]["original_action_ratio"], 2.0)

        applied = self.service.act(SPECIALIST(), applied["id"], applied["version"], "apply_corporate", {})
        settled = self.service.act(SPECIALIST(), settled["id"], settled["version"], "apply_corporate", {})
        settled = self.service.act(OFFICER(), settled["id"], settled["version"], "approve", {})
        settled = self.service.act(OFFICER(), settled["id"], settled["version"], "settle", {'delivered_quantity': 2000, 'cash_paid': 12518.0})
        settled_version = settled["version"]

        result = self.service.confirm_announcement(
            SPECIALIST(), "ANN-1", {'instrument': 'ACME', 'corporate_action': 'split', 'action_ratio': 3.0}
        )
        self.assertFalse(result["duplicate"])
        adjusted_ids = {item["record_id"] for item in result["adjusted"]}
        self.assertEqual(adjusted_ids, {captured["id"], applied["id"]})

        captured = self.service.get_record(Actor("t", "trader"), captured["id"])
        applied = self.service.get_record(Actor("t", "trader"), applied["id"])
        settled = self.service.get_record(Actor("t", "trader"), settled["id"])
        # 未交收指令按新比例重算；已应用公司行动的生效值同步刷新
        self.assertEqual(captured["payload"]["action_ratio"], 3.0)
        self.assertEqual(captured["payload"]["adjusted_quantity"], 3000)
        self.assertEqual(applied["payload"]["adjusted_quantity"], 3000)
        self.assertEqual(applied["payload"]["effective_quantity"], 3000)
        # 原比例保留，供冲正追溯
        self.assertEqual(captured["payload"]["original_action_ratio"], 2.0)
        # 已交收指令原比例、数量、版本全部不动
        self.assertEqual(settled["payload"]["action_ratio"], 2.0)
        self.assertEqual(settled["payload"]["adjusted_quantity"], 2000)
        self.assertEqual(settled["version"], settled_version)

    def test_reverse_recovers_cash_by_frozen_ratio(self):
        record = self.service.create(Actor("t", "trader"), "DIV-1", dict(DIVIDEND))
        record = self.service.act(SPECIALIST(), record["id"], record["version"], "apply_corporate", {})
        record = self.service.act(OFFICER(), record["id"], record["version"], "approve", {})
        record = self.service.act(OFFICER(), record["id"], record["version"], "settle", {'delivered_quantity': 1000, 'cash_paid': 12500.0})
        self.assertEqual(record["payload"]["settled_action_ratio"], 0.5)
        # 交收后公告修正比例，已交收指令不受影响
        self.service.confirm_announcement(
            SPECIALIST(), "ANN-DIV", {'instrument': 'ACME', 'corporate_action': 'dividend', 'action_ratio': 0.8}
        )
        record = self.service.get_record(Actor("t", "trader"), record["id"])
        reversed_record = self.service.act(OFFICER(), record["id"], record["version"], "reverse", {'reverse_reason': '差错处理'})
        self.assertEqual(reversed_record["state"], "reversed")
        # 按交收时冻结的原比例0.5追回500，而不是按新比例0.8追回800
        self.assertEqual(reversed_record["payload"]["reversed_action_ratio"], 0.5)
        self.assertEqual(reversed_record["payload"]["cash_recovered"], 500.0)

    def test_concurrent_different_ratios_only_one_takes_effect(self):
        self.service.create(Actor("t", "trader"), "TRD-C1", dict(SPLIT))
        self.service.create(Actor("t", "trader"), "TRD-C2", dict(SPLIT, settlement_day=3))
        outcomes = []

        def confirm(ratio):
            try:
                result = self.service.confirm_announcement(
                    SPECIALIST(), "ANN-RACE", {'instrument': 'ACME', 'corporate_action': 'split', 'action_ratio': ratio}
                )
                outcomes.append(("ok", ratio, result["duplicate"]))
            except Conflict as exc:
                outcomes.append(("conflict", ratio, str(exc)))

        start = threading.Barrier(2)

        def worker(ratio):
            start.wait()
            confirm(ratio)

        threads = [threading.Thread(target=worker, args=(r,)) for r in (3.0, 4.0)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(outcomes), 2)
        statuses = sorted(outcome[0] for outcome in outcomes)
        self.assertEqual(statuses, ["conflict", "ok"])
        winner = next(outcome for outcome in outcomes if outcome[0] == "ok")
        stored = self.service.get_announcement(SPECIALIST(), "ANN-RACE")
        self.assertEqual(stored["action_ratio"], winner[1])
        # 全部指令只被调整一次，生效的是获胜比例
        for reference in ("TRD-C1", "TRD-C2"):
            [item] = [r for r in self.service.list_records(SPECIALIST(), limit=10) if r["reference"] == reference]
            self.assertEqual(item["payload"]["action_ratio"], winner[1])

    def test_announcement_retry_is_idempotent(self):
        record = self.service.create(Actor("t", "trader"), "TRD-R", dict(SPLIT))
        first = self.service.confirm_announcement(
            SPECIALIST(), "ANN-IDEM", {'instrument': 'ACME', 'corporate_action': 'split', 'action_ratio': 3.0}
        )
        self.assertEqual(len(first["adjusted"]), 1)
        version_after = self.service.get_record(Actor("t", "trader"), record["id"])["version"]

        retry = self.service.confirm_announcement(
            SPECIALIST(), "ANN-IDEM", {'instrument': 'ACME', 'corporate_action': 'split', 'action_ratio': 3.0}
        )
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["adjusted"], [])
        record = self.service.get_record(Actor("t", "trader"), record["id"])
        self.assertEqual(record["version"], version_after)
        timeline = self.service.timeline(SPECIALIST(), record["id"])
        self.assertEqual([event["action"] for event in timeline].count("announcement_adjusted"), 1)

        # 同公告以不同比例重试仍是冲突
        with self.assertRaises(Conflict):
            self.service.confirm_announcement(
                SPECIALIST(), "ANN-IDEM", {'instrument': 'ACME', 'corporate_action': 'split', 'action_ratio': 4.0}
            )

    def test_legacy_instructions_read_and_reverse_with_original_parameters(self):
        legacy_dividend = {
            'instrument': 'LEG', 'side': 'buy', 'quantity': 2000, 'price': 10.0, 'fees': 0.0,
            'currency': 'CNY', 'settlement_day': 3, 'corporate_action': 'dividend',
            'action_ratio': 0.25, 'gross_amount': 20000.0, 'net_amount': 20000.0,
            'adjusted_quantity': 2000, 'adjusted_price': 10.0, 'cash_entitlement': 500.0,
        }
        record_id = insert_legacy_record(self.service, "OLD-1", "approved", legacy_dividend)
        record = self.service.act(OFFICER(), record_id, 1, "settle", {'delivered_quantity': 2000, 'cash_paid': 20000.0})
        self.assertEqual(record["payload"]["settled_action_ratio"], 0.25)
        record = self.service.act(OFFICER(), record_id, record["version"], "reverse", {'reverse_reason': '旧指令冲正'})
        # 旧指令无original_action_ratio字段，按入库参数0.25追回
        self.assertEqual(record["payload"]["reversed_action_ratio"], 0.25)
        self.assertEqual(record["payload"]["cash_recovered"], 500.0)

        # 旧格式未交收指令也能被公告流程正常重算（原比例取action_ratio兜底）
        legacy_split = {
            'instrument': 'LEG', 'side': 'sell', 'quantity': 100, 'price': 8.0, 'fees': 0.0,
            'currency': 'CNY', 'settlement_day': 3, 'corporate_action': 'split',
            'action_ratio': 2.0, 'gross_amount': 800.0, 'net_amount': 800.0,
            'adjusted_quantity': 200, 'adjusted_price': 4.0,
        }
        legacy_id = insert_legacy_record(self.service, "OLD-2", "captured", legacy_split)
        self.service.confirm_announcement(
            SPECIALIST(), "ANN-LEG", {'instrument': 'LEG', 'corporate_action': 'split', 'action_ratio': 4.0}
        )
        record = self.service.get_record(SPECIALIST(), legacy_id)
        self.assertEqual(record["payload"]["adjusted_quantity"], 400)
        self.assertEqual(record["payload"]["action_ratio"], 4.0)

    def test_only_corporate_actions_specialist_may_confirm(self):
        self.service.create(Actor("t", "trader"), "TRD-P", dict(SPLIT))
        with self.assertRaises(PermissionDenied):
            self.service.confirm_announcement(
                OFFICER(), "ANN-P", {'instrument': 'ACME', 'corporate_action': 'split', 'action_ratio': 3.0}
            )
        with self.assertRaises(PermissionDenied):
            self.service.confirm_announcement(
                Actor("x", "trader"), "ANN-P2", {'instrument': 'ACME', 'corporate_action': 'split', 'action_ratio': 3.0}
            )

    def test_reversed_instructions_are_not_recomputed(self):
        record = self.service.create(Actor("t", "trader"), "TRD-V", dict(DIVIDEND))
        record = self.service.act(SPECIALIST(), record["id"], record["version"], "apply_corporate", {})
        record = self.service.act(OFFICER(), record["id"], record["version"], "approve", {})
        record = self.service.act(OFFICER(), record["id"], record["version"], "settle", {'delivered_quantity': 1000, 'cash_paid': 12500.0})
        record = self.service.act(OFFICER(), record["id"], record["version"], "reverse", {'reverse_reason': 'x'})
        version = record["version"]
        result = self.service.confirm_announcement(
            SPECIALIST(), "ANN-V", {'instrument': 'ACME', 'corporate_action': 'dividend', 'action_ratio': 0.9}
        )
        self.assertEqual(result["adjusted"], [])
        record = self.service.get_record(SPECIALIST(), record["id"])
        self.assertEqual(record["version"], version)
        self.assertEqual(record["payload"]["action_ratio"], 0.5)


class RulesUnitTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_frozen_ratio_fallback_chain(self):
        self.assertEqual(self.rules.frozen_ratio({'settled_action_ratio': 1.5, 'original_action_ratio': 2.0, 'action_ratio': 3.0}), 1.5)
        self.assertEqual(self.rules.frozen_ratio({'original_action_ratio': 2.0, 'action_ratio': 3.0}), 2.0)
        self.assertEqual(self.rules.frozen_ratio({'action_ratio': 3.0}), 3.0)

    def test_recompute_refreshes_effective_values_only_when_applied(self):
        payload = self.rules.prepare_create(dict(SPLIT))
        open_payload = self.rules.recompute(payload, 3.0)
        self.assertEqual(open_payload["adjusted_quantity"], 3000)
        self.assertNotIn("effective_quantity", open_payload)
        _, applied, _ = self.rules.apply_action({"state": "captured", "payload": payload}, "apply_corporate", {})
        applied_payload = self.rules.recompute(applied, 3.0)
        self.assertEqual(applied_payload["effective_quantity"], 3000)


if __name__ == "__main__":
    unittest.main()
