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
from app.runtime.store import InboxCapacityExceeded, TaskConflict, TaskStore, operation_id, task_payload


class LeaseLost(Exception):
    """A stale executor must not continue or persist results."""


class PostgresInbox:
    def __init__(self, db_url: str, *, schema: str = "agent_inbox",
                 capacity=None, graph_concurrency=None, worker_concurrency=None):
        if not db_url.strip():
            raise ValueError("AGENT_INBOX_DB_URL is required")
        self._dsn = make_conninfo(db_url, connect_timeout=5)
        self._schema = schema
        self._threads = sql.Identifier(schema, "threads")
        self._jobs = sql.Identifier(schema, "jobs")
        self._quotas = sql.Identifier(schema, "model_quotas")
        self._reservations = sql.Identifier(schema, "model_reservations")
        self._counters = sql.Identifier(schema, "runtime_counters")
        self._settings = sql.Identifier(schema, "runtime_settings")
        self._live_turns = sql.Identifier(schema, "live_turns")
        self.capacity = int(capacity if capacity is not None else os.getenv("AGENT_INBOX_CAPACITY", "10000"))
        self.graph_concurrency = int(graph_concurrency if graph_concurrency is not None else
                                     os.getenv("AGENT_GRAPH_CONCURRENCY", "4"))
        self.worker_concurrency = int(worker_concurrency if worker_concurrency is not None else
                                      os.getenv("AGENT_WORKER_GRAPH_CONCURRENCY", "2"))
        self.model_concurrency = int(os.getenv("AGENT_MODEL_CONCURRENCY", "4"))
        if min(self.capacity, self.graph_concurrency, self.worker_concurrency, self.model_concurrency) < 1:
            raise ValueError("Runtime limits must be positive")

    def _lock(self, db, name):
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                   (f"inbox:{self._schema}:{name}",))

    def _increment(self, db, name, count=1):
        db.execute(sql.SQL("""
            INSERT INTO {} (name,value) VALUES (%s,%s)
            ON CONFLICT (name) DO UPDATE SET value={}.value+EXCLUDED.value
        """).format(self._counters, self._counters), (name, count))

    def count_event(self, name):
        if name not in {"model_timeouts", "model_retries", "model_limits", "model_waits"}:
            raise ValueError("Invalid metric")
        with self.connection() as db:
            self._increment(db, name)

    @contextmanager
    def connection(self):
        with psycopg.connect(self._dsn, row_factory=dict_row) as db:
            db.execute("SET LOCAL statement_timeout = '5s'")
            db.execute("SET LOCAL lock_timeout = '5s'")
            yield db

    def setup(self):
        with self.connection() as db:
            # Serialize first-boot DDL across API and Worker instances.
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
                "live_id TEXT NOT NULL DEFAULT 'legacy:missing-live-id'",
                "admitted_at TIMESTAMPTZ",
                "running_since TIMESTAMPTZ",
                "model_wait_since TIMESTAMPTZ",
                "model_retries INTEGER NOT NULL DEFAULT 0",
                "model_calls INTEGER NOT NULL DEFAULT 0",
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
            db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS jobs_nonterminal ON {} (status,available_at,live_id)
                WHERE status IN ('QUEUED','RUNNING','WAITING_REVIEW','UNKNOWN')
            """).format(self._jobs))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    live_id TEXT PRIMARY KEY, last_turn TIMESTAMPTZ)
            """).format(self._live_turns))
            db.execute(sql.SQL("""
                INSERT INTO {} (live_id,last_turn)
                SELECT live_id,max(admitted_at) FROM {} GROUP BY live_id
                ON CONFLICT (live_id) DO UPDATE
                SET last_turn=GREATEST({}.last_turn,EXCLUDED.last_turn)
            """).format(self._live_turns, self._jobs, self._live_turns))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    name TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0)
            """).format(self._counters))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
                    capacity INTEGER NOT NULL, graph_concurrency INTEGER NOT NULL,
                    worker_concurrency INTEGER NOT NULL, model_concurrency INTEGER NOT NULL)
            """).format(self._settings))
            settings = (self.capacity, self.graph_concurrency,
                        self.worker_concurrency, self.model_concurrency)
            db.execute(sql.SQL("""
                INSERT INTO {} (capacity,graph_concurrency,worker_concurrency,model_concurrency)
                VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """).format(self._settings), settings)
            stored = db.execute(sql.SQL("""
                SELECT capacity,graph_concurrency,worker_concurrency,model_concurrency FROM {}
            """).format(self._settings)).fetchone()
            if tuple(stored.values()) != settings:
                raise ValueError("Inconsistent shared runtime limit configuration")
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    quota_key TEXT PRIMARY KEY, rpm INTEGER NOT NULL, tpm INTEGER NOT NULL,
                    concurrency INTEGER NOT NULL, cooldown_until TIMESTAMPTZ)
            """).format(self._quotas))
            db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    reservation_id UUID PRIMARY KEY, quota_key TEXT NOT NULL,
                    operation_id TEXT, lease_token UUID,
                    tokens INTEGER NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                    expires_at TIMESTAMPTZ NOT NULL)
            """).format(self._reservations))
            db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS model_reservations_window
                ON {} (quota_key,created_at)
            """).format(self._reservations))
            db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS model_reservations_active
                ON {} (expires_at) WHERE active
            """).format(self._reservations))

    def check(self):
        with self.connection() as db:
            db.execute(sql.SQL("SELECT operation_id FROM {} LIMIT 0").format(self._jobs))

    def accept(self, task):
        payload, fingerprint = task_payload(task)
        key = operation_id(task)
        refs = (task.thread_id, str(task.task_id), str(task.case_id),
                str(task.snapshot_id), task.expected_evidence_version)
        with self.connection() as db:
            # Also serialize reused decision IDs across different threads.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (f"inbox-operation:{self._schema}:{key}",))
            old = db.execute(sql.SQL("SELECT * FROM {} WHERE operation_id=%s").format(self._jobs),
                             (key,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise TaskConflict("IDEMPOTENCY_CONFLICT")
                return TaskStore.public(old), False
            self._lock(db, "capacity")
            count = db.execute(sql.SQL("""
                SELECT count(*) AS n FROM {} WHERE status IN
                ('QUEUED','RUNNING','WAITING_REVIEW','UNKNOWN')
            """).format(self._jobs)).fetchone()["n"]
            if count >= self.capacity:
                raise InboxCapacityExceeded()
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
                state = db.execute(sql.SQL("""
                    SELECT EXISTS (
                        SELECT 1 FROM {} WHERE thread_id=%s AND status='UNKNOWN'
                    ) AS unresolved_unknown,
                    (SELECT status FROM {} WHERE thread_id=%s AND status<>'FAILED'
                     ORDER BY sequence DESC LIMIT 1) AS latest_nonfailed
                """).format(self._jobs, self._jobs),
                    (task.thread_id, task.thread_id)).fetchone()
                latest = db.execute(sql.SQL("""
                    SELECT status FROM {} WHERE thread_id=%s ORDER BY sequence DESC LIMIT 1
                """).format(self._jobs), (task.thread_id,)).fetchone()
                # A review can commit in BE before its interrupt is checkpointed.
                # Keep the reference durably behind the current operation; only
                # the owning worker may validate the checkpoint and decision.
                if (latest is None or state["unresolved_unknown"]
                        or latest["status"] not in {
                            "QUEUED", "RUNNING", "WAITING_REVIEW", "FAILED"}
                        or (latest["status"] == "FAILED"
                            and state["latest_nonfailed"] != "WAITING_REVIEW")):
                    raise TaskConflict("THREAD_NOT_WAITING")
            row = db.execute(sql.SQL("""
                INSERT INTO {} (operation_id,thread_id,fingerprint,envelope,live_id)
                VALUES (%s,%s,%s,%s,%s) RETURNING *
            """).format(self._jobs), (key, task.thread_id, fingerprint, json.dumps(payload),
                str(task.live_id) if task.live_id else "legacy:missing-live-id")).fetchone()
            db.execute(sql.SQL("""
                INSERT INTO {} (live_id) VALUES (%s) ON CONFLICT DO NOTHING
            """).format(self._live_turns), (row["live_id"],))
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
            self._lock(db, "graph-admission")
            active = db.execute(sql.SQL("""
                SELECT count(*) AS n, count(*) FILTER (WHERE worker_id=%s) AS local
                FROM {} WHERE status='RUNNING' AND lease_expires_at>clock_timestamp()
            """).format(self._jobs), (worker_id,)).fetchone()
            if active["n"] >= self.graph_concurrency or active["local"] >= self.worker_concurrency:
                return None
            # Thread row locks serialize claims with resume intake across processes.
            thread = db.execute(sql.SQL("""
                WITH pending AS (
                    SELECT j.* FROM {} j WHERE
                    ((j.status='QUEUED' AND j.available_at<=clock_timestamp())
                     OR (j.status='RUNNING' AND j.lease_expires_at<=clock_timestamp()))
                    AND NOT EXISTS (
                        SELECT 1 FROM {} prior WHERE prior.thread_id=j.thread_id
                        AND prior.sequence<j.sequence
                        AND prior.status IN ('QUEUED','RUNNING','UNKNOWN')))
                SELECT t.thread_id FROM {} t JOIN pending p USING (thread_id)
                JOIN {} l USING (live_id)
                WHERE NOT EXISTS (SELECT 1 FROM pending other WHERE other.live_id<>p.live_id)
                   OR (SELECT count(*) FROM {} r WHERE r.status='RUNNING'
                       AND r.lease_expires_at>clock_timestamp() AND r.live_id=p.live_id)<%s
                ORDER BY l.last_turn NULLS FIRST,p.sequence
                FOR UPDATE OF t SKIP LOCKED LIMIT 1
            """).format(self._jobs, self._jobs, self._threads, self._live_turns, self._jobs),
                (max(1, self.graph_concurrency // 2),)).fetchone()
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
            if row["status"] == "RUNNING":
                self._increment(db, "lease_expiries")
                self._increment(db, "lease_recoveries")
            if row["attempts"] >= max_attempts and not recovery:
                db.execute(sql.SQL("""
                    UPDATE {} SET status='FAILED',code='ATTEMPTS_EXHAUSTED',
                    worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                    updated_at=clock_timestamp() WHERE operation_id=%s
                """).format(self._jobs), (row["operation_id"],))
                return None
            db.execute(sql.SQL("UPDATE {} SET last_turn=clock_timestamp() WHERE live_id=%s")
                       .format(self._live_turns), (row["live_id"],))
            return db.execute(sql.SQL("""
                UPDATE {} SET status='RUNNING',worker_id=%s,last_worker_id=%s,
                lease_token=%s,lease_expires_at=clock_timestamp()+(%s * interval '1 second'),
                attempts=attempts+1,recovery_required=%s,updated_at=clock_timestamp(),
                admitted_at=clock_timestamp(),running_since=clock_timestamp(),model_wait_since=NULL
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
            db.execute(sql.SQL("""
                UPDATE {} SET expires_at=clock_timestamp()+(%s * interval '1 second')
                WHERE operation_id=%s AND lease_token=%s AND active
            """).format(self._reservations),
                (lease_seconds, job["operation_id"], job["lease_token"]))

    def reserve_model(self, job, *, quota_key, tokens, rpm, tpm, concurrency):
        if min(tokens, rpm, tpm, concurrency) < 1 or tokens > tpm:
            raise ValueError("Invalid model quota")
        with self.connection() as db:
            self._lock(db, "model-admission")
            self._owned(db, job)
            db.execute(sql.SQL("""
                DELETE FROM {} WHERE NOT active
                AND created_at<clock_timestamp()-interval '70 seconds'
            """).format(self._reservations))
            db.execute(sql.SQL("""
                INSERT INTO {} (quota_key,rpm,tpm,concurrency) VALUES (%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """).format(self._quotas), (quota_key, rpm, tpm, concurrency))
            quota = db.execute(sql.SQL("""
                SELECT *,cooldown_until>clock_timestamp() AS cooling FROM {}
                WHERE quota_key=%s FOR UPDATE
            """).format(self._quotas), (quota_key,)).fetchone()
            if (quota["rpm"], quota["tpm"], quota["concurrency"]) != (rpm, tpm, concurrency):
                raise ValueError("Inconsistent shared model quota configuration")
            usage = db.execute(sql.SQL("""
                SELECT count(*) FILTER (WHERE created_at>clock_timestamp()-interval '60 seconds'
                    AND quota_key=%s) AS requests,
                COALESCE(sum(tokens) FILTER (WHERE created_at>clock_timestamp()-interval '60 seconds'
                    AND quota_key=%s),0) AS tokens,
                count(*) FILTER (WHERE active AND expires_at>clock_timestamp()) AS active,
                count(*) FILTER (WHERE active AND expires_at>clock_timestamp()
                    AND quota_key=%s) AS group_active FROM {}
                WHERE (quota_key=%s AND created_at>statement_timestamp()-interval '60 seconds')
                   OR (active AND expires_at>statement_timestamp())
            """).format(self._reservations), (quota_key, quota_key, quota_key, quota_key)).fetchone()
            if (quota["cooling"] or usage["requests"] >= rpm or usage["tokens"] + tokens > tpm
                    or usage["active"] >= self.model_concurrency
                    or usage["group_active"] >= concurrency):
                db.execute(sql.SQL("""
                    UPDATE {} SET model_wait_since=COALESCE(model_wait_since,clock_timestamp())
                    WHERE operation_id=%s
                """).format(self._jobs), (job["operation_id"],))
                return None
            db.execute(sql.SQL("UPDATE {} SET model_wait_since=NULL WHERE operation_id=%s").format(
                self._jobs), (job["operation_id"],))
            reservation = uuid4()
            db.execute(sql.SQL("""
                INSERT INTO {} (reservation_id,quota_key,operation_id,lease_token,tokens,expires_at)
                SELECT %s,%s,operation_id,lease_token,%s,lease_expires_at FROM {}
                WHERE operation_id=%s
            """).format(self._reservations, self._jobs),
                (reservation, quota_key, tokens, job["operation_id"]))
            return reservation

    def claim_model_call(self, job, *, used, maximum):
        with self.connection() as db:
            self._owned(db, job)
            row = db.execute(sql.SQL("""
                UPDATE {} SET model_calls=GREATEST(model_calls,%s)+1
                WHERE operation_id=%s AND GREATEST(model_calls,%s)<%s
                RETURNING model_calls
            """).format(self._jobs), (used, job["operation_id"], used, maximum)).fetchone()
            self._increment(db, "model_limits" if row is None else "model_calls")
            return row["model_calls"] if row else None

    def claim_model_retry(self, job, *, used, maximum):
        with self.connection() as db:
            self._owned(db, job)
            row = db.execute(sql.SQL("""
                UPDATE {} SET model_retries=GREATEST(model_retries,%s)+1
                WHERE operation_id=%s AND GREATEST(model_retries,%s)<%s
                RETURNING model_retries
            """).format(self._jobs), (used, job["operation_id"], used, maximum)).fetchone()
            self._increment(db, "model_retries" if row else "model_limits")
            return row["model_retries"] if row else None

    def reconcile_model(self, reservation, usage):
        known = (isinstance(usage, dict) and
                 all(type(usage.get(k)) is int and usage[k] >= 0
                     for k in ("inputTokens", "outputTokens")))
        tokens = usage["inputTokens"] + usage["outputTokens"] if known else None
        with self.connection() as db:
            db.execute(sql.SQL("""
                UPDATE {} SET active=FALSE,tokens=COALESCE(%s,tokens) WHERE reservation_id=%s
            """).format(self._reservations), (tokens, reservation))

    def model_cooldown(self, quota_key, seconds):
        with self.connection() as db:
            db.execute(sql.SQL("""
                UPDATE {} SET cooldown_until=GREATEST(COALESCE(cooldown_until,clock_timestamp()),
                    clock_timestamp()+(%s * interval '1 second')) WHERE quota_key=%s
            """).format(self._quotas), (seconds, quota_key))
            self._increment(db, "model_429")

    def metrics(self):
        with self.connection() as db:
            result = db.execute(sql.SQL("""
                SELECT count(*) FILTER (WHERE status='QUEUED') AS queued,
                count(*) FILTER (WHERE status='RUNNING' AND lease_expires_at>clock_timestamp()) AS running,
                count(*) FILTER (WHERE status='WAITING_REVIEW') AS waiting_review,
                count(*) FILTER (WHERE status='UNKNOWN') AS unknown,
                count(*) FILTER (WHERE status='RUNNING' AND lease_expires_at<=clock_timestamp())
                    AS expired_leases,
                COALESCE(max(EXTRACT(EPOCH FROM clock_timestamp()-created_at))
                    FILTER (WHERE status='QUEUED'),0)::float8 AS oldest_wait_seconds,
                COALESCE(max(EXTRACT(EPOCH FROM clock_timestamp()-running_since))
                    FILTER (WHERE status='RUNNING'),0)::float8 AS longest_running_seconds,
                count(*) FILTER (WHERE status='RUNNING' AND model_wait_since IS NOT NULL)
                    AS model_waiting,
                COALESCE(max(EXTRACT(EPOCH FROM clock_timestamp()-model_wait_since))
                    FILTER (WHERE status='RUNNING'),0)::float8 AS oldest_model_wait_seconds
                FROM {}
            """).format(self._jobs)).fetchone()
            age = max(result["oldest_wait_seconds"], result["oldest_model_wait_seconds"])
            result["wait_severity"] = ("critical" if age >= 120 else
                                       "warning" if age >= 30 else "ok")
            result["model_running"] = db.execute(sql.SQL("""
                SELECT count(*) AS n FROM {} WHERE active AND expires_at>clock_timestamp()
            """).format(self._reservations)).fetchone()["n"]
            result["counters"] = dict.fromkeys((
                "model_calls", "model_timeouts", "model_retries", "model_limits", "model_waits",
                "model_429", "lease_expiries", "lease_recoveries"), 0)
            result["counters"].update({row["name"]: row["value"] for row in db.execute(
                sql.SQL("SELECT name,value FROM {}").format(self._counters))})
            return result

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

    def retry_recovery(self, job, *, backoff_seconds=1):
        with self.connection() as db:
            self._owned(db, job)
            current = db.execute(sql.SQL("SELECT started FROM {} WHERE operation_id=%s").format(
                self._jobs), (job["operation_id"],)).fetchone()
            if not current["started"]:
                raise ValueError("Unstarted execution must use retry_unstarted")
            delay = min(300, backoff_seconds * 2 ** min(job["attempts"] - 1, 16))
            self._updated(db.execute(sql.SQL("""
                UPDATE {} SET status='QUEUED',code='RETRY_BACKOFF',
                worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                available_at=clock_timestamp()+(%s * interval '1 second'),
                updated_at=clock_timestamp() WHERE operation_id=%s
                AND lease_expires_at>clock_timestamp()
            """).format(self._jobs), (delay, job["operation_id"])))


@asynccontextmanager
async def configured_inbox():
    db_url = os.getenv("AGENT_INBOX_DB_URL", "")
    if not db_url:
        yield None
        return
    inbox = PostgresInbox(db_url)
    await run_in_threadpool(inbox.setup)
    yield inbox
