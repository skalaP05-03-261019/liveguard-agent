"""Execution-context guards; lease identity never enters Graph state or logs."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row

from app.runtime.inbox import LeaseLost

current_lease = ContextVar("current_worker_lease", default=None)


class LeaseGuard:
    def __init__(self, inbox, job):
        self.inbox, self.job = inbox, job
        self.lost = False

    async def check(self):
        if self.lost:
            raise LeaseLost()
        try:
            await asyncio.to_thread(self.inbox.assert_owned, self.job)
        except Exception:
            self.lost = True
            raise LeaseLost() from None

    async def mutation(self, action_id=None):
        await self.check()
        # Only an action reference is retained, never tool arguments or commands.
        action = str(UUID(str(action_id))) if action_id is not None else None
        await asyncio.to_thread(self.inbox.mark_mutation, self.job, action)
        self.job["mutation_started"] = True
        if action:
            self.job["action_id"] = action


async def check_execution():
    guard = current_lease.get()
    if guard is not None:
        await guard.check()


def guarded_node(fn):
    @wraps(fn)
    async def run(state):
        await check_execution()
        result = await fn(state)
        await check_execution()
        return result
    return run


class GuardedGateway:
    """Guard every trusted BE boundary, including two calls within one node."""
    MUTATIONS = frozenset({"create_review", "submit_analysis", "apply_context", "revalidate", "kick"})

    def __init__(self, gateway):
        self._gateway = gateway

    def __getattr__(self, name):
        method = getattr(self._gateway, name)

        async def invoke(*args, **kwargs):
            await check_execution()
            guard = current_lease.get()
            if guard is not None and name in self.MUTATIONS:
                action = args[0].get("actionId") if name == "kick" and args else None
                await guard.mutation(action)
            result = await method(*args, **kwargs)
            await check_execution()
            return result
        return invoke


class FencedPostgresSaver(AsyncPostgresSaver):
    """Serialize checkpoint writes with lease transfer in the SAME database.

    Checking before aput alone is racy. Row lock, validation and checkpoint writes
    share one short PostgreSQL transaction; no Graph/model/MCP runs inside it.
    """
    async def setup(self):
        # Upstream migrations include CREATE INDEX CONCURRENTLY (autocommit).
        # Serialize worker first boots without putting migrations in a transaction.
        lock = "SELECT pg_advisory_{}(hashtextextended(current_schema() || ':worker-checkpoints',0))"
        await self.conn.execute(lock.format("lock"))
        self._initializing = True
        try:
            await super().setup()
        finally:
            self._initializing = False
            await self.conn.execute(lock.format("unlock"))

    @asynccontextmanager
    async def _cursor(self, *, pipeline=False):
        if getattr(self, "_initializing", False):
            async with super()._cursor(pipeline=pipeline) as cur:
                yield cur
            return
        if pipeline and current_lease.get() is None:
            raise LeaseLost()
        async with self.lock, self.conn.transaction():
            async with self.conn.cursor(binary=True, row_factory=dict_row) as cur:
                await cur.execute("SET LOCAL statement_timeout = '5s'")
                await cur.execute("SET LOCAL lock_timeout = '5s'")
                guard = current_lease.get()
                await self._check_cursor_owned(cur, guard)
                yield cur
                await self._check_cursor_owned(cur, guard)

    async def _check_cursor_owned(self, cur, guard):
        if guard is not None:
            if guard.lost:
                raise LeaseLost()
            await cur.execute(guard.inbox.ownership_sql(),
                              guard.inbox.ownership_params(guard.job))
            if await cur.fetchone() is None:
                guard.lost = True
                raise LeaseLost()

    def _check_thread(self, config):
        guard = current_lease.get()
        if guard is None or config["configurable"]["thread_id"] != guard.job["thread_id"]:
            raise LeaseLost()

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._check_thread(config)
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        self._check_thread(config)
        await super().aput_writes(config, writes, task_id, task_path)
