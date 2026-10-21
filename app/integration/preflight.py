"""Read-only task/BE contract check, independent of model and mutation Gateway."""
from app.case_loader import load_case_data, require_executable_case
from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.runtime.outcomes import safe_error_code


def configured_read_tools():
    return BusinessMcpReadClient(BusinessMcpSettings.from_env())


async def inspect_task(task, tools):
    report = {"taskId": str(task.task_id), "traceId": str(task.trace_id),
              "scope": "READ_ONLY", "executionAuthorized": False,
              "readContractReady": False, "status": "BLOCKED", "blockers": []}
    try:
        loaded = await load_case_data(task, tools)
        require_executable_case(loaded)
    except AgentToolError as error:
        report["blockers"] = [{"code": safe_error_code(error.code) or "INVALID_TOOL_RESPONSE",
                               "retryable": error.retryable}]
        return report
    report.update(status="READ_CONTRACT_READY", readContractReady=True)
    # No chat, policy original, credentials or model findings in a readiness report.
    return report
