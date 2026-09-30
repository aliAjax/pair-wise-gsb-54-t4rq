"""区段接续档案：累计损耗预算、并发去重、幂等重试、晚到记录、历史补登、恢复复核。"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


def create_data(**overrides):
    data = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0,
            'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True,
            'capacity_gbps': 400, 'splice_loss_budget_db': 0.3}
    data.update(overrides)
    return data


class SpliceArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.engineer = Actor("eng-1", "cable_engineer")
        self.record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", create_data())
        self._to_state('approve', Actor("rm", "repair_manager"), {'repair_manager': 'RM-2'})
        self._to_state('mobilize', Actor("vm", "vessel_master"),
                       {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        self._to_state('survey', self.engineer, {'survey_complete': True, 'fault_location_km': 128})

    def tearDown(self):
        self.temp.cleanup()

    def _to_state(self, action, actor, data):
        self.record = self.service.act(actor, self.record["id"], self.record["version"], action, data)
        return self.record

    def _splice(self, submission_id, loss, point=128.0, moment='2026-09-01T08:00:00+00:00', spare=16):
        self.record = self.service.act(self.engineer, self.record["id"], self.record["version"], 'splice',
                                       {'submission_id': submission_id, 'splice_point_km': point, 'occurred_at': moment,
                                        'splice_loss_db': loss, 'spare_used_km': spare})
        return self.record

    def test_cumulative_loss_over_budget_returns_to_rectifying(self):
        # 第一段接续 0.12，累计 0.12 <= 0.3，进入 spliced
        self._splice('SUB-001', 0.12)
        self.assertEqual(self.record["state"], "spliced")
        # 假设整改/二次抢修后再来一条晚到记录：0.12+0.20=0.32 超过预算0.3
        outcome = self.service.submit_field_splice(
            self.engineer, self.record["id"],
            {'submission_id': 'SUB-LATE-1', 'splice_point_km': 130.0,
             'occurred_at': '2026-09-01T06:00:00+00:00', 'splice_loss_db': 0.20})
        self.assertEqual(outcome["accepted_as"], "confirmed")
        self.assertTrue(outcome["regressed"])
        self.assertEqual(outcome["record"]["state"], "rectifying")

    def test_rectify_replaces_bad_entry_and_budget_passes_again(self):
        self._splice('SUB-001', 0.28)
        self.assertEqual(self.record["state"], "spliced")
        # 第二条正式接续让累计 0.28+0.05=0.33 > 0.3 -> 退回待整改
        late = self.service.submit_field_splice(
            self.engineer, self.record["id"],
            {'submission_id': 'SUB-LATE-2', 'splice_point_km': 131.0,
             'occurred_at': '2026-09-01T07:00:00+00:00', 'splice_loss_db': 0.05})
        self.assertEqual(late["record"]["state"], "rectifying")
        self.record = self.service.get_record(self.engineer, self.record["id"])
        entries = self.service.repository.list_splice_entries(cable='SEA-1', segment='S3', status='confirmed')
        bad = next(e for e in entries if e["submission_id"] == "SUB-001")
        # 整改重做：以 0.18 的低损耗替换 0.28 的坏接续，累计 0.18+0.05=0.23 <= 0.3
        self.record = self.service.act(
            self.engineer, self.record["id"], self.record["version"], 'rectify',
            {'submission_id': 'SUB-FIX-1', 'replaces_entry_id': bad["id"], 'splice_point_km': 128.0,
             'occurred_at': '2026-09-02T09:00:00+00:00', 'splice_loss_db': 0.18})
        self.assertEqual(self.record["state"], "spliced")
        superseded = self.service.repository.get_splice_entry(bad["id"])
        self.assertEqual(superseded["status"], "superseded")
        archive = self.service.get_archive(self.engineer, 'SEA-1', 'S3')
        self.assertAlmostEqual(archive["cumulative_loss_db"], 0.23, places=6)

    def test_late_arriving_record_invalidates_previous_test(self):
        self._splice('SUB-001', 0.12)
        self._to_state('test', Actor("noc", "noc_operator"), {'end_to_end_loss_db': 0.3})
        self.assertEqual(self.record["state"], "tested")
        # 晚到记录更新档案，0.12+0.20=0.32 > 0.3：原测试结果失效
        outcome = self.service.submit_field_splice(
            self.engineer, self.record["id"],
            {'submission_id': 'SUB-LATE-3', 'splice_point_km': 129.5,
             'occurred_at': '2026-09-01T05:00:00+00:00', 'splice_loss_db': 0.20})
        self.assertEqual(outcome["record"]["state"], "rectifying")
        # tested -> rectifying 后不允许直接 restore
        with self.assertRaises(Conflict):
            self.service.act(Actor("noc", "noc_operator"), self.record["id"],
                             self.record["version"], 'restore',
                             {'traffic_restored': True, 'restore_capacity_gbps': 400})

    def test_two_engineers_submit_same_splice_only_one_confirmed(self):
        # 模拟两名工程师几乎同时提交同一接续点同一时刻：经线程竞态，一条confirmed一条pending
        results, errors = [], []

        def submit(submission_id, loss):
            try:
                results.append(self.service.repository.ingest_field_entry(
                    self.record["id"], self.engineer.user_id,
                    {'submission_id': submission_id, 'splice_point_km': 128.5,
                     'occurred_at': '2026-09-01T08:30:00+00:00', 'splice_loss_db': loss},
                    0.3))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=submit, args=('SUB-A', 0.11))
        t2 = threading.Thread(target=submit, args=('SUB-B', 0.12))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertFalse(errors)
        statuses = sorted(r["accepted_as"] for r in results)
        self.assertEqual(statuses, ["confirmed", "pending"])
        archive = self.service.get_archive(self.engineer, 'SEA-1', 'S3')
        # 只有一条计入累计
        self.assertEqual(archive["confirmed_count"], 1)
        self.assertEqual(archive["pending_count"], 1)
        self.assertIn(archive["cumulative_loss_db"], (0.11, 0.12))

    def test_pending_entry_blocks_restore_until_resolved(self):
        self._splice('SUB-001', 0.10)
        # 造一条 pending：与 SUB-001 同点同时刻的另一条现场数据
        outcome = self.service.submit_field_splice(
            self.engineer, self.record["id"],
            {'submission_id': 'SUB-DUP', 'splice_point_km': 128.0,
             'occurred_at': '2026-09-01T08:00:00+00:00', 'splice_loss_db': 0.10})
        self.assertEqual(outcome["accepted_as"], "pending")
        self._to_state('test', Actor("noc", "noc_operator"), {'end_to_end_loss_db': 0.3})
        with self.assertRaises(Conflict):
            self.service.act(Actor("noc", "noc_operator"), self.record["id"],
                             self.record["version"], 'restore',
                             {'traffic_restored': True, 'restore_capacity_gbps': 400})
        # 丢弃重复测量后可恢复
        pending = next(e for e in self.service.repository.list_splice_entries(status='pending')
                       if e["submission_id"] == "SUB-DUP")
        self.service.resolve_field_entry(self.engineer, pending["id"], {'decision': 'discard'})
        self.record = self.service.get_record(self.engineer, self.record["id"])
        self.record = self.service.act(Actor("noc", "noc_operator"), self.record["id"],
                                       self.record["version"], 'restore',
                                       {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(self.record["state"], "restored")

    def test_retry_with_same_submission_id_does_not_double_count(self):
        first = self._splice('SUB-RETRY', 0.11)
        version = first["version"]
        # 写入超时后客户端用相同 submission_id 重试：直接返回既有条目，状态机不再推进
        retry = self.service.act(self.engineer, self.record["id"], version, 'splice',
                                 {'submission_id': 'SUB-RETRY', 'splice_point_km': 128.0,
                                  'occurred_at': '2026-09-01T08:00:00+00:00', 'splice_loss_db': 0.11,
                                  'spare_used_km': 16})
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(retry["version"], version)
        archive = self.service.get_archive(self.engineer, 'SEA-1', 'S3')
        self.assertEqual(archive["confirmed_count"], 1)
        self.assertAlmostEqual(archive["cumulative_loss_db"], 0.11, places=6)

    def test_field_entry_retry_is_idempotent(self):
        # 先推进到spliced再接收现场数据
        self._splice('SUB-001', 0.10)
        data = {'submission_id': 'SUB-F-RETRY', 'splice_point_km': 130.0,
                'occurred_at': '2026-09-01T09:00:00+00:00', 'splice_loss_db': 0.05}
        first = self.service.submit_field_splice(self.engineer, self.record["id"], data)
        second = self.service.submit_field_splice(self.engineer, self.record["id"], dict(data))
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["entry"]["id"], first["entry"]["id"])
        archive = self.service.get_archive(self.engineer, 'SEA-1', 'S3')
        self.assertEqual(archive["confirmed_count"], 2)
        self.assertEqual(archive["pending_count"], 0)

    def test_old_ticket_must_backfill_history_before_further_actions(self):
        # 新建一张带2个未登记历史接续的旧单
        old = self.service.create(
            Actor("creator", "noc_operator"), "CABLE-LEGACY-9",
            create_data(start_km=200.0, end_km=220.0, segment='S9'))
        # 用带 legacy 的数据重建（create已用默认值，这里直接验证默认情况），换一条显式带遗留项的单
        old2 = self.service.create(
            Actor("creator", "noc_operator"), "CABLE-LEGACY-10",
            create_data(start_km=200.0, end_km=220.0, segment='S10', legacy_unrecorded_splices=2))
        for action, actor, data in [
            ('approve', Actor("rm", "repair_manager"), {'repair_manager': 'RM-2'}),
            ('mobilize', Actor("vm", "vessel_master"),
             {'weather_window_hours': 40, 'available_spare_km': 24, 'vessel_name': 'CS-2'}),
            ('survey', self.engineer, {'survey_complete': True, 'fault_location_km': 210}),
        ]:
            old2 = self.service.act(actor, old2["id"], old2["version"], action, data)
        old2 = self.service.act(self.engineer, old2["id"], old2["version"], 'splice',
                                {'submission_id': 'SUB-OLD-1', 'splice_point_km': 210.0,
                                 'occurred_at': '2026-08-01T08:00:00+00:00', 'splice_loss_db': 0.08,
                                 'spare_used_km': 22})
        self.assertEqual(old2["state"], "spliced")
        # 历史未补齐：test/restore 被冻结
        with self.assertRaises(Conflict):
            self.service.act(Actor("noc", "noc_operator"), old2["id"], old2["version"],
                             'test', {'end_to_end_loss_db': 0.3})
        # 先补1项，还差1项，仍不能继续
        out = self.service.act(self.engineer, old2["id"], old2["version"], 'backfill',
                               {'items': [{'submission_id': 'HIST-1', 'splice_point_km': 205.0,
                                           'occurred_at': '2026-07-01T08:00:00+00:00',
                                           'splice_loss_db': 0.07}]})
        self.assertFalse(out["payload"]["history_complete"])
        old2 = out
        with self.assertRaises(Conflict):
            self.service.act(Actor("noc", "noc_operator"), old2["id"], old2["version"],
                             'test', {'end_to_end_loss_db': 0.3})
        # 补齐第2项后可以继续；0.08+0.07+0.07=0.22 <= 0.3
        out = self.service.act(self.engineer, old2["id"], old2["version"], 'backfill',
                               {'items': [{'submission_id': 'HIST-2', 'splice_point_km': 215.0,
                                           'occurred_at': '2026-07-02T08:00:00+00:00',
                                           'splice_loss_db': 0.07}]})
        self.assertTrue(out["payload"]["history_complete"])
        old2 = out
        old2 = self.service.act(Actor("noc", "noc_operator"), old2["id"], old2["version"],
                                'test', {'end_to_end_loss_db': 0.3})
        old2 = self.service.act(Actor("rm2", "repair_manager"), old2["id"], old2["version"], 'restore',
                                {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(old2["state"], "restored")

    def test_backfill_retry_does_not_double_count(self):
        old2 = self.service.create(
            Actor("creator", "noc_operator"), "CABLE-LEGACY-11",
            create_data(start_km=300.0, end_km=320.0, segment='S11', legacy_unrecorded_splices=1))
        for action, actor, data in [
            ('approve', Actor("rm", "repair_manager"), {'repair_manager': 'RM-2'}),
            ('mobilize', Actor("vm", "vessel_master"),
             {'weather_window_hours': 40, 'available_spare_km': 24, 'vessel_name': 'CS-2'}),
            ('survey', self.engineer, {'survey_complete': True, 'fault_location_km': 310}),
            ('splice', self.engineer, {'submission_id': 'SUB-OLD-11', 'splice_point_km': 310.0,
                                       'occurred_at': '2026-08-01T08:00:00+00:00', 'splice_loss_db': 0.08,
                                       'spare_used_km': 22}),
        ]:
            old2 = self.service.act(actor, old2["id"], old2["version"], action, data)
        payload = {'items': [{'submission_id': 'HIST-11', 'splice_point_km': 305.0,
                              'occurred_at': '2026-07-01T08:00:00+00:00', 'splice_loss_db': 0.07}]}
        out = self.service.act(self.engineer, old2["id"], old2["version"], 'backfill', payload)
        self.assertTrue(out["payload"]["history_complete"])
        # 重试：同 submission_id 整笔拒绝，版本不变、累计不变
        with self.assertRaises(Conflict):
            self.service.act(self.engineer, old2["id"], old2["version"], 'backfill', payload)
        archive = self.service.get_archive(self.engineer, 'SEA-1', 'S11')
        self.assertEqual(archive["confirmed_count"], 2)
        self.assertAlmostEqual(archive["cumulative_loss_db"], 0.15, places=6)

    def test_backfill_over_budget_regresses_to_rectifying(self):
        old2 = self.service.create(
            Actor("creator", "noc_operator"), "CABLE-LEGACY-12",
            create_data(start_km=400.0, end_km=420.0, segment='S12', legacy_unrecorded_splices=1))
        for action, actor, data in [
            ('approve', Actor("rm", "repair_manager"), {'repair_manager': 'RM-2'}),
            ('mobilize', Actor("vm", "vessel_master"),
             {'weather_window_hours': 40, 'available_spare_km': 24, 'vessel_name': 'CS-2'}),
            ('survey', self.engineer, {'survey_complete': True, 'fault_location_km': 410}),
            ('splice', self.engineer, {'submission_id': 'SUB-OLD-12', 'splice_point_km': 410.0,
                                       'occurred_at': '2026-08-01T08:00:00+00:00', 'splice_loss_db': 0.2,
                                       'spare_used_km': 22}),
        ]:
            old2 = self.service.act(actor, old2["id"], old2["version"], action, data)
        # 历史项 0.15 -> 累计 0.35 > 0.3：补登后退回待整改，即使是旧单也不能放行
        out = self.service.act(self.engineer, old2["id"], old2["version"], 'backfill',
                               {'items': [{'submission_id': 'HIST-12', 'splice_point_km': 405.0,
                                           'occurred_at': '2026-07-01T08:00:00+00:00',
                                           'splice_loss_db': 0.15}]})
        self.assertEqual(out["state"], "rectifying")

    def test_archive_accumulates_across_repair_orders(self):
        # 两张故障单同区段（里程不重叠）：累计在档案层共享
        self._splice('SUB-001', 0.12)
        second = self.service.create(Actor("creator", "noc_operator"), "CABLE-30002",
                                     create_data(start_km=140.0, end_km=150.0, splice_loss_budget_db=0.3))
        # 新单在 detected，直接写一条晚到现场数据不被允许，验证状态门
        with self.assertRaises(Conflict):
            self.service.submit_field_splice(
                self.engineer, second["id"],
                {'submission_id': 'SUB-X', 'splice_point_km': 121.0,
                 'occurred_at': '2026-09-01T10:00:00+00:00', 'splice_loss_db': 0.05})
        archive = self.service.get_archive(self.engineer, 'SEA-1', 'S3')
        self.assertAlmostEqual(archive["cumulative_loss_db"], 0.12, places=6)
        self.assertEqual(archive["loss_budget_db"], 0.3)


if __name__ == "__main__":
    unittest.main()
