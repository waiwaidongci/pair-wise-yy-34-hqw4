from __future__ import annotations

from datetime import datetime, timezone

from .domain import ConflictError, ValidationError


TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; MERGE_ENTITY='合并请求'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); REPORT_ROLES=set(['reporter', 'investigator', 'safety_manager']); MERGE_ROLES=set(['safety_manager']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed']); OPEN_STATUSES=set(STATES[:-1])
DEFINITIVE_CONCLUSIONS={'work_related','not_work_related'}; CONFLICTING_CONCLUSIONS=frozenset([('work_related','not_work_related'),('not_work_related','work_related')]); RESPONSIBILITY_RANK={'safety':3,'investigator':2,'team':1}


def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))


def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))


def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)


def can_transition(current,target): return target in TRANSITIONS.get(current,[])


def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")


def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []


def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))


def _time(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def same_accident(left,right):
    """命中条件：同现场编号、同伤者、事发时段重叠，且对方仍未结案。"""
    if left['site_ref'] != right['site_ref'] or left['injured_person'] != right['injured_person']:
        return False
    if right.get('status') not in OPEN_STATUSES:
        return False
    left_start=_time(left['incident_start'])
    left_end=_time(left['incident_end'])
    right_start=_time(right['incident_start'])
    right_end=_time(right['incident_end'])
    return left_start <= right_end and right_start <= left_end


def recalculate_material(members):
    """严重度取最高；伤害指数取最高；阈值取最低，再由规则动态重算期限和优先级。"""
    if not members:
        raise ValidationError("没有可合并的事故材料")
    severity=max((m['severity'] for m in members), key=SEVERITIES.index)
    quantity=max(float(m.get('quantity') or 0.0) for m in members)
    threshold=min(float(m.get('threshold') or 1.0) for m in members)
    if threshold <= 0:
        threshold=1.0
    return {'severity':severity,'quantity':quantity,'threshold':threshold}


def conclusion_conflict(members):
    definitive={m.get('conclusion_code') for m in members if m.get('conclusion_code') in DEFINITIVE_CONCLUSIONS}
    if 'work_related' in definitive and 'not_work_related' in definitive:
        items=[
            {'item_id':m['item_id'],'source':m.get('report_source'),
             'conclusion_code':m.get('conclusion_code'),
             'responsibility':m.get('responsibility')}
            for m in members if m.get('conclusion_code') in DEFINITIVE_CONCLUSIONS
        ]
        return "事故定性冲突：同时存在工伤与非工伤结论", items
    return None, []


def recalculate_responsibility(members):
    candidates=[m for m in members if m.get('responsibility')]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda m:(RESPONSIBILITY_RANK.get(m.get('report_source'),0), m.get('item_id',0)),
        reverse=True,
    )[0]['responsibility']
