"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ca_announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    corporate_action TEXT NOT NULL,
                    ratio REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ca_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    announcement_id INTEGER NOT NULL REFERENCES ca_announcements(id),
                    request_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    old_ratio REAL,
                    new_ratio REAL NOT NULL,
                    affected INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(announcement_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_adjust_announcement ON ca_adjustments(announcement_id, request_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def register_announcement(self, connection: sqlite3.Connection, reference: str, corporate_action: str, ratio: float, now: str) -> None:
        # 指令入库时顺带登记公告；已存在（可能已有更新比例）时保留现状，不覆盖
        connection.execute(
            "INSERT OR IGNORE INTO ca_announcements(reference,corporate_action,ratio,status,version,updated_by,created_at,updated_at) VALUES(?,?,?,'pending',0,'',?,?)",
            (reference, corporate_action, float(ratio), now, now),
        )

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                announcement_ref = payload.get("announcement_ref")
                if announcement_ref:
                    self.register_announcement(
                        connection,
                        announcement_ref,
                        payload.get("corporate_action", "none"),
                        float(payload.get("action_ratio", 0)),
                        now,
                    )
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def get_announcement(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ca_announcements WHERE reference=?", (reference,)).fetchone()
        return dict(row) if row is not None else None

    def list_announcements(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM ca_announcements ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def records_for_announcement(self, reference: str) -> List[Dict[str, Any]]:
        # 公告编号存于payload JSON中，读出后由规则层判定是否参与重算
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = self._row(row)
            if item["payload"].get("announcement_ref") == reference:
                result.append(item)
        return result

    def adjust_announcement(self, reference: str, expected_version: int, ratio: float, request_id: str, actor_id: str, build_adjustment, is_unsettled) -> Dict[str, Any]:
        """原子地确认公告新比例并重算未交收指令。

        - expected_version与公告当前版本不一致 → 后到的专员收到冲突；
        - 同一(公告, request_id)重试 → 幂等返回首次结果，不重复调整；
        - 已交收/已冲正指令跳过，保留交收时原比例。
        """
        now = _now()
        ratio = round(float(ratio), 6)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            announcement = connection.execute("SELECT * FROM ca_announcements WHERE reference=?", (reference,)).fetchone()
            if announcement is None:
                connection.rollback()
                raise NotFound("公司行动公告不存在")
            announcement_id = int(announcement["id"])

            existing = connection.execute(
                "SELECT * FROM ca_adjustments WHERE announcement_id=? AND request_id=?",
                (announcement_id, request_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                adjustment = dict(existing)
                adjustment["idempotent"] = True
                adjustment["announcement_version"] = int(announcement["version"])
                return adjustment

            if int(announcement["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("公告比例已被他人更新，请刷新后重试")

            old_ratio = float(announcement["ratio"])
            version = int(announcement["version"]) + 1
            affected = 0
            skipped = 0
            for record in self.records_for_announcement(reference):
                if is_unsettled(record):
                    new_payload, changes = build_adjustment(record, ratio)
                    record_version = int(record["version"]) + 1
                    connection.execute(
                        "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                        (record["state"], record_version, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), actor_id, now, record["id"]),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            record["id"],
                            "ratio_adjusted",
                            actor_id,
                            record_version,
                            json.dumps(
                                {"announcement": reference, "request_id": request_id, "old_ratio": old_ratio, "new_ratio": ratio, "changes": changes},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
                    affected += 1
                else:
                    skipped += 1

            connection.execute(
                "UPDATE ca_announcements SET ratio=?,status='confirmed',version=?,updated_by=?,updated_at=? WHERE id=?",
                (ratio, version, actor_id, now, announcement_id),
            )
            cursor = connection.execute(
                "INSERT INTO ca_adjustments(announcement_id,request_id,actor_id,version,old_ratio,new_ratio,affected,skipped,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (announcement_id, request_id, actor_id, version, old_ratio, ratio, affected, skipped, now),
            )
            adjustment_id = int(cursor.lastrowid)
            latest = connection.execute("SELECT * FROM ca_announcements WHERE id=?", (announcement_id,)).fetchone()
            adjustment = connection.execute("SELECT * FROM ca_adjustments WHERE id=?", (adjustment_id,)).fetchone()
            connection.commit()
        result = dict(adjustment)
        result["idempotent"] = False
        result["announcement"] = dict(latest)
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
