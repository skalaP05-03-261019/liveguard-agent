"""Agent-only durable inbox. Contains envelopes and safe status, never business rows.

SQLite is a local-volume deployment choice for the HTTP-first runner. Atomic claims
and per-thread exclusion work across workers sharing the SAME database file.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.schemas import TaskEnvelope


class TaskConflict(Exception):
    pass


def operation_id(task):
    return f"resume:{task.decision_id}" if task.kind == "RESUME_REVIEW" else f"run:{task.task_id}"


class TaskStore:
    def __init__(self, path):
        if not path or path == ":memory:":
            raise ValueError("AGENT_TASK_DB_PATH must be a persistent file")
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                    case_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, evidence_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    operation_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, envelope TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'QUEUED', code TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_per_thread
                    ON jobs(thread_id) WHERE status = 'RUNNING';
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def accept(self, task: TaskEnvelope):
        payload = task.model_dump(mode="json", by_alias=True)
        logical = {k: v for k, v in payload.items() if k not in {"eventId", "traceId"}}
        fingerprint = hashlib.sha256(json.dumps(logical, sort_keys=True).encode()).hexdigest()
        key = operation_id(task)
        refs = (task.thread_id, str(task.task_id), str(task.case_id), str(task.snapshot_id),
                task.expected_evidence_version)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE operation_id=?", (key,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise TaskConflict("IDEMPOTENCY_CONFLICT")
                db.commit()
                return self.public(old), False
            origin = db.execute("SELECT * FROM threads WHERE thread_id=?", (task.thread_id,)).fetchone()
            if task.kind == "RESUME_REVIEW":
                if origin is None or tuple(origin) != refs:
                    raise TaskConflict("THREAD_BINDING_MISMATCH")
                latest = db.execute("SELECT status FROM jobs WHERE thread_id=? ORDER BY rowid DESC LIMIT 1",
                                    (task.thread_id,)).fetchone()
                if latest["status"] != "WAITING_REVIEW":
                    raise TaskConflict("THREAD_NOT_WAITING")
            else:
                if origin:
                    raise TaskConflict("THREAD_ALREADY_BOUND")
                try:
                    db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", refs)
                except sqlite3.IntegrityError:
                    raise TaskConflict("TASK_ALREADY_BOUND") from None
            db.execute("INSERT INTO jobs(operation_id,thread_id,fingerprint,envelope) VALUES (?,?,?,?)",
                       (key, task.thread_id, fingerprint, json.dumps(payload)))
            row = db.execute("SELECT * FROM jobs WHERE operation_id=?", (key,)).fetchone()
            db.commit()
            return self.public(row), True

    def claim(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM jobs j WHERE status='QUEUED'
                AND NOT EXISTS (SELECT 1 FROM jobs busy WHERE busy.thread_id=j.thread_id
                    AND busy.status IN ('RUNNING','UNKNOWN')) ORDER BY rowid LIMIT 1""").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='RUNNING',updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                           (row["operation_id"],))
            db.commit()
            return dict(row) if row else None

    def finish(self, key, status, code=None):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status=?,code=?,updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                       (status, code, key))

    def get(self, task_id):
        with self.connection() as db:
            row = db.execute("""SELECT j.* FROM jobs j JOIN threads t ON t.thread_id=j.thread_id
                WHERE t.task_id=? ORDER BY j.rowid DESC LIMIT 1""", (str(task_id),)).fetchone()
            return self.public(row) if row else None

    @staticmethod
    def public(row):
        task = json.loads(row["envelope"])
        return {"taskId": task["taskId"], "threadId": task["threadId"],
                "operationId": row["operation_id"], "status": row["status"], "code": row["code"]}
