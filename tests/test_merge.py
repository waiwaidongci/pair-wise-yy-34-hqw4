import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


def accident_payload(reference, source="team", severity="minor", quantity=1,
                     threshold=10, start="2026-09-25T08:00:00+00:00",
                     end="2026-09-25T09:00:00+00:00", site="SITE-1",
                     person="王五", responsibility=None, conclusion_code=None):
    return {
        "title": reference, "description": "duplicate accident",
        "severity": severity, "quantity": quantity, "threshold": threshold,
        "external_ref": reference, "site_ref": site, "injured_person": person,
        "incident_start": start, "incident_end": end,
        "report_source": source, "responsibility": responsibility,
        "conclusion_code": conclusion_code,
    }


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_match_merge_transfer_recalculate_and_undo(self):
        team = self.service.create_item(
            accident_payload("TEAM-1", responsibility="班组初判责任"),
            "leader", "reporter")
        self.service.add_record(team["id"], {
            "kind": "evidence", "detail": "班组照片", "status": "closed",
            "external_ref": "EV-TEAM",
        }, "leader", "investigator")
        self.service.add_record(team["id"], {
            "kind": "action", "detail": "班组临时措施", "status": "open",
            "external_ref": "ACT-TEAM",
        }, "leader", "investigator")

        supplement = self.service.create_item(accident_payload(
            "SAFE-1", source="safety", severity="fatal", quantity=3,
            threshold=5, start="2026-09-25T08:30:00+00:00",
            end="2026-09-25T09:30:00+00:00",
            responsibility="安全科核定责任", conclusion_code="work_related"),
            "safety", "safety_manager")
        self.assertEqual(supplement["merge_status"], "pending")
        request_id = supplement["merge_request_id"]

        request = self.service.get_merge_request(request_id, "safety_manager")
        self.assertEqual(request["status"], "pending")
        self.assertEqual({m["external_ref"] for m in request["members"]},
                         {"TEAM-1", "SAFE-1"})

        merged = self.service.merge_request(request_id, {
            "master_item_id": supplement["id"],
            "expected_version": supplement["version"],
        }, "safety", "safety_manager")
        self.assertEqual(merged["status"], "merged")

        master = self.service.get_item(supplement["id"], "viewer")
        source = self.service.get_item(team["id"], "viewer")
        self.assertEqual(master["severity"], "fatal")
        self.assertEqual(master["quantity"], 3)
        self.assertEqual(master["threshold"], 5)
        self.assertEqual(master["deadline_hours"], 4)
        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["external_ref"], "TEAM-1")
        self.assertEqual(len(self.service.list_records(master["id"], "viewer")), 2)
        self.assertEqual(self.service.list_records(source["id"], "viewer"), [])
        self.assertEqual(master["accident_report"]["responsibility"], "安全科核定责任")
        self.assertEqual(master["accident_report"]["conclusion_code"], "work_related")
        with self.assertRaises(ConflictError):
            self.service.add_record(source["id"], {
                "kind": "evidence", "detail": "不能再向被合并单登记",
            }, "x", "investigator")

        undone = self.service.undo_merge(request_id, "safety", "safety_manager")
        self.assertEqual(undone["status"], "undone")
        restored_source = self.service.get_item(source["id"], "viewer")
        restored_master = self.service.get_item(master["id"], "viewer")
        self.assertEqual(restored_source["status"], "reported")
        self.assertEqual(restored_source["external_ref"], "TEAM-1")
        self.assertEqual(restored_source["severity"], "minor")
        self.assertEqual(restored_master["severity"], "fatal")
        self.assertEqual(restored_master["external_ref"], "SAFE-1")
        self.assertEqual(len(self.service.list_records(source["id"], "viewer")), 2)
        self.assertEqual(self.service.list_records(master["id"], "viewer"), [])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_conflicting_conclusions_stop_merge_and_record_conflict(self):
        team = self.service.create_item(accident_payload(
            "TEAM-C1", conclusion_code="work_related"), "leader", "reporter")
        safety = self.service.create_item(accident_payload(
            "SAFE-C1", source="safety",
            start="2026-09-25T08:30:00+00:00",
            end="2026-09-25T09:30:00+00:00",
            conclusion_code="not_work_related"), "safety", "safety_manager")
        request_id = safety["merge_request_id"]

        with self.assertRaises(ConflictError) as caught:
            self.service.merge_request(request_id, {
                "master_item_id": safety["id"],
                "expected_version": safety["version"],
            }, "safety", "safety_manager")
        self.assertIn("冲突", str(caught.exception))

        request = self.service.get_merge_request(request_id, "viewer")
        self.assertEqual(request["status"], "conflicted")
        self.assertIn("定性冲突", request["reason"])
        self.assertEqual(len(request["conflict_detail"]), 2)
        self.assertEqual(self.service.get_item(team["id"], "viewer")["status"], "reported")
        self.assertEqual(self.service.list_records(team["id"], "viewer"), [])

    def test_no_merge_without_same_identity_and_overlapping_window(self):
        first = self.service.create_item(
            accident_payload("N-1", person="赵六"), "a", "reporter")
        second = self.service.create_item(accident_payload(
            "N-2", person="钱七",
            start="2026-09-25T10:00:00+00:00",
            end="2026-09-25T11:00:00+00:00"), "b", "safety_manager")
        self.assertNotIn("merge_request_id", first)
        self.assertNotIn("merge_request_id", second)
        self.assertEqual(self.service.list_merge_requests("viewer"), [])

    def test_only_safety_can_merge(self):
        self.service.create_item(accident_payload("P-1"), "a", "reporter")
        safety = self.service.create_item(accident_payload(
            "P-2", source="safety",
            start="2026-09-25T08:30:00+00:00",
            end="2026-09-25T09:30:00+00:00"), "b", "safety_manager")
        with self.assertRaises(PermissionDenied):
            self.service.merge_request(safety["merge_request_id"], {
                "master_item_id": safety["id"],
                "expected_version": safety["version"],
            }, "reporter", "reporter")


if __name__ == "__main__":
    unittest.main()
