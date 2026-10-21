"""Offline inbox reconciliation. Default dry-run; --apply changes Agent status only.

python -m app.recover_tasks --task-id UUID [--apply]
Use the SAME task volume/checkpoint DB and Graph version as the worker. Workers
must run the shared-lock version before this tool is used; stop older workers.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from app.harness.checkpoint import CheckpointSettings, open_checkpointer
from app.integration.workflow import build_moderation_graph
from app.runtime.recovery import reconcile_task
from app.runtime.store import MaintenanceBusy, TaskStore


async def inspect(args):
    path = os.getenv("AGENT_TASK_DB_PATH", "")
    if not path or not Path(path).is_file():
        return {"code": "TASK_STORE_NOT_FOUND", "applied": False}
    async with open_checkpointer(CheckpointSettings.from_env().db_url, initialize=False) as saver:
        # Only aget_state is used. No model, business tools or Gateway is composed.
        graph = build_moderation_graph(model=None, business=None, policy=None,
                                       backend=None, checkpointer=saver)
        return await reconcile_task(TaskStore(path), graph, args.task_id, apply=args.apply)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = asyncio.run(inspect(args))
    except MaintenanceBusy:
        report = {"code": "RECOVERY_WORKER_ACTIVE", "applied": False}
    except Exception:
        # DB exception messages/DSNs may contain credentials. Print safe metadata only.
        report = {"code": "RECOVERY_UNAVAILABLE", "applied": False}
    print(json.dumps(report, ensure_ascii=False))
    if report.get("proposedStatus") == "UNKNOWN" or report.get("code") in {
        "TASK_STORE_NOT_FOUND", "TASK_NOT_FOUND", "RECOVERY_WORKER_ACTIVE", "RECOVERY_UNAVAILABLE"
    } or report.get("recoveryCode") == "CHECKPOINT_UNAVAILABLE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
