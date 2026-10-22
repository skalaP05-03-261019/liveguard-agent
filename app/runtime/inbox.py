"""Shared intake only. Graph execution, leases and recovery are separate concerns."""
import json
import os
from contextlib import asynccontextmanager, contextmanager

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from starlette.concurrency import run_in_threadpool

from app.runtime.store import TaskConflict, TaskStore, operation_id, task_payload


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
            # Serialize first-boot DDL across API instances, not just within a process.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (f"inbox-setup:{self._schema}",))
            db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(self._schema)))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    thread_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
                    case_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    evidence_version INTEGER NOT NULL CHECK (evidence_version >= 1)
                )
            """).format(self._threads))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
                    operation_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES {}(thread_id),
                    fingerprint TEXT NOT NULL, envelope TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'QUEUED', code TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """).format(self._jobs, self._threads))
            db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS jobs_thread_sequence
                ON {} (thread_id, sequence DESC)
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
            # The operation lock also covers a reused decisionId on a different thread.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (f"inbox-operation:{self._schema}:{key}",))
            old = db.execute(sql.SQL(
                "SELECT * FROM {} WHERE operation_id=%s"
            ).format(self._jobs), (key,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise TaskConflict("IDEMPOTENCY_CONFLICT")
                return TaskStore.public(old), False

            if task.kind != "RESUME_REVIEW":
                inserted = db.execute(sql.SQL("""
                    INSERT INTO {} (thread_id, task_id, case_id, snapshot_id, evidence_version)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING thread_id
                """).format(self._threads), refs).fetchone()
                if inserted is None:
                    origin = db.execute(sql.SQL(
                        "SELECT task_id FROM {} WHERE thread_id=%s"
                    ).format(self._threads), (task.thread_id,)).fetchone()
                    raise TaskConflict("THREAD_ALREADY_BOUND" if origin else "TASK_ALREADY_BOUND")
            else:
                origin = db.execute(sql.SQL(
                    """SELECT thread_id, task_id, case_id, snapshot_id, evidence_version
                    FROM {} WHERE thread_id=%s FOR UPDATE"""
                ).format(self._threads), (task.thread_id,)).fetchone()
                if origin is None or tuple(origin.values()) != refs:
                    raise TaskConflict("THREAD_BINDING_MISMATCH")
                latest = db.execute(sql.SQL("""
                    SELECT status FROM {} WHERE thread_id=%s ORDER BY sequence DESC LIMIT 1
                """).format(self._jobs), (task.thread_id,)).fetchone()
                if latest is None or latest["status"] != "WAITING_REVIEW":
                    raise TaskConflict("THREAD_NOT_WAITING")

            row = db.execute(sql.SQL("""
                INSERT INTO {} (operation_id, thread_id, fingerprint, envelope)
                VALUES (%s,%s,%s,%s) RETURNING *
            """).format(self._jobs),
                (key, task.thread_id, fingerprint, json.dumps(payload))).fetchone()
            return TaskStore.public(row), True

    def get(self, task_id):
        with self.connection() as db:
            row = db.execute(sql.SQL("""
                SELECT j.* FROM {} j JOIN {} t ON t.thread_id=j.thread_id
                WHERE t.task_id=%s ORDER BY j.sequence DESC LIMIT 1
            """).format(self._jobs, self._threads), (str(task_id),)).fetchone()
            return TaskStore.public(row) if row else None


@asynccontextmanager
async def configured_inbox():
    db_url = os.getenv("AGENT_INBOX_DB_URL", "")
    if not db_url:
        yield None
        return
    inbox = PostgresInbox(db_url)
    await run_in_threadpool(inbox.setup)
    yield inbox
