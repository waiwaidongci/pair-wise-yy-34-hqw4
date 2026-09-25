from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from .domain import (CONCLUSION_CODES, REPORT_SOURCES, ConflictError, ensure_role,
                     normalize_injured_person, normalize_severity, normalize_site_ref,
                     require_choice, require_number, require_text, require_time_window)
from .repository import Repository
from .rules import (AUDIT_ROLES, ENTITY,
                    MERGE_ENTITY, MERGE_ROLES, OPEN_STATUSES, RECORD_ROLES,
                    REPORT_ROLES, VIEW_ROLES,
                    completion_blockers, conclusion_conflict, escalation_required,
                    priority_score, recalculate_material,
                    recalculate_responsibility, response_deadline_hours,
                    role_for_transition, same_accident, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository
        self._merge_lock = threading.RLock()

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        report = self._report_payload(payload)
        new_report = {
            'site_ref': report['site_ref'], 'injured_person': report['injured_person'],
            'incident_start': report['incident_start'], 'incident_end': report['incident_end'],
            'status': 'reported',
        }

        with self._merge_lock:
            candidates = [
                candidate for candidate in self.repository.find_duplicate_reports(
                    report['site_ref'], report['injured_person'])
                if same_accident(new_report, candidate)
            ]
            groups = self.repository.active_pending_groups([c['item_id'] for c in candidates])
            active_groups = set(groups.values())
            if len(active_groups) > 1:
                raise ConflictError("同类事故已分属不同待合并组，请安全科先处理")

            item = self.repository.create_accident_item(
                title, description, severity, quantity, threshold, external_ref, actor, report)
            member_ids = [candidate['item_id'] for candidate in candidates]
            if active_groups:
                request_id = next(iter(active_groups))
                self.repository.add_merge_members(request_id, [item['id']] + member_ids)
                action = "supplement_pending_merge"
            elif member_ids:
                request = self.repository.create_merge_request(member_ids + [item['id']], actor)
                request_id = request['id']
                action = "merge_pending"
            else:
                request_id = None
                action = "supplement_create"

        detail = {
            "title": title, "severity": severity, "quantity": quantity,
            "site_ref": report['site_ref'], "injured_person": report['injured_person'],
            "incident_start": report['incident_start'], "incident_end": report['incident_end'],
            "priority": priority_score(severity, quantity, threshold),
        }
        if request_id is not None:
            detail["merge_request_id"] = request_id
        self.repository.append_audit(action, ENTITY, item["id"], actor, detail)
        result = self.enrich(self.repository.get_item(item['id']))
        if request_id is not None:
            result['merge_request_id'] = request_id
            result['merge_status'] = 'pending'
        return result

    @staticmethod
    def _report_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        report_source = require_choice(payload.get('report_source', 'team'),
                                       'report_source', REPORT_SOURCES)
        conclusion_code = payload.get('conclusion_code')
        if conclusion_code is not None:
            conclusion_code = require_choice(conclusion_code, 'conclusion_code',
                                             CONCLUSION_CODES)
        conclusion_note = payload.get('conclusion_note')
        if conclusion_note is not None:
            conclusion_note = require_text(conclusion_note, 'conclusion_note')
        responsibility = payload.get('responsibility')
        if responsibility is not None:
            responsibility = require_text(responsibility, 'responsibility', 200)
        start, end = require_time_window(payload.get('incident_start'),
                                         payload.get('incident_end'))
        return {
            'site_ref': normalize_site_ref(payload.get('site_ref')),
            'injured_person': normalize_injured_person(payload.get('injured_person')),
            'incident_start': start,
            'incident_end': end,
            'report_source': report_source,
            'responsibility': responsibility,
            'conclusion_code': conclusion_code,
            'conclusion_note': conclusion_note,
        }

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            from .domain import ValidationError
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.get_item(item_id)
        if item['status'] == 'merged':
            raise ConflictError("事故已合并，证据和措施请登记到主单")
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item['status'] == 'merged':
            raise ConflictError("事故已合并，请到主单继续流程")
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def merge_request(self, request_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, MERGE_ROLES)
        actor = require_text(actor, 'actor', 100)
        master_item_id = payload.get('master_item_id')
        if not isinstance(master_item_id, int) or master_item_id < 1:
            from .domain import ValidationError
            raise ValidationError('master_item_id必须是正整数')
        expected_version = payload.get('expected_version')
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError('expected_version必须是正整数')

        with self._merge_lock:
            request = self.repository.get_merge_request(request_id)
            if request['status'] != 'pending':
                raise ConflictError("只能合并待处理的合并请求")
            member_ids = [member['item_id'] for member in request['members']]
            if master_item_id not in member_ids:
                from .domain import ValidationError
                raise ValidationError('主单必须来自本次同类事故')
            members = request['members']
            for member in members:
                if member['status'] not in OPEN_STATUSES:
                    raise ConflictError(f"事故{member['item_id']}已结案，不能合并")
            reason, conflicts = conclusion_conflict(members)
            if reason:
                self.repository.mark_merge_conflict(request_id, reason, conflicts)
                self.repository.append_audit(
                    'merge_conflict', MERGE_ENTITY, request_id, actor,
                    {'reason': reason, 'conflicts': conflicts})
                raise ConflictError(reason)

            material = recalculate_material(members)
            responsibility = recalculate_responsibility(members)
            conclusion_code, conclusion_note = self._merged_conclusion(members)
            snapshot = self._build_snapshot(member_ids, master_item_id)
            self.repository.commit_merge(
                request_id, master_item_id, expected_version, material, responsibility,
                conclusion_code, conclusion_note, snapshot, actor)
            merged = self.repository.get_merge_request(request_id)
            self.repository.append_audit('merge', MERGE_ENTITY, request_id, actor, {
                'master_item_id': master_item_id,
                'source_item_ids': [i for i in member_ids if i != master_item_id],
                'material': material,
                'responsibility': responsibility,
                'retained_refs': {
                    str(item_id): self.repository.get_item(item_id)['external_ref']
                    for item_id in member_ids
                },
            })
        return merged

    @staticmethod
    def _merged_conclusion(members):
        def rank(member):
            return {'safety': 3, 'investigator': 2, 'team': 1}.get(member.get('report_source'), 0)
        codes = {member.get('conclusion_code') for member in members
                 if member.get('conclusion_code')}
        if 'work_related' in codes:
            selected = 'work_related'
        elif 'not_work_related' in codes:
            selected = 'not_work_related'
        elif 'inconclusive' in codes:
            selected = 'inconclusive'
        else:
            return None, None
        chosen = sorted(
            [m for m in members if m.get('conclusion_code') == selected],
            key=lambda m: (rank(m), m['item_id']), reverse=True,
        )[0]
        return selected, chosen.get('conclusion_note')

    def _build_snapshot(self, member_ids, master_item_id):
        items = {}
        profiles = {}
        records = []
        for item_id in member_ids:
            item = self.repository.get_item(item_id)
            items[str(item_id)] = item
            profiles[str(item_id)] = self.repository.get_accident_report(item_id)
            if item_id != master_item_id:
                for record in self.repository.list_records(item_id):
                    records.append({
                        'id': record['id'],
                        'original_owner_item_id': record['owner_item_id'],
                    })
        return {'member_ids': member_ids, 'items': items, 'profiles': profiles,
                'records': records}

    def undo_merge(self, request_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MERGE_ROLES)
        actor = require_text(actor, 'actor', 100)
        with self._merge_lock:
            request = self.repository.get_merge_request(request_id)
            if request['status'] != 'merged':
                raise ConflictError("只能撤销已完成的合并")
            result = self.repository.undo_merge(request_id, actor)
            snapshot = self.repository.get_merge_request(request_id)['material_snapshot']
            self.repository.append_audit('merge_undo', MERGE_ENTITY, request_id, actor, {
                'master_item_id': request['master_item_id'],
                'restored_item_ids': snapshot['member_ids'],
            })
        return result

    def get_merge_request(self, request_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_merge_request(request_id)

    def list_merge_requests(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_merge_requests(status)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        result = self.enrich(self.repository.get_item(item_id))
        try:
            result['accident_report'] = self.repository.get_accident_report(item_id)
        except Exception:
            pass
        return result

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None,
              entity_type: Optional[str] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id, entity_type)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
