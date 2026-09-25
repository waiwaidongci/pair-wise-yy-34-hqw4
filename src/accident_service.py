"""重复事故合并的用例编排：权限、校验、调用规则层与存储层、审计。

规则在 accident_rules，持久化在 accident_repository，本层只做流程编排。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .accident_domain import (GROUP_ASSIGNABLE, GROUP_CONFLICT,
                              MergeConflictDetected, MergeStateError,
                              normalize_severity, normalize_source,
                              normalize_str_list, normalize_verdict,
                              require_case_window, require_scene_no)
from .accident_repository import AccidentRepository
from .accident_rules import (AMEND_ROLES, MERGE_ROLES, REPORT_ROLES,
                             VIEW_ROLES, detect_conclusion_conflicts,
                             match_reasons, matches_pending_case,
                             recompute_deadline, recompute_severity,
                             union_responsibilities)
from .audit import utc_now
from .domain import (ValidationError, ensure_role, require_text)


class AccidentService:
    def __init__(self, repository: AccidentRepository):
        self.repository = repository

    # ---------- 补报 ----------
    def report_accident(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        """班组/安全科补报：登记现场编号、伤者身份、事发时段；
        命中同类未结案事故则进入待合并组。"""
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        source = normalize_source(payload.get("source"))
        victim_name = require_text(payload.get("victim_name"), "victim_name", 100)
        victim_id = payload.get("victim_id")
        if victim_id is not None:
            victim_id = require_text(victim_id, "victim_id", 40)
        start, end = require_case_window(payload)
        scene_no = require_scene_no(payload.get("scene_no"))
        if not scene_no and not victim_name:
            raise ValidationError("必须至少登记现场编号或伤者身份")
        location = payload.get("location")
        if location is not None:
            location = require_text(location, "location", 200)
        severity = normalize_severity(payload.get("severity"))
        responsibilities = normalize_str_list(payload.get("responsibilities"),
                                              "responsibilities")
        verdict = normalize_verdict(payload.get("verdict"))
        conclusion = payload.get("conclusion")
        if conclusion is not None:
            conclusion = require_text(conclusion, "conclusion", 2000)

        accident = self.repository.create_accident({
            "source": source, "scene_no": scene_no, "victim_name": victim_name,
            "victim_id": victim_id, "occurred_start": start, "occurred_end": end,
            "location": location, "severity": severity,
            "responsibilities": responsibilities, "verdict": verdict,
            "conclusion": conclusion,
        }, actor)
        for ev in payload.get("evidence", []) or []:
            self._create_evidence(accident["id"], ev, actor)
        for mv in payload.get("measures", []) or []:
            self._create_measure(accident["id"], mv, actor)

        candidates = [c for c in self.repository.list_open_accidents()
                      if c["id"] != accident["id"]
                      and matches_pending_case(accident, c)]
        hit_group = self._link_to_merge_group(accident, candidates, actor)

        result = self._detail(accident)
        if hit_group is not None:
            result["merge"] = self._merge_section(accident["id"], hit_group)
            result["merge"]["matched_cases"] = [
                {"case_id": c["id"], "case_no": c["case_no"],
                 "reasons": match_reasons(accident, c)} for c in candidates]
        self.repository.append_audit(
            "accident.report", "accident", accident["id"], actor, {
                "case_no": accident["case_no"], "scene_no": scene_no,
                "source": source, "matched": len(candidates),
                "group_id": hit_group["id"] if hit_group else None})
        return result

    def _link_to_merge_group(self, accident: Dict[str, Any],
                             candidates: List[Dict[str, Any]],
                             actor: str) -> Optional[Dict[str, Any]]:
        """命中后归入候选所在的待合并组（多组桥接时归并为最早组）；
        都不在组内则新建待合并组。未命中不建组。"""
        if not candidates:
            return None
        groups: Dict[int, Dict[str, Any]] = {}
        for c in candidates:
            g = self.repository.group_of_open_accident(c["id"])
            if g is not None:
                groups[g["id"]] = g
        now = utc_now()
        if groups:
            ordered = sorted(groups.values(), key=lambda g: g["id"])
            target = ordered[0]
            self.repository.merge_groups_into(
                target["id"], [g["id"] for g in ordered[1:]], now)
            self.repository.add_member(target["id"], accident["id"], "member", now)
            return self.repository.get_group(target["id"])
        group = self.repository.create_group()
        self.repository.add_member(group["id"], candidates[0]["id"], "member", now)
        self.repository.add_member(group["id"], accident["id"], "member", now)
        self.repository.append_audit(
            "merge.group_created", "merge_group", group["id"], actor, {
                "group_no": group["group_no"],
                "members": [candidates[0]["id"], accident["id"]]})
        return group

    # ---------- 证据 / 措施 ----------
    def _create_evidence(self, accident_id: int, item: Dict[str, Any],
                         actor: str) -> None:
        kind = require_text(item.get("kind"), "evidence.kind", 60)
        title = require_text(item.get("title"), "evidence.title", 200)
        detail = ""
        if item.get("detail") is not None:
            detail = require_text(item.get("detail"), "evidence.detail", 2000)
        self.repository.add_evidence(accident_id, kind, title, detail, actor)

    def _create_measure(self, accident_id: int, item: Dict[str, Any],
                        actor: str) -> None:
        title = require_text(item.get("title"), "measure.title", 200)
        detail = ""
        if item.get("detail") is not None:
            detail = require_text(item.get("detail"), "measure.detail", 2000)
        self.repository.add_measure(accident_id, title, detail, actor)

    def add_evidence(self, accident_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_open(accident_id)
        kind = require_text(payload.get("kind"), "kind", 60)
        title = require_text(payload.get("title"), "title", 200)
        detail = ""
        if payload.get("detail") is not None:
            detail = require_text(payload.get("detail"), "detail", 2000)
        evidence = self.repository.add_evidence(
            accident_id, kind, title, detail, actor)
        self.repository.append_audit(
            "accident.evidence_added", "accident", accident_id, actor,
            {"evidence_id": evidence["id"], "kind": kind, "title": title})
        return evidence

    def add_measure(self, accident_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_open(accident_id)
        title = require_text(payload.get("title"), "title", 200)
        detail = ""
        if payload.get("detail") is not None:
            detail = require_text(payload.get("detail"), "detail", 2000)
        measure = self.repository.add_measure(
            accident_id, title, detail, actor)
        self.repository.append_audit(
            "accident.measure_added", "accident", accident_id, actor,
            {"measure_id": measure["id"], "title": title})
        return measure

    def _require_open(self, accident_id: int) -> Dict[str, Any]:
        case = self.repository.get_accident(accident_id)
        if case["status"] != "open":
            raise MergeStateError("事故单不是未结案状态，不能再登记材料")
        return case

    # ---------- 补充/修正新材料（冲突后解除冲突） ----------
    def amend_conclusion(self, accident_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """两边结论冲突后，按新材料修正某一单的结论；冲突随之解除，可再指定主单。"""
        ensure_role(role, AMEND_ROLES)
        actor = require_text(actor, "actor", 100)
        self._require_open(accident_id)
        verdict = normalize_verdict(payload.get("verdict"), "pending")
        conclusion = payload.get("conclusion")
        if conclusion is not None:
            conclusion = require_text(conclusion, "conclusion", 2000)
        reason = require_text(payload.get("reason"), "reason", 500)
        updated = self.repository.update_conclusion(
            accident_id, verdict, conclusion, actor)
        group = self.repository.group_of_open_accident(accident_id)
        section = None
        if group is not None and group["status"] == GROUP_CONFLICT:
            members = [self.repository.get_accident(i)
                       for i in self.repository.list_member_ids(group["id"])]
            if not detect_conclusion_conflicts(members):
                self.repository.clear_conflict(group["id"])
                group = self.repository.get_group(group["id"])
        if group is not None:
            section = self._group_payload(group)
        self.repository.append_audit(
            "accident.conclusion_amended", "accident", accident_id, actor, {
                "verdict": verdict, "reason": reason,
                "group_id": group["id"] if group else None,
                "group_status": group["status"] if group else None})
        result = self._detail(updated)
        result["merge"] = section
        return result

    # ---------- 指定主单并执行合并 ----------
    def designate_master(self, group_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        """安全科指定主单：先查结论冲突，冲突则停止并写明；
        否则证据与措施转主单，严重度/期限/责任按新材料重算。"""
        ensure_role(role, MERGE_ROLES)
        actor = require_text(actor, "actor", 100)
        group = self.repository.get_group(group_id)
        if group["status"] not in GROUP_ASSIGNABLE:
            raise MergeStateError(
                f"合并组当前状态{group['status']}不能指定主单"
                + ("（如需更正请先撤销）" if group["status"] == GROUP_MERGED else ""))
        master_id = payload.get("master_case_id")
        if not isinstance(master_id, int) or master_id <= 0:
            raise ValidationError("master_case_id必须是正整数")
        member_ids = self.repository.list_member_ids(group_id)
        if master_id not in member_ids:
            raise ValidationError("主单必须是合并组内的事故单")

        members = [self.repository.get_accident(i) for i in member_ids]
        conflicts = detect_conclusion_conflicts(members)
        if conflicts:
            self.repository.mark_conflict(group_id, master_id, conflicts)
            group = self.repository.get_group(group_id)
            self.repository.append_audit(
                "merge.conflicted", "merge_group", group_id, actor, {
                    "master_case_id": master_id, "conflicts": conflicts})
            raise MergeConflictDetected({
                "message": "两边结论冲突，合并停止；请补充新材料修正后重试",
                "group": self._group_payload(group), "conflicts": conflicts})

        new_values = self._recompute(members, master_id)
        self.repository.apply_merge(group_id, master_id, new_values, actor)
        group = self.repository.get_group(group_id)
        master = self.repository.get_accident(master_id)
        self.repository.append_audit(
            "merge.completed", "merge_group", group_id, actor, {
                "group_no": group["group_no"], "master_case_id": master_id,
                "master_case_no": master["case_no"], "members": member_ids,
                "new_severity": new_values["severity"],
                "new_deadline_due": new_values["deadline_due"],
                "new_responsibilities": new_values["responsibilities"]})
        return self._group_payload(group)

    @staticmethod
    def _recompute(members: List[Dict[str, Any]],
                   master_id: int) -> Dict[str, Any]:
        from datetime import datetime
        severity = recompute_severity(members)
        merged_at = datetime.fromisoformat(utc_now())
        due = recompute_deadline(severity, merged_at)
        return {
            "severity": severity,
            "deadline_due": due.isoformat(),
            "responsibilities": union_responsibilities(members, master_id),
        }

    # ---------- 撤销合并 ----------
    def revoke_merge(self, group_id: int, payload: Optional[Dict[str, Any]],
                     actor: str, role: str) -> Dict[str, Any]:
        """合并错了能撤销：各原件按快照恢复，转入材料退回删除，组置revoked；
        曾用编号在主单上保留。"""
        ensure_role(role, MERGE_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = ""
        if payload:
            reason = require_text(payload.get("reason"), "reason", 500)
        group = self.repository.revoke_merge(group_id, actor)
        member_ids = self.repository.list_member_ids(group_id)
        self.repository.append_audit(
            "merge.revoked", "merge_group", group_id, actor, {
                "group_no": group["group_no"], "members": member_ids,
                "reason": reason})
        return self._group_payload(group)

    # ---------- 查询 ----------
    def get_accident(self, accident_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self._detail(self.repository.get_accident(accident_id))

    def list_accidents(self, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return {"accidents": [self._detail(a)
                              for a in self.repository.list_accidents()]}

    def get_group(self, group_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self._group_payload(self.repository.get_group(group_id))

    def list_groups(self, role: str,
                    status: Optional[str] = None) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return {"groups": [self._group_payload(g)
                           for g in self.repository.list_groups(status)]}

    # ---------- 组装 ----------
    def _detail(self, case: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(case)
        result["evidence"] = self.repository.list_evidence(case["id"])
        result["measures"] = self.repository.list_measures(case["id"])
        result["aliases"] = self.repository.list_aliases(case["id"])
        group = self.repository.group_of_open_accident(case["id"])
        result["merge"] = self._merge_section(case["id"], group)
        return result

    def _merge_section(self, accident_id: int,
                       group: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if group is None:
            return None
        payload = self._group_payload(group)
        payload["role_in_group"] = self._role_in_group(group["id"], accident_id)
        return payload

    def _role_in_group(self, group_id: int, accident_id: int) -> str:
        if self.repository.get_group(group_id).get("master_id") == accident_id:
            return "master"
        return "member"

    def _group_payload(self, group: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(group)
        member_ids = self.repository.list_member_ids(group["id"])
        members = []
        for cid in member_ids:
            case = self.repository.get_accident(cid)
            members.append({
                "case_id": cid, "case_no": case["case_no"],
                "scene_no": case["scene_no"],
                "victim_name": case["victim_name"],
                "source": case["source"], "severity": case["severity"],
                "verdict": case["verdict"], "status": case["status"],
                "role": "master" if group.get("master_id") == cid else "member",
            })
        payload["members"] = members
        payload["master_case_id"] = group.get("master_id")
        return payload
