"""重复事故合并流程的领域模型：数据结构、状态与基础校验。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .domain import DomainError, ValidationError, require_text


class MergeConflictDetected(DomainError):
    """结论冲突，合并被规则层中止（HTTP 409）。"""
    kind = "conflict"


class MergeStateError(DomainError):
    """合并组或事故当前状态不允许该操作（HTTP 409）。"""
    kind = "conflict"


# 补报来源：班组自报 / 安全科补报
SOURCES = ["crew", "safety"]
SOURCE_LABELS = {"crew": "班组", "safety": "安全科"}

# 事故单状态：未结案 / 已关闭 / 已并入主单
CASE_OPEN = "open"
CASE_CLOSED = "closed"
CASE_MERGED = "merged"
CASE_STATES = [CASE_OPEN, CASE_CLOSED, CASE_MERGED]

# 合并组状态：待合并 / 冲突中止 / 已合并 / 已撤销
GROUP_PENDING = "pending"
GROUP_CONFLICT = "conflict"
GROUP_MERGED = "merged"
GROUP_REVOKED = "revoked"
GROUP_STATES = [GROUP_PENDING, GROUP_CONFLICT, GROUP_MERGED, GROUP_REVOKED]
# 只有这两种状态允许指定主单（冲突后补新材料可重试）
GROUP_ASSIGNABLE = [GROUP_PENDING, GROUP_CONFLICT]

# 结论判定：有责 / 无责 / 待定
VERDICTS = ["liable", "not_liable", "pending"]

SEVERITIES = ["minor", "moderate", "serious", "fatal"]


@dataclass(frozen=True)
class Accident:
    id: int
    case_no: str               # 本单正式编号（系统发号）
    scene_no: Optional[str]    # 现场编号（补报时登记，可能两边各写一个）
    source: str                # crew / safety
    victim_name: str
    victim_id: Optional[str]
    occurred_start: str
    occurred_end: str
    location: Optional[str]
    severity: str
    responsibilities: List[str]
    verdict: str
    conclusion: Optional[str]
    deadline_due: Optional[str]
    status: str
    merged_into: Optional[int]
    version: int
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Evidence:
    id: int
    accident_id: int
    kind: str
    title: str
    detail: str
    origin_accident_id: Optional[int]   # 合并转入时的原单；撤销时按此退回
    transferred: bool
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Measure:
    id: int
    accident_id: int
    title: str
    detail: str
    status: str                          # open / closed
    origin_accident_id: Optional[int]
    transferred: bool
    created_by: str
    created_at: str


@dataclass(frozen=True)
class MergeGroup:
    id: int
    group_no: str
    status: str
    master_id: Optional[int]
    conflict_reasons: List[Dict[str, Any]]
    created_at: str
    updated_at: str


def require_scene_no(value) -> Optional[str]:
    if value is None:
        return None
    value = require_text(value, "scene_no", 80)
    return value.upper()


def require_case_window(payload: Dict[str, Any]) -> tuple:
    start = require_text(payload.get("occurred_start"), "occurred_start", 40)
    end = require_text(payload.get("occurred_end"), "occurred_end", 40)
    if end < start:
        raise ValidationError("事发时段结束时间不能早于开始时间")
    return start, end


def normalize_severity(value, default: str = "minor") -> str:
    if value is None:
        return default
    if value not in SEVERITIES:
        raise ValidationError("severity不在允许范围内")
    return value


def normalize_source(value) -> str:
    if value not in SOURCES:
        raise ValidationError("source必须是crew或safety")
    return value


def normalize_verdict(value, default: str = "pending") -> str:
    if value is None:
        return default
    if value not in VERDICTS:
        raise ValidationError("verdict必须是liable/not_liable/pending")
    return value


def normalize_str_list(value, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"{field}必须是字符串数组")
    result: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{field}不能含空项")
        item = item.strip()
        if len(item) > 100:
            raise ValidationError(f"{field}单项不能超过100字")
        if item not in result:
            result.append(item)
    return result
