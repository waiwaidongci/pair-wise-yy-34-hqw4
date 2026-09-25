from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import STATES


ITEM_STATUSES = STATES + ['merged']
MERGE_STATUSES = ['pending', 'conflicted', 'merged', 'undone']


class Repository:
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
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in ITEM_STATUSES)
        merge_statuses = ",".join("'" + s + "'" for s in MERGE_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    owner_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE INDEX IF NOT EXISTS ix_records_owner ON records(owner_item_id);
                CREATE TABLE IF NOT EXISTS accident_reports (
                    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
                    site_ref TEXT NOT NULL,
                    injured_person TEXT NOT NULL,
                    incident_start TEXT NOT NULL,
                    incident_end TEXT NOT NULL,
                    report_source TEXT NOT NULL
                        CHECK(report_source IN ('team','safety')),
                    responsibility TEXT,
                    conclusion_code TEXT
                        CHECK(conclusion_code IS NULL OR conclusion_code IN
                              ('work_related','not_work_related','inconclusive')),
                    conclusion_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_accident_match
                    ON accident_reports(site_ref, injured_person);
                CREATE TABLE IF NOT EXISTS merge_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL CHECK(status IN ({merge_statuses})),
                    master_item_id INTEGER REFERENCES items(id),
                    reason TEXT,
                    conflict_detail TEXT,
                    material_snapshot TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    merged_at TEXT,
                    undone_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS merge_members (
                    request_id INTEGER NOT NULL REFERENCES merge_requests(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    active INTEGER NOT NULL DEFAULT 1,
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY(request_id, item_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_active_merge_member
                    ON merge_members(item_id) WHERE active=1;
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
            columns = {row['name'] for row in self.conn.execute("PRAGMA table_info(records)")}
            if 'owner_item_id' not in columns:
                self.conn.execute(
                    "ALTER TABLE records ADD COLUMN owner_item_id INTEGER REFERENCES items(id)"
                )
                self.conn.execute("UPDATE records SET owner_item_id=item_id WHERE owner_item_id IS NULL")
                self.conn.execute("CREATE INDEX IF NOT EXISTS ix_records_owner ON records(owner_item_id)")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_accident_item(self, title: str, description: str, severity: str,
                             quantity: float, threshold: float,
                             external_ref: Optional[str], actor: str,
                             report: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
                self.conn.execute(
                    """INSERT INTO accident_reports(item_id, site_ref, injured_person,
                       incident_start, incident_end, report_source, responsibility,
                       conclusion_code, conclusion_note, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, report['site_ref'], report['injured_person'],
                     report['incident_start'], report['incident_end'],
                     report['report_source'], report.get('responsibility'),
                     report.get('conclusion_code'), report.get('conclusion_note'), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("事故不存在")
        return self._item(row)

    def get_accident_report(self, item_id: int) -> Dict[str, Any]:
        self.get_item(item_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM accident_reports WHERE item_id=?", (item_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("事故补报信息不存在")
        return dict(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("事故不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, owner_item_id, kind, detail, status,
                       external_ref, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE owner_item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE owner_item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def find_duplicate_reports(self, site_ref: str, injured_person: str,
                               exclude_item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = """
            SELECT ar.*, i.title, i.description, i.severity, i.quantity, i.threshold,
                   i.status, i.version, i.external_ref
            FROM accident_reports ar JOIN items i ON i.id=ar.item_id
            WHERE ar.site_ref=? AND ar.injured_person=? AND i.status IN
                  ('reported','investigating','corrective_action','verification')
        """
        params: List[Any] = [site_ref, injured_person]
        if exclude_item_id is not None:
            sql += " AND ar.item_id<>?"
            params.append(exclude_item_id)
        sql += " ORDER BY ar.item_id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def active_pending_groups(self, item_ids: List[int]) -> Dict[int, int]:
        if not item_ids:
            return {}
        placeholders = ",".join("?" for _ in item_ids)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT mm.request_id, mm.item_id FROM merge_members mm
                    JOIN merge_requests mr ON mr.id=mm.request_id
                    WHERE mm.active=1 AND mr.status='pending'
                      AND mm.item_id IN ({placeholders})""",
                item_ids,
            ).fetchall()
        return {int(row['item_id']): int(row['request_id']) for row in rows}

    def create_merge_request(self, member_ids: List[int], actor: str) -> Dict[str, Any]:
        now = utc_now()
        snapshot = json.dumps({'member_ids': member_ids}, ensure_ascii=False)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO merge_requests(status, material_snapshot, created_by,
                   created_at, version) VALUES('pending',?,?,?,1)""",
                (snapshot, actor, now),
            )
            request_id = int(cur.lastrowid)
            self.conn.executemany(
                """INSERT INTO merge_members(request_id,item_id,active,joined_at)
                   VALUES(?,?,1,?)""",
                [(request_id, item_id, now) for item_id in member_ids],
            )
        return self.get_merge_request(request_id)

    def add_merge_members(self, request_id: int, member_ids: List[int]) -> None:
        if not member_ids:
            return
        now = utc_now()
        with self._lock, self.conn:
            for item_id in member_ids:
                self.conn.execute(
                    """INSERT INTO merge_members(request_id,item_id,active,joined_at)
                       VALUES(?,?,1,?) ON CONFLICT(request_id,item_id)
                       DO UPDATE SET active=1""",
                    (request_id, item_id, now),
                )
            self.conn.execute(
                "UPDATE merge_requests SET material_snapshot=? WHERE id=?",
                (json.dumps({'member_ids': self._active_member_ids(request_id)},
                            ensure_ascii=False), request_id),
            )

    def _active_member_ids(self, request_id: int) -> List[int]:
        rows = self.conn.execute(
            "SELECT item_id FROM merge_members WHERE request_id=? AND active=1 ORDER BY item_id",
            (request_id,),
        ).fetchall()
        return [int(row['item_id']) for row in rows]

    def _merge_request(self, row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result['material_snapshot'] = json.loads(result['material_snapshot'])
        if result.get('conflict_detail'):
            result['conflict_detail'] = json.loads(result['conflict_detail'])
        members = self.conn.execute(
            """SELECT ar.*, i.title, i.description, i.severity, i.quantity, i.threshold,
                      i.status, i.version, i.external_ref, i.created_at
               FROM merge_members mm
               JOIN accident_reports ar ON ar.item_id=mm.item_id
               JOIN items i ON i.id=mm.item_id
               WHERE mm.request_id=? ORDER BY mm.item_id""",
            (row['id'],),
        ).fetchall()
        result['members'] = [dict(member) for member in members]
        return result

    def get_merge_request(self, request_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM merge_requests WHERE id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("合并请求不存在")
        with self._lock:
            return self._merge_request(row)

    def list_merge_requests(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM merge_requests"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [self._merge_request(row) for row in rows]

    def commit_merge(self, request_id: int, master_item_id: int, expected_version: int,
                     material: Dict[str, Any], responsibility: Optional[str],
                     conclusion_code: Optional[str], conclusion_note: Optional[str],
                     snapshot: Dict[str, Any], actor: str) -> None:
        now = utc_now()
        source_ids = [item_id for item_id in snapshot['member_ids'] if item_id != master_item_id]
        placeholders = ",".join("?" for _ in source_ids) if source_ids else ""
        with self._lock, self.conn:
            request = self.conn.execute(
                "SELECT * FROM merge_requests WHERE id=?", (request_id,)
            ).fetchone()
            if request is None:
                raise NotFoundError("合并请求不存在")
            if request['status'] != 'pending':
                raise ConflictError("合并请求不是待合并状态")
            master_row = self.conn.execute(
                "SELECT version FROM items WHERE id=?", (master_item_id,)
            ).fetchone()
            if master_row is None:
                raise NotFoundError("主单不存在")
            if int(master_row['version']) != expected_version:
                raise ConflictError("主单版本冲突，请刷新后重试")
            self.conn.execute(
                """UPDATE items SET severity=?, quantity=?, threshold=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (material['severity'], material['quantity'], material['threshold'],
                 now, master_item_id),
            )
            if source_ids:
                self.conn.execute(
                    f"UPDATE items SET status='merged', updated_at=? WHERE id IN ({placeholders})",
                    [now] + source_ids,
                )
                self.conn.execute(
                    f"""UPDATE records SET owner_item_id=?
                        WHERE item_id IN ({placeholders}) AND owner_item_id<>?""",
                    [master_item_id] + source_ids + [master_item_id],
                )
            self.conn.execute(
                """UPDATE accident_reports SET responsibility=?, conclusion_code=?,
                   conclusion_note=?, updated_at=? WHERE item_id=?""",
                (responsibility, conclusion_code, conclusion_note, now, master_item_id),
            )
            self.conn.execute(
                """UPDATE merge_requests SET status='merged', master_item_id=?,
                   material_snapshot=?, merged_at=?, version=version+1 WHERE id=?""",
                (master_item_id, json.dumps(snapshot, ensure_ascii=False), now, request_id),
            )
            self.conn.execute(
                "UPDATE merge_members SET active=0 WHERE request_id=?", (request_id,)
            )

    def mark_merge_conflict(self, request_id: int, reason: str,
                            conflicts: List[Dict[str, Any]]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE merge_requests SET status='conflicted', reason=?,
                   conflict_detail=?, version=version+1 WHERE id=?""",
                (reason, json.dumps(conflicts, ensure_ascii=False), request_id),
            )
            self.conn.execute(
                "UPDATE merge_members SET active=0 WHERE request_id=?", (request_id,)
            )

    def undo_merge(self, request_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM merge_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("合并请求不存在")
            if row['status'] != 'merged':
                raise ConflictError("只能撤销已完成的合并")
            snapshot = json.loads(row['material_snapshot'])
            master_id = int(row['master_item_id'])
            master = snapshot['items'][str(master_id)]
            self.conn.execute(
                """UPDATE items SET title=?, description=?, severity=?, quantity=?,
                   threshold=?, status=?, external_ref=?, version=version+1, updated_at=?
                   WHERE id=?""",
                (master['title'], master['description'], master['severity'],
                 master['quantity'], master['threshold'], master['status'],
                 master['external_ref'], now, master_id),
            )
            master_profile = snapshot['profiles'][str(master_id)]
            self.conn.execute(
                """UPDATE accident_reports SET responsibility=?, conclusion_code=?,
                   conclusion_note=?, updated_at=? WHERE item_id=?""",
                (master_profile.get('responsibility'), master_profile.get('conclusion_code'),
                 master_profile.get('conclusion_note'), now, master_id),
            )
            for item_id, source in snapshot['items'].items():
                if int(item_id) == master_id:
                    continue
                self.conn.execute(
                    """UPDATE items SET title=?, description=?, severity=?, quantity=?,
                       threshold=?, status=?, external_ref=?, updated_at=? WHERE id=?""",
                    (source['title'], source['description'], source['severity'],
                     source['quantity'], source['threshold'], source['status'],
                     source['external_ref'], now, int(item_id)),
                )
            for record in snapshot['records']:
                self.conn.execute(
                    "UPDATE records SET owner_item_id=? WHERE id=? AND owner_item_id=?",
                    (record['original_owner_item_id'], record['id'], master_id),
                )
            self.conn.execute(
                """UPDATE merge_requests SET status='undone', undone_at=?,
                   version=version+1 WHERE id=?""",
                (now, request_id),
            )
            self.conn.execute(
                "UPDATE merge_members SET active=0 WHERE request_id=?", (request_id,)
            )
        return self.get_merge_request(request_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: List[Any] = []
        if entity_id is not None:
            sql += " AND entity_id=?"
            params.append(entity_id)
        if entity_type is not None:
            sql += " AND entity_type=?"
            params.append(entity_type)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
