"""Trusted deployment composition. BE adapter routes remain a separate contract."""
import importlib
import os
from contextlib import asynccontextmanager

from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings
from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.harness.checkpoint import CheckpointSettings, open_checkpointer
from app.integration.workflow import build_moderation_graph
from app.investigation.model import model_from_env
from app.runtime.runner import TaskRunner
from app.runtime.store import TaskStore


@asynccontextmanager
async def configured_runtime():
    # An explicit, server-owned async context manager supplies the real BE gateway.
    # No stub adapter or URL guessing turns readiness on.
    reference = os.getenv("AGENT_GATEWAY_FACTORY", "")
    if not reference:
        yield None
        return
    module, name = reference.split(":", 1)
    factory = getattr(importlib.import_module(module), name)
    store = TaskStore(os.environ["AGENT_TASK_DB_PATH"])
    business = BusinessMcpReadClient(BusinessMcpSettings.from_env())
    policy = RagMcpReadClient(RagMcpSettings.from_env())
    model = model_from_env()
    async with open_checkpointer(CheckpointSettings.from_env().db_url) as saver, factory() as gateway:
        graph = build_moderation_graph(model=model, business=business, policy=policy,
                                       backend=gateway, checkpointer=saver)
        yield TaskRunner(store, graph)
