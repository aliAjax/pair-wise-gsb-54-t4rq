"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 接续条目自然键：同一（区段, 接续点, 现场时刻）只允许一条已确认记录，
# 两名工程师同时提交时只收一条，另一条留作待确认现场数据。
SCHEMA = """
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
CREATE TABLE IF NOT EXISTS segment_archives (
    cable TEXT NOT NULL,
    segment TEXT NOT NULL,
    loss_budget_db REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cable, segment)
);
CREATE TABLE IF NOT EXISTS splice_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cable TEXT NOT NULL,
    segment TEXT NOT NULL,
    record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
    submission_id TEXT NOT NULL,
    splice_point_km REAL NOT NULL,
    splice_loss_db REAL NOT NULL,
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    replaces_entry_id INTEGER,
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (cable, segment, submission_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_splice_unique_confirmed
    ON splice_entries(cable, segment, splice_point_km, occurred_at)
    WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
CREATE INDEX IF NOT EXISTS idx_splice_lookup ON splice_entries(cable, segment, status);
CREATE INDEX IF NOT EXISTS idx_splice_record ON splice_entries(record_id, status);
"""


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
            connection.executescript(SCHEMA)

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _entry_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        if item.get("record_id") is not None:
            item["record_id"] = int(item["record_id"])
        if item.get("replaces_entry_id") is not None:
            item["replaces_entry_id"] = int(item["replaces_entry_id"])
        for key in ("splice_point_km", "splice_loss_db"):
            item[key] = round(float(item[key]), 6)
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                # 接续预算以档案建立时的首张故障单为准
                connection.execute(
                    "INSERT INTO segment_archives(cable,segment,loss_budget_db,created_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(cable,segment) DO NOTHING",
                    (payload["cable"], payload["segment"], float(payload["splice_loss_budget_db"]), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def get_by_reference(self, reference: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()
        if row is None:
            raise NotFound("故障单不存在")
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

    # ------------------------------------------------------------------
    # 区段接续档案
    # ------------------------------------------------------------------

    def get_archive(self, cable: str, segment: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM segment_archives WHERE cable=? AND segment=?", (cable, segment)).fetchone()
            if row is None:
                return None
            stats = connection.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status='confirmed' THEN splice_loss_db ELSE 0 END),0) AS cumulative_loss_db, "
                "SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) AS confirmed_count, "
                "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count "
                "FROM splice_entries WHERE cable=? AND segment=?",
                (cable, segment),
            ).fetchone()
        return {
            "cable": row["cable"],
            "segment": row["segment"],
            "loss_budget_db": round(float(row["loss_budget_db"]), 4),
            "cumulative_loss_db": round(float(stats["cumulative_loss_db"]), 6),
            "confirmed_count": int(stats["confirmed_count"] or 0),
            "pending_count": int(stats["pending_count"] or 0),
        }

    def list_splice_entries(self, cable: str = None, segment: str = None, status: str = None, limit: int = 200) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if cable:
            clauses.append("cable=?")
            params.append(cable)
        if segment:
            clauses.append("segment=?")
            params.append(segment)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM splice_entries" + where + " ORDER BY occurred_at, id LIMIT ?", params
            ).fetchall()
        return [self._entry_row(row) for row in rows]

    def get_splice_entry(self, entry_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM splice_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise NotFound("接续条目不存在")
        return self._entry_row(row)

    @staticmethod
    def _archive_stats(connection: sqlite3.Connection, cable: str, segment: str) -> Dict[str, Any]:
        row = connection.execute(
            "SELECT loss_budget_db FROM segment_archives WHERE cable=? AND segment=?", (cable, segment)
        ).fetchone()
        if row is None:
            raise NotFound("区段接续档案不存在")
        stats = connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status='confirmed' THEN splice_loss_db ELSE 0 END),0) AS loss, "
            "SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) AS confirmed, "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending "
            "FROM splice_entries WHERE cable=? AND segment=?",
            (cable, segment),
        ).fetchone()
        return {
            "loss_budget_db": round(float(row["loss_budget_db"]), 4),
            "cumulative_loss_db": round(float(stats["loss"]), 6),
            "confirmed_count": int(stats["confirmed"] or 0),
            "pending_count": int(stats["pending"] or 0),
        }

    @staticmethod
    def _regress(connection: sqlite3.Connection, record_id: int, *, reason: str, actor_id: str, extra_details: Dict[str, Any]) -> None:
        """将故障单退回待整改，原测试结论随之失效；已记录审计版本号。"""
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        version = int(row["version"]) + 1
        now = _now()
        connection.execute(
            "UPDATE records SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?",
            ("rectifying", version, actor_id, now, record_id),
        )
        details = {"summary": reason, "reason": reason, "from_state_regression": True}
        details.update(extra_details)
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "regressed", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    def commit_splice(self, record_id: int, expected_version: int, actor_id: str, fields: Dict[str, Any],
                      replaces_entry_id: Optional[int], budget: float, action: str) -> Dict[str, Any]:
        """正式接续动作(splice)或整改重做(rectify)：乐观锁 + 档案累计 + 预算判定，一个事务完成。

        重试不会重复累加：同一(cable,segment,submission_id)直接返回既有条目；
        整改重做时旧条目置为superseded，不再计入累计损耗。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            payload = json.loads(record["payload"])
            cable, segment = payload["cable"], payload["segment"]
            version = int(record["version"])

            # 幂等重试：同一次提交重复写入直接返回既有结果，不校验版本/状态，不再次累加
            duplicate = connection.execute(
                "SELECT * FROM splice_entries WHERE cable=? AND segment=? AND submission_id=?",
                (cable, segment, fields["submission_id"]),
            ).fetchone()
            if duplicate is not None:
                stats = self._archive_stats(connection, cable, segment)
                result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
                return {"record": self._row(result), "entry": self._entry_row(duplicate), "duplicate": True,
                        "accepted_status": duplicate["status"],
                        "new_state": result["state"], "archive": stats}

            if version != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            required_state = "rectifying" if action == "rectify" else "surveyed"
            if record["state"] != required_state:
                connection.rollback()
                raise Conflict("当前状态不允许执行%s" % action)

            superseded_entry = None
            if replaces_entry_id is not None:
                target = connection.execute(
                    "SELECT * FROM splice_entries WHERE id=? AND cable=? AND segment=?",
                    (replaces_entry_id, cable, segment),
                ).fetchone()
                if target is None:
                    connection.rollback()
                    raise NotFound("被整改的接续条目不存在")
                if target["status"] != "confirmed":
                    connection.rollback()
                    raise Conflict("只能整改已确认的接续条目")
                superseded_entry = target

            connection.execute(
                "INSERT INTO segment_archives(cable,segment,loss_budget_db,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(cable,segment) DO NOTHING",
                (cable, segment, float(budget), now),
            )
            # 同点同时刻已有确认记录：只收一条，本次测量留待确认，不计入累计
            # 整改重做时排除即将被替换的旧条目本身
            twin = connection.execute(
                "SELECT * FROM splice_entries WHERE cable=? AND segment=? AND splice_point_km=? AND occurred_at=? "
                "AND status='confirmed'" + (" AND id<>?" if replaces_entry_id is not None else ""),
                (cable, segment, fields["splice_point_km"], fields["occurred_at"]) +
                ((replaces_entry_id,) if replaces_entry_id is not None else ()),
            ).fetchone()
            accepted_status = "pending" if twin is not None else "confirmed"
            try:
                cursor = connection.execute(
                    "INSERT INTO splice_entries(cable,segment,record_id,submission_id,splice_point_km,splice_loss_db,"
                    "occurred_at,status,origin,replaces_entry_id,submitted_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cable, segment, record_id, fields["submission_id"], fields["splice_point_km"], fields["splice_loss_db"],
                     fields["occurred_at"], accepted_status, action, replaces_entry_id, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("接续记录提交冲突，请刷新后重试") from exc
            entry_id = int(cursor.lastrowid)
            if superseded_entry is not None:
                connection.execute(
                    "UPDATE splice_entries SET status='superseded', updated_at=? WHERE id=?", (now, superseded_entry["id"])
                )

            stats = self._archive_stats(connection, cable, segment)
            over_budget = stats["cumulative_loss_db"] > stats["loss_budget_db"] + 1e-9
            new_state = "rectifying" if over_budget else "spliced"
            new_version = version + 1
            payload["cumulative_loss_db"] = stats["cumulative_loss_db"]
            if action == "splice":
                payload["spare_used_km"] = float(fields["spare_used_km"])
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, new_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            summary = "累计接续损耗%sdB超过预算%sdB，退回待整改" % (stats["cumulative_loss_db"], stats["loss_budget_db"]) if over_budget else ("光缆接续完成" if action == "splice" else "整改重做完成")
            details = {
                "summary": summary, "from": record["state"], "to": new_state,
                "entry_id": entry_id, "submission_id": fields["submission_id"],
                "splice_loss_db": fields["splice_loss_db"], "cumulative_loss_db": stats["cumulative_loss_db"],
                "loss_budget_db": stats["loss_budget_db"], "over_budget": over_budget,
                "accepted_status": accepted_status,
                "replaces_entry_id": replaces_entry_id,
            }
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, new_version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            entry = connection.execute("SELECT * FROM splice_entries WHERE id=?", (entry_id,)).fetchone()
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "entry": self._entry_row(entry), "duplicate": False,
                "accepted_status": accepted_status,
                "new_state": new_state, "archive": stats}

    def ingest_field_entry(self, record_id: int, actor_id: str, fields: Dict[str, Any], budget: float) -> Dict[str, Any]:
        """接收晚到/并行的现场接续数据：

        - submission_id 已存在：重试幂等，原样返回；
        - (接续点, 时刻) 已有 confirmed：只收一条，本次留 pending 待确认；
        - 否则确认并入档案，累计超预算时把未恢复的故障单退回待整改（原测试结果失效）。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            payload = json.loads(record["payload"])
            cable, segment = payload["cable"], payload["segment"]

            duplicate = connection.execute(
                "SELECT * FROM splice_entries WHERE cable=? AND segment=? AND submission_id=?",
                (cable, segment, fields["submission_id"]),
            ).fetchone()
            if duplicate is not None:
                stats = self._archive_stats(connection, cable, segment)
                result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
                return {"record": self._row(result), "entry": self._entry_row(duplicate), "duplicate": True,
                        "accepted_as": duplicate["status"], "regressed": False, "archive": stats}

            twin = connection.execute(
                "SELECT * FROM splice_entries WHERE cable=? AND segment=? AND splice_point_km=? AND occurred_at=? "
                "AND status='confirmed'",
                (cable, segment, fields["splice_point_km"], fields["occurred_at"]),
            ).fetchone()
            status = "pending" if twin is not None else "confirmed"

            connection.execute(
                "INSERT INTO segment_archives(cable,segment,loss_budget_db,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(cable,segment) DO NOTHING",
                (cable, segment, float(budget), now),
            )
            try:
                cursor = connection.execute(
                    "INSERT INTO splice_entries(cable,segment,record_id,submission_id,splice_point_km,splice_loss_db,"
                    "occurred_at,status,origin,replaces_entry_id,submitted_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'field_data',NULL,?,?,?)",
                    (cable, segment, record_id, fields["submission_id"], fields["splice_point_km"], fields["splice_loss_db"],
                     fields["occurred_at"], status, actor_id, now, now),
                )
            except sqlite3.IntegrityError:
                # 两名工程师同时提交竞态：唯一确认索引冲突的一方留待确认，不重复累加
                status = "pending"
                cursor = connection.execute(
                    "INSERT INTO splice_entries(cable,segment,record_id,submission_id,splice_point_km,splice_loss_db,"
                    "occurred_at,status,origin,replaces_entry_id,submitted_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending','field_data',NULL,?,?,?)",
                    (cable, segment, record_id, fields["submission_id"], fields["splice_point_km"], fields["splice_loss_db"],
                     fields["occurred_at"], actor_id, now, now),
                )
            entry_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "field_splice", actor_id, int(record["version"]),
                 json.dumps({"summary": "现场接续数据已接收", "status": status, "entry_id": entry_id,
                             "submission_id": fields["submission_id"], "splice_loss_db": fields["splice_loss_db"],
                             "duplicate_of": int(twin["id"]) if twin is not None else None},
                            ensure_ascii=False, sort_keys=True), now),
            )

            regressed = False
            reason = ""
            if status == "confirmed":
                stats = self._archive_stats(connection, cable, segment)
                if stats["cumulative_loss_db"] > stats["loss_budget_db"] + 1e-9 and record["state"] in {"spliced", "tested"}:
                    reason = "晚到现场接续记录使累计损耗%sdB超过预算%sdB，原测试结果失效，退回待整改" % (
                        stats["cumulative_loss_db"], stats["loss_budget_db"])
                    self._regress(connection, record_id, reason=reason, actor_id=actor_id,
                                  extra_details={"entry_id": entry_id, "cumulative_loss_db": stats["cumulative_loss_db"],
                                                 "loss_budget_db": stats["loss_budget_db"]})
                    regressed = True
            stats = self._archive_stats(connection, cable, segment)
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            entry = connection.execute("SELECT * FROM splice_entries WHERE id=?", (entry_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "entry": self._entry_row(entry), "duplicate": False,
                "accepted_as": status, "regressed": regressed, "reason": reason, "archive": stats}

    def resolve_field_entry(self, entry_id: int, actor_id: str, decision: str, budget: float) -> Dict[str, Any]:
        """确认或丢弃待确认的现场数据。确认即入档案累计，超预算联动退回待整改。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            entry = connection.execute("SELECT * FROM splice_entries WHERE id=?", (entry_id,)).fetchone()
            if entry is None:
                connection.rollback()
                raise NotFound("接续条目不存在")
            if entry["status"] != "pending":
                connection.rollback()
                raise Conflict("该条目已处理，当前状态:%s" % entry["status"])
            cable, segment = entry["cable"], entry["segment"]
            connection.execute(
                "INSERT INTO segment_archives(cable,segment,loss_budget_db,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(cable,segment) DO NOTHING",
                (cable, segment, float(budget), now),
            )
            if decision == "discard":
                connection.execute("UPDATE splice_entries SET status='discarded', updated_at=? WHERE id=?", (now, entry_id))
                action_name, summary = "field_discarded", "现场接续数据已丢弃"
                regressed = False
            else:
                twin = connection.execute(
                    "SELECT * FROM splice_entries WHERE cable=? AND segment=? AND splice_point_km=? AND occurred_at=? "
                    "AND status='confirmed'",
                    (cable, segment, entry["splice_point_km"], entry["occurred_at"]),
                ).fetchone()
                if twin is not None:
                    connection.rollback()
                    raise Conflict("同一接续点同时刻已存在确认记录(条目%s)，请改为丢弃" % twin["id"])
                connection.execute("UPDATE splice_entries SET status='confirmed', updated_at=? WHERE id=?", (now, entry_id))
                action_name, summary = "field_confirmed", "现场接续数据已确认并入档"
                regressed = False
            stats = self._archive_stats(connection, cable, segment)
            reason = ""
            regressed_record = None
            if decision == "confirm" and stats["cumulative_loss_db"] > stats["loss_budget_db"] + 1e-9:
                targets = connection.execute("SELECT id,state,payload FROM records").fetchall()
                for row in targets:
                    if row["state"] not in {"spliced", "tested"}:
                        continue
                    row_payload = json.loads(row["payload"])
                    if row_payload.get("cable") == cable and row_payload.get("segment") == segment:
                        reason = "确认现场接续后累计损耗%sdB超过预算%sdB，原测试结果失效，退回待整改" % (
                            stats["cumulative_loss_db"], stats["loss_budget_db"])
                        self._regress(connection, int(row["id"]), reason=reason, actor_id=actor_id,
                                      extra_details={"entry_id": entry_id, "cumulative_loss_db": stats["cumulative_loss_db"],
                                                     "loss_budget_db": stats["loss_budget_db"]})
                        regressed = True
                        regressed_record = int(row["id"])
                        break
            if entry["record_id"] is not None:
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (int(entry["record_id"]), action_name, actor_id,
                     int(connection.execute("SELECT version FROM records WHERE id=?", (int(entry["record_id"]),)).fetchone()["version"]),
                     json.dumps({"summary": summary, "entry_id": entry_id, "decision": decision,
                                 "regressed_record": regressed_record}, ensure_ascii=False, sort_keys=True), now),
                )
            refreshed = connection.execute("SELECT * FROM splice_entries WHERE id=?", (entry_id,)).fetchone()
            connection.commit()
        return {"entry": self._entry_row(refreshed), "regressed": regressed, "regressed_record_id": regressed_record,
                "reason": reason, "archive": stats}

    def backfill_history(self, record_id: int, expected_version: int, actor_id: str, items: List[Dict[str, Any]],
                         budget: float) -> Dict[str, Any]:
        """旧单补登历史接续项：一次提交、原子入档，累计超预算同样退回待整改。

        submission_id 冲突即判定为重复补登（重试），整笔拒绝，不产生任何累加。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            version = int(record["version"])
            if version != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if record["state"] not in {"spliced", "tested", "rectifying"}:
                connection.rollback()
                raise Conflict("当前状态不允许执行backfill")
            payload = json.loads(record["payload"])
            cable, segment = payload["cable"], payload["segment"]
            remaining = max(0, int(payload.get("legacy_unrecorded_splices", 0)) - int(payload.get("history_items_recorded", 0)))
            if remaining <= 0:
                connection.rollback()
                raise Conflict("该故障单无需补登历史接续项")
            if len(items) > remaining:
                connection.rollback()
                raise Conflict("补登项超过待补数量，还需%s项" % remaining)
            connection.execute(
                "INSERT INTO segment_archives(cable,segment,loss_budget_db,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(cable,segment) DO NOTHING",
                (cable, segment, float(budget), now),
            )
            entry_ids = []
            for item in items:
                exists = connection.execute(
                    "SELECT 1 FROM splice_entries WHERE cable=? AND segment=? AND submission_id=?",
                    (cable, segment, item["submission_id"]),
                ).fetchone()
                if exists is not None:
                    connection.rollback()
                    raise Conflict("历史接续项%s已补登，请勿重复提交" % item["submission_id"])
                try:
                    cursor = connection.execute(
                        "INSERT INTO splice_entries(cable,segment,record_id,submission_id,splice_point_km,splice_loss_db,"
                        "occurred_at,status,origin,replaces_entry_id,submitted_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,'confirmed','backfill',NULL,?,?,?)",
                        (cable, segment, record_id, item["submission_id"], item["splice_point_km"], item["splice_loss_db"],
                         item["occurred_at"], actor_id, now, now),
                    )
                    entry_ids.append(int(cursor.lastrowid))
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    raise Conflict("历史接续项与既有确认记录冲突(同点同时刻)，请核对后再补登") from exc

            stats = self._archive_stats(connection, cable, segment)
            recorded = int(payload.get("history_items_recorded", 0)) + len(items)
            required = int(payload.get("legacy_unrecorded_splices", 0))
            payload["history_items_recorded"] = recorded
            payload["history_complete"] = recorded >= required
            payload["cumulative_loss_db"] = stats["cumulative_loss_db"]
            over_budget = stats["cumulative_loss_db"] > stats["loss_budget_db"] + 1e-9
            new_state = "rectifying" if over_budget else record["state"]
            new_version = version + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, new_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            summary = "补登%s条历史接续，累计损耗%sdB" % (len(items), stats["cumulative_loss_db"])
            if payload["history_complete"]:
                summary += "，历史接续已补齐"
            if over_budget:
                summary += "，超过预算%sdB，退回待整改" % stats["loss_budget_db"]
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "backfill", actor_id, new_version,
                 json.dumps({"summary": summary, "from": record["state"], "to": new_state, "entry_ids": entry_ids,
                             "recorded": recorded, "required": required, "history_complete": payload["history_complete"],
                             "cumulative_loss_db": stats["cumulative_loss_db"], "over_budget": over_budget},
                            ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "entry_ids": entry_ids, "new_state": new_state,
                "history_complete": payload["history_complete"], "archive": stats}

    def enforce_budget(self, record_id: int, actor_id: str) -> Dict[str, Any]:
        """恢复流量前按最新档案复核：超预算则联动退回待整改（原测试结论失效）。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            payload = json.loads(record["payload"])
            stats = self._archive_stats(connection, payload["cable"], payload["segment"])
            regressed = False
            reason = ""
            if stats["cumulative_loss_db"] > stats["loss_budget_db"] + 1e-9 and record["state"] in {"spliced", "tested"}:
                reason = "按最新档案复核：累计损耗%sdB超过预算%sdB，原测试结果失效，退回待整改" % (
                    stats["cumulative_loss_db"], stats["loss_budget_db"])
                self._regress(connection, record_id, reason=reason, actor_id=actor_id,
                              extra_details={"cumulative_loss_db": stats["cumulative_loss_db"],
                                             "loss_budget_db": stats["loss_budget_db"]})
                regressed = True
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "regressed": regressed, "reason": reason, "archive": stats}

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

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
