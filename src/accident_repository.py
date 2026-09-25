"""重复事故合并的存储层：SQLite建表、事务、合并快照与曾用编号。

只负责持久化，不包含任何合并规则；所有跨表变更在同一事务内完成。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .accident_domain import (CASE_MERGED, CASE_OPEN, GROUP_CONFLICT,
                              GROUP_MERGED, GROUP_PENDING, GROUP_REVOKED,
                              MergeStateError, ValidationError)
from .accident_rules import CASE_NO_PREFIX, GROUP_NO_PREFIX
from .audit import utc_now
from .domain import ConflictError, NotFoundError


class AccidentRepository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS accidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_no TEXT NOT NULL UNIQUE,
                    scene_no TEXT,
                    source TEXT NOT NULL,
                    victim_name TEXT NOT NULL,
                    victim_id TEXT,
                    occurred_start TEXT NOT NULL,
                    occurred_end TEXT NOT NULL,
                    location TEXT,
                    severity TEXT NOT NULL,
                    responsibilities TEXT NOT NULL DEFAULT '[]',
                    verdict TEXT NOT NULL DEFAULT 'pending',
                    conclusion TEXT,
                    deadline_due TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    merged_into INTEGER REFERENCES accidents(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_accidents_status ON accidents(status);
                CREATE TABLE IF NOT EXISTS accident_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accident_id INTEGER NOT NULL REFERENCES accidents(id),
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    origin_accident_id INTEGER REFERENCES accidents(id),
                    transferred INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accident_measures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accident_id INTEGER NOT NULL REFERENCES accidents(id),
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    origin_accident_id INTEGER REFERENCES accidents(id),
                    transferred INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS merge_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_no TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    master_id INTEGER REFERENCES accidents(id),
                    conflict_reasons TEXT NOT NULL DEFAULT '[]',
                    merged_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS merge_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL REFERENCES merge_groups(id),
                    accident_id INTEGER NOT NULL REFERENCES accidents(id),
                    role TEXT NOT NULL CHECK(role IN ('master','member')),
                    joined_at TEXT NOT NULL,
                    UNIQUE(group_id, accident_id)
                );
                CREATE TABLE IF NOT EXISTS merge_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL REFERENCES merge_groups(id),
                    accident_id INTEGER NOT NULL REFERENCES accidents(id),
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    accident_id INTEGER NOT NULL REFERENCES accidents(id),
                    alias_no TEXT NOT NULL,
                    alias_type TEXT NOT NULL CHECK(alias_type IN ('case_no','scene_no')),
                    source_accident_id INTEGER REFERENCES accidents(id),
                    group_id INTEGER REFERENCES merge_groups(id),
                    note TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(accident_id, alias_no)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    # ---------- 基础映射 ----------
    @staticmethod
    def _accident(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["responsibilities"] = json.loads(data["responsibilities"] or "[]")
        return data

    @staticmethod
    def _group(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["conflict_reasons"] = json.loads(data["conflict_reasons"] or "[]")
        return data

    # ---------- 发号 ----------
    def _next_case_no(self) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM accidents").fetchone()
            return f"{CASE_NO_PREFIX}{int(row['n']) + 1:06d}"

    def _next_group_no(self) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM merge_groups").fetchone()
            return f"{GROUP_NO_PREFIX}{int(row['n']) + 1:06d}"

    # ---------- 事故单 ----------
    def create_accident(self, data: Dict[str, Any], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                """INSERT INTO accidents(case_no, scene_no, source, victim_name,
                   victim_id, occurred_start, occurred_end, location, severity,
                   responsibilities, verdict, conclusion, deadline_due, status,
                   merged_into, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self._next_case_no(), data.get("scene_no"), data["source"],
                 data["victim_name"], data.get("victim_id"),
                 data["occurred_start"], data["occurred_end"], data.get("location"),
                 data["severity"], json.dumps(data.get("responsibilities", []),
                                              ensure_ascii=False),
                 data.get("verdict", "pending"), data.get("conclusion"),
                 data.get("deadline_due"), CASE_OPEN, None, 1, actor, now, now),
            )
            accident_id = int(row.lastrowid)
        return self.get_accident(accident_id)

    def get_accident(self, accident_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM accidents WHERE id=?", (accident_id,)).fetchone()
        if row is None:
            raise NotFoundError("事故单不存在")
        return self._accident(row)

    def get_accident_by_case_no(self, case_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM accidents WHERE case_no=?", (case_no,)).fetchone()
        if row is None:
            raise NotFoundError(f"事故单{case_no}不存在")
        return self._accident(row)

    def list_open_accidents(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM accidents WHERE status='open' ORDER BY id").fetchall()
        return [self._accident(r) for r in rows]

    def list_accidents(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM accidents ORDER BY id").fetchall()
        return [self._accident(r) for r in rows]

    def add_evidence(self, accident_id: int, kind: str, title: str,
                     detail: str, actor: str) -> Dict[str, Any]:
        self.get_accident(accident_id)
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO accident_evidence(accident_id, kind, title, detail,
                   origin_accident_id, transferred, created_by, created_at)
                   VALUES(?,?,?,?,?,0,?,?)""",
                (accident_id, kind, title, detail, None, actor, now))
            evidence_id = int(cur.lastrowid)
        return self.get_evidence(evidence_id)

    def get_evidence(self, evidence_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM accident_evidence WHERE id=?",
                (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError("证据不存在")
        return dict(row)

    def list_evidence(self, accident_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM accident_evidence WHERE accident_id=? ORDER BY id",
                (accident_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_measure(self, accident_id: int, title: str, detail: str,
                    actor: str) -> Dict[str, Any]:
        self.get_accident(accident_id)
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO accident_measures(accident_id, title, detail, status,
                   origin_accident_id, transferred, created_by, created_at)
                   VALUES(?,?,?,'open',NULL,0,?,?)""",
                (accident_id, title, detail, actor, now))
            measure_id = int(cur.lastrowid)
        return self.get_measure(measure_id)

    def get_measure(self, measure_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM accident_measures WHERE id=?",
                (measure_id,)).fetchone()
        if row is None:
            raise NotFoundError("措施不存在")
        return dict(row)

    def list_measures(self, accident_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM accident_measures WHERE accident_id=? ORDER BY id",
                (accident_id,)).fetchall()
        return [dict(r) for r in rows]

    def update_conclusion(self, accident_id: int, verdict: str,
                          conclusion: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE accidents SET verdict=?, conclusion=?, version=version+1,
                   updated_at=? WHERE id=? AND status='open'""",
                (verdict, conclusion, now, accident_id))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM accidents WHERE id=?", (accident_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("事故单不存在")
                raise MergeStateError("只有未结案事故可以补充/修正结论材料")
        return self.get_accident(accident_id)

    # ---------- 合并组 ----------
    def create_group(self) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO merge_groups(group_no, status, master_id,
                   conflict_reasons, merged_at, revoked_at, created_at, updated_at)
                   VALUES(?, 'pending', NULL, '[]', NULL, NULL, ?, ?)""",
                (self._next_group_no(), now, now))
            group_id = int(cur.lastrowid)
        return self.get_group(group_id)

    def get_group(self, group_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM merge_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            raise NotFoundError("合并组不存在")
        return self._group(row)

    def list_groups(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM merge_groups"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._group(r) for r in rows]

    def group_of_open_accident(self, accident_id: int) -> Optional[Dict[str, Any]]:
        """事故当前所在的、尚未终结（pending/conflict）的合并组。"""
        with self._lock:
            row = self.conn.execute(
                """SELECT g.* FROM merge_groups g
                   JOIN merge_members m ON m.group_id=g.id
                   WHERE m.accident_id=? AND g.status IN (?,?)
                   ORDER BY g.id LIMIT 1""",
                (accident_id, GROUP_PENDING, GROUP_CONFLICT)).fetchone()
        return self._group(row) if row else None

    def add_member(self, group_id: int, accident_id: int, role: str,
                   joined_at: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO merge_members(group_id, accident_id,
                   role, joined_at) VALUES(?,?,?,?)""",
                (group_id, accident_id, role, joined_at))

    def merge_groups_into(self, target_id: int, source_ids: List[int],
                          joined_at: str) -> None:
        """一份补报同时命中多个待合并组时，把它们并入最早的组；空组删除。"""
        if not source_ids:
            return
        with self._lock, self.conn:
            for sid in source_ids:
                self.conn.execute(
                    """INSERT OR IGNORE INTO merge_members(group_id, accident_id,
                       role, joined_at)
                       SELECT ?, accident_id, role, ? FROM merge_members
                       WHERE group_id=?""",
                    (target_id, joined_at, sid))
                self.conn.execute(
                    "DELETE FROM merge_members WHERE group_id=?", (sid,))
                self.conn.execute(
                    "DELETE FROM merge_groups WHERE id=? AND status=?",
                    (sid, GROUP_PENDING))
            self.conn.execute(
                "UPDATE merge_groups SET updated_at=? WHERE id=?",
                (joined_at, target_id))

    def list_member_ids(self, group_id: int) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT accident_id FROM merge_members WHERE group_id=? ORDER BY id",
                (group_id,)).fetchall()
        return [int(r["accident_id"]) for r in rows]

    def mark_conflict(self, group_id: int, master_id: int,
                      reasons: List[Dict[str, Any]]) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE merge_groups SET status=?, master_id=?,
                   conflict_reasons=?, updated_at=? WHERE id=?""",
                (GROUP_CONFLICT, master_id,
                 json.dumps(reasons, ensure_ascii=False), now, group_id))

    def clear_conflict(self, group_id: int) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE merge_groups SET status=?, conflict_reasons='[]',
                   updated_at=? WHERE id=? AND status=?""",
                (GROUP_PENDING, now, group_id, GROUP_CONFLICT))

    def apply_merge(self, group_id: int, master_id: int,
                    new_values: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """原子执行合并：快照原件 -> 证据/措施转主单 -> 非主单置merged -> 组置merged。"""
        now = utc_now()
        member_ids = self.list_member_ids(group_id)
        if master_id not in member_ids:
            raise ValidationError("主单必须是合并组成员")
        with self._lock, self.conn:
            # 1. 快照各成员原件（用于撤销恢复）
            for cid in member_ids:
                case = self.get_accident(cid)
                self.conn.execute(
                    """INSERT INTO merge_snapshots(group_id, accident_id, snapshot)
                       VALUES(?,?,?)""",
                    (group_id, cid,
                     json.dumps(case, ensure_ascii=False, default=str)))

            # 2. 证据与措施转到主单（同kind+标题去重；保留来源单，撤销时退回）
            existing_ev = {
                (r["kind"], r["title"].strip().lower())
                for r in self.conn.execute(
                    "SELECT kind,title FROM accident_evidence WHERE accident_id=?",
                    (master_id,)).fetchall()
            }
            existing_mv = {
                r["title"].strip().lower()
                for r in self.conn.execute(
                    "SELECT title FROM accident_measures WHERE accident_id=?",
                    (master_id,)).fetchall()
            }
            transferred = {"evidence": [], "measures": []}
            for cid in member_ids:
                if cid == master_id:
                    continue
                for r in self.conn.execute(
                        "SELECT * FROM accident_evidence WHERE accident_id=?",
                        (cid,)).fetchall():
                    key = (r["kind"], r["title"].strip().lower())
                    if key in existing_ev:
                        continue
                    existing_ev.add(key)
                    self.conn.execute(
                        """INSERT INTO accident_evidence(accident_id, kind, title,
                           detail, origin_accident_id, transferred, created_by,
                           created_at) VALUES(?,?,?,?,?,1,?,?)""",
                        (master_id, r["kind"], r["title"], r["detail"], cid,
                         actor, now))
                    transferred["evidence"].append(
                        {"from_case": cid, "title": r["title"]})
                for r in self.conn.execute(
                        "SELECT * FROM accident_measures WHERE accident_id=?",
                        (cid,)).fetchall():
                    key = r["title"].strip().lower()
                    if key in existing_mv:
                        continue
                    existing_mv.add(key)
                    self.conn.execute(
                        """INSERT INTO accident_measures(accident_id, title, detail,
                           status, origin_accident_id, transferred, created_by,
                           created_at) VALUES(?,?,?,?,?,1,?,?)""",
                        (master_id, r["title"], r["detail"], r["status"], cid,
                         actor, now))
                    transferred["measures"].append(
                        {"from_case": cid, "title": r["title"]})

            # 3. 主单按新材料重算（版本+1，保持open继续处置）
            self.conn.execute(
                """UPDATE accidents SET severity=?, responsibilities=?,
                   deadline_due=?, version=version+1, updated_at=? WHERE id=?""",
                (new_values["severity"],
                 json.dumps(new_values["responsibilities"], ensure_ascii=False),
                 new_values["deadline_due"], now, master_id))

            # 4. 非主单标记并入主单
            self.conn.execute(
                "UPDATE accidents SET status=?, merged_into=?, updated_at=? "
                "WHERE id != ? AND id IN (%s)" % ",".join("?" * len(member_ids)),
                [CASE_MERGED, master_id, now, master_id, *member_ids])

            # 5. 登记曾用编号（各单自身的现场编号与非主单编号），撤销后仍保留
            master = self.get_accident(master_id)
            for cid in member_ids:
                case = self.get_accident(cid)
                aliases = []
                if case.get("scene_no") and case["scene_no"] != master.get("scene_no"):
                    aliases.append((case["scene_no"], "scene_no"))
                if cid != master_id:
                    aliases.append((case["case_no"], "case_no"))
                for alias_no, alias_type in aliases:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO case_aliases(accident_id, alias_no,
                           alias_type, source_accident_id, group_id, note, created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (master_id, alias_no, alias_type, cid, group_id,
                         "合并保留的曾用编号", now))

            # 6. 合并组置为已合并
            self.conn.execute(
                """UPDATE merge_groups SET status=?, master_id=?,
                   conflict_reasons='[]', merged_at=?, updated_at=? WHERE id=?""",
                (GROUP_MERGED, master_id, now, now, group_id))
        return self.get_group(group_id)

    def list_aliases(self, accident_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM case_aliases WHERE accident_id=? ORDER BY id",
                (accident_id,)).fetchall()
        return [dict(r) for r in rows]

    def revoke_merge(self, group_id: int, actor: str) -> Dict[str, Any]:
        """撤销合并：原件各自恢复、转入材料退回删除、组置revoked；曾用编号保留。"""
        group = self.get_group(group_id)
        if group["status"] != GROUP_MERGED:
            raise MergeStateError("只有已完成的合并可以撤销")
        now = utc_now()
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT accident_id, snapshot FROM merge_snapshots WHERE group_id=?",
                (group_id,)).fetchall()
            for row in rows:
                snap = json.loads(row["snapshot"])
                self.conn.execute(
                    """UPDATE accidents SET case_no=?, scene_no=?, source=?,
                       victim_name=?, victim_id=?, occurred_start=?, occurred_end=?,
                       location=?, severity=?, responsibilities=?, verdict=?,
                       conclusion=?, deadline_due=?, status=?, merged_into=NULL,
                       version=?, updated_at=? WHERE id=?""",
                    (snap["case_no"], snap["scene_no"], snap["source"],
                     snap["victim_name"], snap["victim_id"], snap["occurred_start"],
                     snap["occurred_end"], snap["location"], snap["severity"],
                     json.dumps(snap["responsibilities"], ensure_ascii=False),
                     snap["verdict"], snap["conclusion"], snap["deadline_due"],
                     snap["status"], snap["version"], snap["updated_at"],
                     snap["id"]))
            # 删除本次合并转入主单的证据与措施（原件本就保留在各自单上）
            self.conn.execute(
                "DELETE FROM accident_evidence WHERE transferred=1 AND "
                "origin_accident_id IS NOT NULL AND accident_id=?",
                (group["master_id"],))
            self.conn.execute(
                "DELETE FROM accident_measures WHERE transferred=1 AND "
                "origin_accident_id IS NOT NULL AND accident_id=?",
                (group["master_id"],))
            self.conn.execute(
                """UPDATE merge_groups SET status=?, revoked_at=?, updated_at=?
                   WHERE id=?""",
                (GROUP_REVOKED, now, now, group_id))
        return self.get_group(group_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            from .audit import make_entry
            event = make_entry(action, entity_type, entity_id, actor, detail,
                               previous)
            self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor,
                   detail, previous_hash, entry_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"],
                 event["actor"], json.dumps(event["detail"], ensure_ascii=False,
                                            sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]))
        return event

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
