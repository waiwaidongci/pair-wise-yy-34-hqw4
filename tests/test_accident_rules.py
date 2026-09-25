import unittest

from src.accident_rules import (detect_conclusion_conflicts,
                                matches_pending_case, recompute_deadline,
                                recompute_severity, union_responsibilities,
                                windows_overlap)
from datetime import datetime, timezone


def case(**over):
    data = {
        "id": 1, "case_no": "AI1", "scene_no": "S1",
        "victim_name": "伤者", "victim_id": "ID1",
        "occurred_start": "2026-09-20T08:00:00+08:00",
        "occurred_end": "2026-09-20T09:00:00+08:00",
        "severity": "minor", "status": "open", "verdict": "pending",
        "conclusion": "", "responsibilities": [],
    }
    data.update(over)
    return data


class AccidentRulesTest(unittest.TestCase):
    def test_window_overlap(self):
        self.assertTrue(windows_overlap(
            "2026-09-20T08:00:00+08:00", "2026-09-20T09:00:00+08:00",
            "2026-09-20T08:30:00+08:00", "2026-09-20T10:00:00+08:00"))
        self.assertTrue(windows_overlap(
            "2026-09-20T08:00:00+08:00", "2026-09-20T09:00:00+08:00",
            "2026-09-20T09:00:00+08:00", "2026-09-20T10:00:00+08:00"))
        self.assertFalse(windows_overlap(
            "2026-09-20T08:00:00+08:00", "2026-09-20T09:00:00+08:00",
            "2026-09-21T08:00:00+08:00", "2026-09-21T09:00:00+08:00"))

    def test_match_requires_open_and_same_scene_or_person_window(self):
        report = case()
        self.assertTrue(matches_pending_case(report, case(id=2, case_no="AI2")))
        # 已关闭、已并入不命中
        self.assertFalse(matches_pending_case(
            report, case(id=2, status="closed")))
        self.assertFalse(matches_pending_case(
            report, case(id=2, status="merged")))
        # 现场编号相同即命中（伤者信息不同也成立）
        self.assertTrue(matches_pending_case(
            case(victim_name="甲", victim_id=None),
            case(id=2, victim_name="乙", victim_id=None,
                 occurred_start="2026-10-01T00:00:00+08:00",
                 occurred_end="2026-10-01T01:00:00+08:00")))
        # 同人但时段错开不命中
        self.assertFalse(matches_pending_case(
            report, case(id=2, scene_no="S2",
                         occurred_start="2026-10-01T00:00:00+08:00",
                         occurred_end="2026-10-01T01:00:00+08:00")))
        # 不同人且不同现场不命中
        self.assertFalse(matches_pending_case(
            case(scene_no="S1"),
            case(id=2, scene_no="X", victim_name="他人", victim_id="ID2")))

    def test_severity_and_deadline(self):
        self.assertEqual(
            recompute_severity([case(severity="minor"),
                                case(id=2, severity="fatal"),
                                case(id=3, severity="moderate")]),
            "fatal")
        start = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(recompute_deadline("fatal", start),
                         datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc))

    def test_union_responsibilities_keeps_master_first(self):
        a = case(id=1, responsibilities=["班组"])
        b = case(id=2, responsibilities=["设备科", "班组"])
        self.assertEqual(union_responsibilities([a, b], master_id=2),
                         ["设备科", "班组"])
        self.assertEqual(union_responsibilities([a, b], master_id=1),
                         ["班组", "设备科"])

    def test_conflict_detection(self):
        # 有责 vs 无责 => 冲突；待定不构成对立
        self.assertTrue(detect_conclusion_conflicts(
            [case(verdict="liable", conclusion="违章"),
             case(id=2, verdict="not_liable", conclusion="意外")]))
        self.assertFalse(detect_conclusion_conflicts(
            [case(verdict="liable", conclusion="违章"),
             case(id=2, verdict="pending")]))
        # 判定一致但结论文字冲突
        conflicts = detect_conclusion_conflicts(
            [case(verdict="liable", conclusion="说法一"),
             case(id=2, verdict="liable", conclusion="说法二")])
        self.assertTrue(any(c["type"] == "conclusion_text_conflict"
                            for c in conflicts))


if __name__ == "__main__":
    unittest.main()
