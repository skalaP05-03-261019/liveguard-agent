"""Run with `python -m app.worker`; API-only composition remains app.main."""
import asyncio
from contextlib import asynccontextmanager
import importlib
import os
import signal
import sys

from psycopg import sql

from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings
from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.harness.checkpoint import CheckpointSettings, open_checkpointer
from app.integration.workflow import build_moderation_graph
from app.investigation.model import model_from_env
from app.runtime.fencing import FencedPostgresSaver
from app.runtime.inbox import PostgresInbox
from app.runtime.worker import GraphWorker, WorkerSettings


@asynccontextmanager
async def configured_worker():
    settings = WorkerSettings.from_env()
    inbox = PostgresInbox(os.environ["AGENT_INBOX_DB_URL"])
    await asyncio.to_thread(inbox.setup)
    reference = os.environ["AGENT_GATEWAY_FACTORY"]
    module, name = reference.split(":", 1)
    factory = getattr(importlib.import_module(module), name)
    business = BusinessMcpReadClient(BusinessMcpSettings.from_env())
    policy = RagMcpReadClient(RagMcpSettings.from_env())
    model = model_from_env()
    async with open_checkpointer(CheckpointSettings.from_env().db_url,
                                 saver_type=FencedPostgresSaver) as saver, factory() as gateway:
        # Fencing requires checkpoint + inbox writes in one database transaction.
        def identity():
            with inbox.connection() as db:
                return tuple(db.execute(
                    "SELECT current_database(),pg_postmaster_start_time()").fetchone().values())
        expected = await asyncio.to_thread(identity)
        async with saver._cursor() as cur:
            await cur.execute("SELECT current_database(),pg_postmaster_start_time()")
            actual = tuple((await cur.fetchone()).values())
            if actual != expected:
                raise ValueError("Worker inbox and checkpoints must share one PostgreSQL database")
            await cur.execute(sql.SQL("SELECT lease_token FROM {} LIMIT 0").format(inbox._jobs))
        for method in ("create_review", "get_decision", "submit_analysis", "apply_context",
                       "revalidate", "kick", "read_case", "read_action"):
            if not callable(getattr(gateway, method, None)):
                raise ValueError("Worker gateway recovery capability missing")
        graph = build_moderation_graph(model=model, business=business, policy=policy,
                                      backend=gateway, checkpointer=saver)
        worker = GraphWorker(inbox, graph, gateway=gateway, settings=settings)
        try:
            yield worker
        finally:
            await worker.close()


async def main():
    try:
        async with configured_worker() as worker:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, worker.stop_claiming)
            try:
                worker.start()
                print("WORKER_READY", flush=True)
                stopped = asyncio.create_task(worker.stopping.wait())
                try:
                    await asyncio.wait((worker.worker, stopped), return_when=asyncio.FIRST_COMPLETED)
                    await worker.close()
                finally:
                    stopped.cancel()
                    await asyncio.gather(stopped, return_exceptions=True)
            finally:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
    except Exception:
        # Never expose DSNs, exception strings, model output or tool arguments.
        print("WORKER_NOT_READY: WORKER_RUNTIME_UNAVAILABLE", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
