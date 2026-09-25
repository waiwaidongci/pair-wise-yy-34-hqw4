"""重复事故合并的请求入口：JSON路由与统一错误响应。

只承担HTTP协议适配；规则与流程在 accident_service，存储在 accident_repository。
返回 (status, payload) 元组；无法处理的路径返回 None，由宿主继续分派。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .accident_domain import MergeConflictDetected, MergeStateError
from .accident_service import AccidentService
from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)

RouteResult = Optional[Tuple[int, Any]]


def error_response(exc: Exception) -> Tuple[int, Dict[str, Any]]:
    if isinstance(exc, MergeConflictDetected):
        payload = getattr(exc, "message", None)
        if isinstance(payload, dict):
            return 409, payload
        return 409, {"error": "MergeConflictDetected", "message": str(exc)}
    if isinstance(exc, MergeStateError):
        return 409, {"error": "MergeStateError", "message": str(exc)}
    if isinstance(exc, ValidationError):
        return 422, {"error": "ValidationError", "message": str(exc)}
    if isinstance(exc, NotFoundError):
        return 404, {"error": "NotFoundError", "message": str(exc)}
    if isinstance(exc, PermissionDenied):
        return 403, {"error": "PermissionDenied", "message": str(exc)}
    if isinstance(exc, ConflictError):
        return 409, {"error": "ConflictError", "message": str(exc)}
    if isinstance(exc, DomainError):
        return 400, {"error": exc.__class__.__name__, "message": str(exc)}
    return 500, {"error": "InternalError", "message": str(exc)}


class AccidentRouter:
    def __init__(self, service: AccidentService):
        self.service = service

    def handle_get(self, path: str, query: Dict[str, list],
                   actor: str, role: str) -> RouteResult:
        if path == "/api/accidents":
            return 200, self.service.list_accidents(role)
        if path.startswith("/api/accidents/"):
            accident_id = int(path.rsplit("/", 1)[-1])
            return 200, self.service.get_accident(accident_id, role)
        if path == "/api/merge-groups":
            status = query.get("status", [None])[0]
            return 200, self.service.list_groups(role, status)
        if path.startswith("/api/merge-groups/"):
            group_id = int(path.rsplit("/", 1)[-1])
            return 200, self.service.get_group(group_id, role)
        return None

    def handle_post(self, path: str, body: Dict[str, Any],
                    actor: str, role: str) -> RouteResult:
        if path == "/api/accidents":
            return 201, self.service.report_accident(body, actor, role)
        if path.startswith("/api/accidents/") and path.endswith("/evidence"):
            accident_id = int(path.split("/")[3])
            return 201, self.service.add_evidence(accident_id, body, actor, role)
        if path.startswith("/api/accidents/") and path.endswith("/measures"):
            accident_id = int(path.split("/")[3])
            return 201, self.service.add_measure(accident_id, body, actor, role)
        if path.startswith("/api/accidents/") and path.endswith("/amend-conclusion"):
            accident_id = int(path.split("/")[3])
            return 200, self.service.amend_conclusion(
                accident_id, body, actor, role)
        if (path.startswith("/api/merge-groups/")
                and path.endswith("/designate-master")):
            group_id = int(path.split("/")[3])
            return 200, self.service.designate_master(
                group_id, body, actor, role)
        if path.startswith("/api/merge-groups/") and path.endswith("/revoke"):
            group_id = int(path.split("/")[3])
            return 200, self.service.revoke_merge(
                group_id, body, actor, role)
        return None
