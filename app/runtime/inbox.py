"""Shared PostgreSQL intake and short, ownership-fenced worker transactions."""
import json
import os
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from starlette.concurrency import run_in_threadpool

from app.runtime.outcomes import safe_error_code
from app.runtime.store import TaskConflict, TaskStore, operation_id, task_payload


class LeaseLost(Exception):
    """A stale executor must not continue or persist results."""


class PostgresInbox:
    def __init__(self, db_url: str, *, schema: str = "agent_inbox"):
        if not db_url.strip():
            raise ValueError("AGENT_INBOX_DB_URL is required")
        self._dsn = make_conninfo(db_url, connect_timeout=5)
        self._schema = schema
        self._threads = sql.Identifier(schema, "threads")
        self._jobs = sql.Identifier(schema, "jobs")

    @contextmanager
    def connection(self):
        with psycopg.connect(self._dsn, row_factory=dict_row) as db:
            db.execute("SET LOCAL statement_timeout = '5s'")
            db.execute("SET LOCAL lock_timeout = '5s'")
            yield db

    def setup(self):
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (f"inbox-setup:{self._schema}",))
            db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self._schema)))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    thread_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                    case_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    evidence_version INTEGER NOT NULL CHECK (evidence_version >= 1))
            """).format(self._threads))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
                    operation_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES {}(thread_id),
                    fingerprint TEXT NOT NULL, envelope TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'QUEUED', code TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)
            """).format(self._jobs, self._threads))
            # Upgrade #58 installations without rewriting envelopes or identities.
            for column in (
                "worker_id TEXT", "lease_token UUID", "lease_expires_at TIMESTAMPTZ",
                "attempts INTEGER NOT NULL DEFAULT 0",
                "available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
                "started BOOLEAN NOT NULL DEFAULT FALSE",
                "mutation_started BOOLEAN NOT NULL DEFAULT FALSE", "action_id UUID",
                "recovery_required BOOLEAN NOT NULL DEFAULT FALSE",
                "last_worker_id TEXT",
            ):
                db.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS ").format(self._jobs)
                           + sql.SQL(column))
            db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS jobs_thread_sequence ON {} (thread_id, sequence DESC)
            """).format(self._jobs))
            db.execute(sql.SQL("""
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running_thread
                ON {} (thread_id) WHERE status='RUNNING'
            """).format(self._jobs))

    def check(self):
        with self.connection() as db:
            db.execute(sql.SQL("SELECT operation_id FROM {} LIMIT 0").format(self._jobs))

    def accept(self, task):
        payload, fingerprint = task_payload(task)
        key = operation_id(task)
        refs = (task.thread_id, str(task.task_id), str(task.case_id),
                str(task.snapshot_id), task.expected_evidence_version)
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (f"inbox-operation:{self._schema}:{key}",))
            old = db.execute(sql.SQL("SELECT * FROM {} WHERE operation_id=%s").format(self._jobs),
                             (key,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise TaskConflict("IDEMPOTENCY_CONFLICT")
                return TaskStore.public(old), False
            if task.kind != "RESUME_REVIEW":
                inserted = db.execute(sql.SQL("""
                    INSERT INTO {} (thread_id,task_id,case_id,snapshot_id,evidence_version)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING thread_id
                """).format(self._threads), refs).fetchone()
                if inserted is None:
                    origin = db.execute(sql.SQL("SELECT task_id FROM {} WHERE thread_id=%s").format(
                        self._threads), (task.thread_id,)).fetchone()
                    raise TaskConflict("THREAD_ALREADY_BOUND" if origin else "TASK_ALREADY_BOUND")
            else:
                origin = db.execute(sql.SQL("""
                    SELECT thread_id,task_id,case_id,snapshot_id,evidence_version
                    FROM {} WHERE thread_id=%s FOR UPDATE
                """).format(self._threads), (task.thread_id,)).fetchone()
                if origin is None or tuple(origin.values()) != refs:
                    raise TaskConflict("THREAD_BINDING_MISMATCH")
                latest = db.execute(sql.SQL("""
                    SELECT status FROM {} WHERE thread_id=%s ORDER BY sequence DESC LIMIT 1
                """).format(self._jobs), (task.thread_id,)).fetchone()
                if latest is None or latest["status"] != "WAITING_REVIEW":
                    raise TaskConflict("THREAD_NOT_WAITING")
            row = db.execute(sql.SQL("""
                INSERT INTO {} (operation_id,thread_id,fingerprint,envelope)
                VALUES (%s,%s,%s,%s) RETURNING *
            """).format(self._jobs), (key, task.thread_id, fingerprint, json.dumps(payload))).fetchone()
            return TaskStore.public(row), True

    def get(self, task_id):
        with self.connection() as db:
            row = db.execute(sql.SQL("""
                SELECT j.* FROM {} j JOIN {} t ON t.thread_id=j.thread_id
                WHERE t.task_id=%s ORDER BY j.sequence DESC LIMIT 1
            """).format(self._jobs, self._threads), (str(task_id),)).fetchone()
            return TaskStore.public(row) if row else None

    def claim(self, worker_id, *, lease_seconds=30, max_attempts=3):
        if not worker_id or len(worker_id) > 128 or lease_seconds <= 0 or max_attempts < 1:
            raise ValueError("Invalid worker lease settings")
        with self.connection() as db:
            # Thread row locks serialize claims with resume intake across processes.
            thread = db.execute(sql.SQL("""
                SELECT t.thread_id FROM {} t WHERE EXISTS (
                    SELECT 1 FROM {} j WHERE j.thread_id=t.thread_id AND
                    ((j.status='QUEUED' AND j.available_at<=clock_timestamp())
                     OR (j.status='RUNNING' AND j.lease_expires_at<=clock_timestamp())))
                ORDER BY t.thread_id FOR UPDATE OF t SKIP LOCKED LIMIT 1
            """).format(self._threads, self._jobs)).fetchone()
            if thread is None:
                return None
            row = db.execute(sql.SQL("""
                SELECT * FROM {} WHERE thread_id=%s
                AND status IN ('QUEUED','RUNNING','UNKNOWN')
                ORDER BY sequence LIMIT 1 FOR UPDATE
            """).format(self._jobs), (thread["thread_id"],)).fetchone()
            if (row is None or row["status"] == "UNKNOWN"
                    or (row["status"] == "QUEUED" and row["available_at"] > db.execute(
                        "SELECT clock_timestamp() AS now").fetchone()["now"])
                    or (row["status"] == "RUNNING" and row["lease_expires_at"] > db.execute(
                        "SELECT clock_timestamp() AS now").fetchone()["now"])):
                return None
            recovery = row["started"]
            if row["attempts"] >= max_attempts and not recovery:
                db.execute(sql.SQL("""
                    UPDATE {} SET status='FAILED',code='ATTEMPTS_EXHAUSTED',
                    worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                    updated_at=clock_timestamp() WHERE operation_id=%s
                """).format(self._jobs), (row["operation_id"],))
                return None
            return db.execute(sql.SQL("""
                UPDATE {} SET status='RUNNING',worker_id=%s,last_worker_id=%s,
                lease_token=%s,lease_expires_at=clock_timestamp()+(%s * interval '1 second'),
                attempts=attempts+1,recovery_required=%s,updated_at=clock_timestamp()
                WHERE operation_id=%s RETURNING *
            """).format(self._jobs), (worker_id, worker_id, uuid4(), lease_seconds,
                                     recovery, row["operation_id"])).fetchone()

    def ownership_sql(self):
        return sql.SQL("""
            SELECT operation_id FROM {} WHERE operation_id=%s AND thread_id=%s
            AND worker_id=%s AND lease_token=%s AND status='RUNNING'
            AND lease_expires_at>clock_timestamp() FOR UPDATE
        """).format(self._jobs)

    @staticmethod
    def ownership_params(job):
        return (job["operation_id"], job["thread_id"], job["worker_id"], job["lease_token"])

    def _owned(self, db, job):
        if db.execute(self.ownership_sql(), self.ownership_params(job)).fetchone() is None:
            raise LeaseLost()

    @staticmethod
    def _updated(result):
        if result.rowcount != 1:
            raise LeaseLost()

    def assert_owned(self, job):
        with self.connection() as db:
            self._owned(db, job)

    def heartbeat(self, job, *, lease_seconds):
        with self.connection() as db:
            self._owned(db, job)
            self._updated(db.execute(sql.SQL("""
                UPDATE {} SET lease_expires_at=clock_timestamp()+(%s * interval '1 second'),
                updated_at=clock_timestamp() WHERE operation_id=%s
                AND lease_expires_at>clock_timestamp()
            """).format(self._jobs), (lease_seconds, job["operation_id"])))

    def mark_started(self, job):
        with self.connection() as db:
            self._owned(db, job)
            self._updated(db.execute(sql.SQL("""
                UPDATE {} SET started=TRUE WHERE operation_id=%s
                AND lease_expires_at>clock_timestamp()
            """).format(self._jobs), (job["operation_id"],)))

    def mark_mutation(self, job, action_id=None):
        with self.connection() as db:
            self._owned(db, job)
            self._updated(db.execute(sql.SQL("""
                UPDATE {} SET mutation_started=TRUE,action_id=COALESCE(%s,action_id)
                WHERE operation_id=%s AND lease_expires_at>clock_timestamp()
            """).format(self._jobs), (action_id, job["operation_id"])))

    def finish(self, job, status, code=None):
        if status not in {"SUCCEEDED", "FAILED", "NEEDS_REVIEW", "WAITING_REVIEW", "UNKNOWN"}:
            raise ValueError("Invalid task outcome")
        with self.connection() as db:
            self._owned(db, job)
            self._updated(db.execute(sql.SQL("""
                UPDATE {} SET status=%s,code=%s,worker_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,updated_at=clock_timestamp() WHERE operation_id=%s
                AND lease_expires_at>clock_timestamp()
            """).format(self._jobs), (status, safe_error_code(code), job["operation_id"])))

    def retry_unstarted(self, job, *, max_attempts=3, backoff_seconds=1):
        with self.connection() as db:
            self._owned(db, job)
            current = db.execute(sql.SQL("SELECT started FROM {} WHERE operation_id=%s").format(
                self._jobs), (job["operation_id"],)).fetchone()
            if current["started"]:
                raise ValueError("Started execution requires reconciliation")
            exhausted = job["attempts"] >= max_attempts
            delay = min(300, backoff_seconds * 2 ** min(job["attempts"] - 1, 16))
            self._updated(db.execute(sql.SQL("""
                UPDATE {} SET status=%s,code=%s,worker_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,available_at=clock_timestamp()+(%s * interval '1 second'),
                updated_at=clock_timestamp() WHERE operation_id=%s
                AND lease_expires_at>clock_timestamp()
            """).format(self._jobs), ("FAILED" if exhausted else "QUEUED",
                "ATTEMPTS_EXHAUSTED" if exhausted else "RETRY_BACKOFF", delay, job["operation_id"])))


@asynccontextmanager
async def configured_inbox():
    db_url = os.getenv("AGENT_INBOX_DB_URL", "")
    if not db_url:
        yield None
        return
    inbox = PostgresInbox(db_url)
    await run_in_threadpool(inbox.setup)
    yield inbox
