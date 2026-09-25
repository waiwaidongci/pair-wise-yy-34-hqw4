import tempfile
import unittest
from pathlib import Path

from src.accident_domain import (MergeConflictDetected, MergeStateError,
                                 ValidationError)
from src.accident_repository import AccidentRepository
from src.accident_service import AccidentService
from src.domain import PermissionDenied


def base_report(**over):
    payload = {
        "source": "crew",
        "scene_no": "SCN-1",
        "victim_name": "李四",
        "victim_id": "ID-LS-1",
        "occurred_start": "2026-09-20T08:00:00+08:00",
        "occurred_end": "2026-09-20T09:00:00+08:00",
        "severity": "minor",
        "responsibilities": [],
        "verdict": "pending",
    }
    payload.update(over)
    return payload


class AccidentMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = AccidentRepository(str(Path(self.tmp.name) / "a.db"))
        self.svc = AccidentService(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _crew(self, **over):
        return self.svc.report_accident(
            base_report(**over), "crew-user", "reporter")

    def _safety(self, **over):
        payload = base_report(source="safety", **over)
        return self.svc.report_accident(payload, "safe-user", "safety_manager")

    def test_report_registers_keys_and_no_false_match(self):
        first = self._crew()
        self.assertIsNone(first["merge"])
        # 不同伤者、不同现场、时段不重叠 -> 不命中
        other = self._safety(scene_no="SCN-9", victim_name="王五",
                             victim_id="ID-WW",
                             occurred_start="2026-09-21T08:00:00+08:00",
                             occurred_end="2026-09-21T09:00:00+08:00")
        self.assertIsNone(other["merge"])

    def test_scene_or_identity_window_match(self):
        first = self._crew(verdict="pending")
        # 同一现场编号，即使伤者信息缺失也命中
        by_scene = self._safety(victim_name="赵六", victim_id=None,
                                occurred_start="2026-09-20T20:00:00+08:00",
                                occurred_end="2026-09-20T21:00:00+08:00")
        self.assertEqual(by_scene["merge"]["status"], "pending")
        self.assertEqual(by_scene["merge"]["matched_cases"][0]["reasons"],
                         ["同一现场编号"])
        # 同伤者 + 时段重叠（现场编号不同）也命中，并与上一个待合并组桥接
        by_person = self._crew(scene_no="SCN-2",
                               occurred_start="2026-09-20T08:30:00+08:00",
                               occurred_end="2026-09-20T09:30:00+08:00")
        self.assertEqual(by_person["merge"]["id"],
                         by_scene["merge"]["id"])
        group = self.svc.get_group(by_scene["merge"]["id"], "viewer")
        self.assertEqual(len(group["members"]), 3)
        # 同伤者但时段完全错开不命中
        apart = self._crew(scene_no="SCN-3",
                           occurred_start="2026-09-25T08:30:00+08:00",
                           occurred_end="2026-09-25T09:30:00+08:00")
        self.assertIsNone(apart["merge"])

    def test_closed_and_merged_cases_are_not_matched(self):
        first = self._crew()
        self.repo.conn.execute(
            "UPDATE accidents SET status='closed' WHERE id=?", (first["id"],))
        second = self._safety()
        self.assertIsNone(second["merge"])

    def test_merge_transfers_and_recomputes(self):
        first = self._crew(
            severity="minor", responsibilities=["班组"], verdict="liable",
            conclusion="已确认违章")
        self.svc.add_evidence(first["id"], {"kind": "photo",
                              "title": "现场照片", "detail": "x"},
                              "u", "investigator")
        self.svc.add_measure(first["id"], {"title": "停工培训", "detail": "d"},
                             "u", "investigator")
        second = self._safety(
            severity="serious", responsibilities=["设备科"], verdict="liable",
            conclusion="已确认违章")
        self.svc.add_evidence(second["id"], {"kind": "report",
                               "title": "调查笔录", "detail": "y"},
                              "u", "safety_manager")
        self.svc.add_measure(second["id"], {"title": "设备检修", "detail": "e"},
                             "u", "safety_manager")
        gid = second["merge"]["id"]

        group = self.svc.designate_master(
            gid, {"master_case_id": second["id"]}, "boss", "safety_manager")
        self.assertEqual(group["status"], "merged")
        master = self.svc.get_accident(second["id"], "viewer")
        # 严重度取最高；期限按serious重算；责任合并（主单责任在前）
        self.assertEqual(master["severity"], "serious")
        self.assertIsNotNone(master["deadline_due"])
        self.assertEqual(master["responsibilities"], ["设备科", "班组"])
        titles = {e["title"] for e in master["evidence"]}
        self.assertEqual(titles, {"调查笔录", "现场照片"})
        self.assertEqual({m["title"] for m in master["measures"]},
                         {"设备检修", "停工培训"})
        # 曾用编号保留：并入单编号登记到主单
        self.assertIn(first["case_no"],
                      [a["alias_no"] for a in master["aliases"]])
        subordinate = self.svc.get_accident(first["id"], "viewer")
        self.assertEqual(subordinate["status"], "merged")
        self.assertEqual(subordinate["merged_into"], second["id"])

    def test_conflict_stops_and_amend_unblocks(self):
        first = self._crew(verdict="liable", conclusion="班组违章")
        second = self._safety(verdict="not_liable", conclusion="设备故障")
        gid = second["merge"]["id"]
        with self.assertRaises(MergeConflictDetected) as ctx:
            self.svc.designate_master(
                gid, {"master_case_id": second["id"]}, "boss",
                "safety_manager")
        self.assertTrue(any(c["type"] == "verdict_conflict"
                            for c in ctx.exception.message["conflicts"]))
        group = self.svc.get_group(gid, "viewer")
        self.assertEqual(group["status"], "conflict")
        self.assertEqual(group["master_case_id"], second["id"])
        # 冲突未解除前仍不能强行合并
        with self.assertRaises(MergeConflictDetected):
            self.svc.designate_master(
                gid, {"master_case_id": second["id"]}, "boss",
                "safety_manager")
        # 按新材料修正后冲突解除，可再次指定主单完成合并
        self.svc.amend_conclusion(
            second["id"], {"verdict": "liable", "conclusion": "班组违章",
                           "reason": "复查确认"},
            "boss", "safety_manager")
        group = self.svc.designate_master(
            gid, {"master_case_id": second["id"]}, "boss", "safety_manager")
        self.assertEqual(group["status"], "merged")

    def test_revoke_restores_originals_keeps_aliases(self):
        first = self._crew(severity="minor", responsibilities=["班组"])
        second = self._safety(severity="serious", responsibilities=["设备科"])
        gid = second["merge"]["id"]
        self.svc.designate_master(
            gid, {"master_case_id": second["id"]}, "boss", "safety_manager")
        restored = self.svc.revoke_merge(
            gid, {"reason": "选错主单"}, "boss", "safety_manager")
        self.assertEqual(restored["status"], "revoked")
        a = self.svc.get_accident(first["id"], "viewer")
        b = self.svc.get_accident(second["id"], "viewer")
        self.assertEqual(a["status"], "open")
        self.assertEqual(a["severity"], "minor")
        self.assertEqual(a["responsibilities"], ["班组"])
        self.assertEqual(b["status"], "open")
        self.assertEqual(b["severity"], "serious")
        self.assertEqual(b["responsibilities"], ["设备科"])
        # 转入证据/措施已退回删除；曾用编号仍保留
        self.assertEqual(len(b["evidence"]), 0)
        self.assertEqual(len(a["measures"]), 0)
        self.assertTrue(any(x["alias_no"] == first["case_no"]
                            for x in b["aliases"]))
        # 已撤销的组不能再次撤销
        with self.assertRaises(MergeStateError):
            self.svc.revoke_merge(gid, None, "boss", "safety_manager")

    def test_re_remerge_after_revoke(self):
        first = self._crew()
        second = self._safety()
        gid = second["merge"]["id"]
        self.svc.designate_master(
            gid, {"master_case_id": second["id"]}, "boss", "safety_manager")
        self.svc.revoke_merge(
            gid, {"reason": "误并"}, "boss", "safety_manager")
        # 撤销后补报第三单仍能命中恢复为未结案的原件并形成新组
        third = self._crew(scene_no="SCN-1")
        self.assertIsNotNone(third["merge"])
        self.assertEqual(third["merge"]["status"], "pending")

    def test_permissions_and_master_membership(self):
        first = self._crew()
        second = self._safety()
        gid = second["merge"]["id"]
        with self.assertRaises(PermissionDenied):
            self.svc.designate_master(
                gid, {"master_case_id": second["id"]}, "x", "reporter")
        with self.assertRaises(ValidationError):
            self.svc.designate_master(
                gid, {"master_case_id": 9999}, "boss", "safety_manager")
        with self.assertRaises(PermissionDenied):
            self.svc.report_accident(
                base_report(), "x", "viewer")

    def test_audit_chain(self):
        first = self._crew()
        second = self._safety()
        gid = second["merge"]["id"]
        self.svc.designate_master(
            gid, {"master_case_id": second["id"]}, "boss", "safety_manager")
        self.svc.revoke_merge(
            gid, {"reason": "r"}, "boss", "safety_manager")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
