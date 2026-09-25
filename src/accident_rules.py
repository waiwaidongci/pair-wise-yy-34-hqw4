"""重复事故合并规则（纯函数，不接触存储与HTTP）。

职责：
- 判定一份补报是否命中同类“未结案”事故；
- 合并前的结论冲突检测；
- 合并后严重度、期限、责任的重算；
- 角色矩阵。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .accident_domain import (CASE_CLOSED, CASE_MERGED, SEVERITIES,
                              ValidationError)
from .rules import DEADLINE_HOURS

CASE_NO_PREFIX = "AI"
GROUP_NO_PREFIX = "MG"

REPORT_ROLES = {"reporter", "investigator", "safety_manager"}
MERGE_ROLES = {"safety_manager"}
AMEND_ROLES = {"investigator", "safety_manager"}
VIEW_ROLES = {"reporter", "investigator", "safety_manager", "viewer"}

# 时段重叠判定允许的最大间隔（小时）：端点相接也算同一时段
WINDOW_GRACE_HOURS = 0


def _parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("时间必须是ISO 8601格式") from exc


def normalize_id_card(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.strip().upper() if not ch.isspace())


def windows_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """两个事发时段是否重叠（允许 WINDOW_GRACE_HOURS 的相接宽限）。"""
    a_start, a_end = _parse_iso(start_a), _parse_iso(end_a)
    b_start, b_end = _parse_iso(start_b), _parse_iso(end_b)
    grace = timedelta(hours=WINDOW_GRACE_HOURS)
    return a_start <= b_end + grace and b_start <= a_end + grace


def same_scene(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    sa, sb = (a.get("scene_no") or "").strip().upper(), (b.get("scene_no") or "").strip().upper()
    return bool(sa and sb and sa == sb)


def same_victim(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """伤者身份一致：优先比对证件号，缺失时比对姓名。"""
    id_a, id_b = normalize_id_card(a.get("victim_id")), normalize_id_card(b.get("victim_id"))
    if id_a and id_b:
        return id_a == id_b
    name_a = (a.get("victim_name") or "").strip()
    name_b = (b.get("victim_name") or "").strip()
    return bool(name_a and name_b and name_a == name_b)


def is_open_case(case: Dict[str, Any]) -> bool:
    """只有未结案事故可参与合并；已关闭、已并入的都不命中。"""
    return case.get("status") not in (CASE_CLOSED, CASE_MERGED)


def matches_pending_case(report: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
    """命中同类未结案事故：同一现场编号，或伤者身份一致且事发时段重叠。"""
    if not is_open_case(candidate):
        return False
    if same_scene(report, candidate):
        return True
    return same_victim(report, candidate) and windows_overlap(
        report["occurred_start"], report["occurred_end"],
        candidate["occurred_start"], candidate["occurred_end"])


def match_reasons(report: Dict[str, Any], candidate: Dict[str, Any]) -> List[str]:
    reasons = []
    if same_scene(report, candidate):
        reasons.append("同一现场编号")
    if same_victim(report, candidate):
        reasons.append("伤者身份一致")
        if windows_overlap(report["occurred_start"], report["occurred_end"],
                           candidate["occurred_start"], candidate["occurred_end"]):
            reasons.append("事发时段重叠")
    return reasons


def detect_conclusion_conflicts(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """两边结论冲突就中止。冲突口径：

    1. 判定结论（verdict）出现互斥的已定结论（有责/无责），待定不参与对立；
    2. 已定结论双方都写了结论文字且内容不一致。
    """
    conflicts: List[Dict[str, Any]] = []
    decided = [c for c in cases if c.get("verdict") in ("liable", "not_liable")]
    verdicts = {c["verdict"] for c in decided}
    if len(verdicts) > 1:
        verdict_text = {"liable": "有责", "not_liable": "无责"}
        detail = "、".join(
            f"{c['case_no']}={verdict_text[c['verdict']]}" for c in decided)
        conflicts.append({
            "type": "verdict_conflict",
            "message": f"结论判定冲突：{detail}",
            "cases": [{"case_id": c["id"], "case_no": c["case_no"],
                       "verdict": c["verdict"]} for c in decided],
        })
    texted = [c for c in decided if (c.get("conclusion") or "").strip()]
    for i in range(len(texted)):
        for j in range(i + 1, len(texted)):
            a, b = texted[i], texted[j]
            if a["conclusion"].strip() != b["conclusion"].strip():
                conflicts.append({
                    "type": "conclusion_text_conflict",
                    "message": f"结论文字冲突：{a['case_no']} 与 {b['case_no']} 结论不一致",
                    "cases": [
                        {"case_id": a["id"], "case_no": a["case_no"],
                         "conclusion": a["conclusion"]},
                        {"case_id": b["id"], "case_no": b["case_no"],
                         "conclusion": b["conclusion"]},
                    ],
                })
    return conflicts


def recompute_severity(cases: List[Dict[str, Any]]) -> str:
    """严重度按新材料重算：取各单最高级，只能上调、不回落。"""
    levels = [SEVERITIES.index(c["severity"]) for c in cases if c.get("severity") in SEVERITIES]
    if not levels:
        raise ValidationError("缺少可用于重算严重度的材料")
    return SEVERITIES[max(levels)]


def recompute_deadline(severity: str, merged_at: datetime) -> datetime:
    """期限按新严重度对应的处置时限，自合并完成时重新起算。"""
    if severity not in DEADLINE_HOURS:
        raise ValidationError("unknown severity")
    return merged_at + timedelta(hours=DEADLINE_HOURS[severity])


def union_responsibilities(cases: List[Dict[str, Any]], master_id: int) -> List[str]:
    """责任按新材料重算：主单责任方保留在前，其余按首次出现并入、去重。"""
    ordered = sorted(cases, key=lambda c: (c["id"] != master_id, c["id"]))
    result: List[str] = []
    for case in ordered:
        for party in case.get("responsibilities") or []:
            if party not in result:
                result.append(party)
    return result


def dedupe_key_kind(item: Dict[str, Any]) -> tuple:
    return (item.get("kind") or "", (item.get("title") or "").strip().lower())


def dedupe_key_measure(item: Dict[str, Any]) -> tuple:
    return ((item.get("title") or "").strip().lower(),)
