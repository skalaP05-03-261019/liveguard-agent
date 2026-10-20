"""사건 조회 노드를 연결한다. 아직 미연결인 나머지 단계는 명시적으로 중단한다."""

from app.state import AgentState
from app.case_loader import load_case_data
from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings
from app.clients.ports import ReadTools


class IntegrationNotConfigured(RuntimeError):
    pass


async def load_case(state: AgentState, *, tools: ReadTools | None = None) -> dict:
    """작업 메타데이터·정책 버전·사건 스냅샷 로드."""
    client = tools if tools is not None else BusinessMcpReadClient(BusinessMcpSettings.from_env())
    return await load_case_data(state["task"], client)


async def investigate(state: AgentState, *, model=None, business=None, policy=None,
                      gate=None, present_data=None) -> dict:
    """AI1 read-only investigation; injectable #6 execution gate and #7 presentation."""
    from app.investigation.model import model_from_env
    from app.investigation.runner import run_investigation
    from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings

    if model is None:
        try:
            model = model_from_env()
        except ValueError:
            raise IntegrationNotConfigured("MODEL_PROVIDER/MODEL_NAME/MODEL_API_KEY 설정 필요") from None
    if business is None:
        business = BusinessMcpReadClient(BusinessMcpSettings.from_env())
    if policy is None:
        policy = RagMcpReadClient(RagMcpSettings.from_env())
    options = {"gate": gate}
    if present_data is not None:
        options["present_data"] = present_data
    return await run_investigation(state, model=model, business=business, policy=policy, **options)


async def validate_judgment(state: AgentState) -> dict:
    """Recheck every finding against the immutable case and verified policy evidence."""
    from app.investigation.validation import validate_analysis
    payload = {
        "caseId": state["case_id"], "snapshotId": state["snapshot_id"],
        "evidenceVersion": state["evidence_version"], "policyVersion": state["policy_version"],
        "messageFindings": [f.model_dump(mode="json", by_alias=True) if hasattr(f, "model_dump") else f
                            for f in state.get("message_findings", [])],
    }
    result = validate_analysis(state, payload)
    return {"message_findings": result.message_findings}


async def request_plan(state: AgentState) -> dict:
    """Backend에 판단·조치 후보를 보내 검증된 계획을 받는다."""
    raise IntegrationNotConfigured("Backend 계획 발급 API 연결 필요")


async def execute_plan(state: AgentState) -> dict:
    """발급된 명령·actionId·멱등 키만 사용한다. AI가 명령을 만들지 않는다."""
    raise IntegrationNotConfigured("Business MCP 실행 연결 필요")


async def verify_result(state: AgentState) -> dict:
    """UNKNOWN이면 기존 actionId를 조회. 부분 성공·통지 실패를 분리한다."""
    raise IntegrationNotConfigured("조치 조회·UNKNOWN 복구 연결 필요")


async def wait_review(state: AgentState) -> dict:
    """Harness checkpoint/interrupt 연결 후, 저장된 결정으로 같은 thread 재개."""
    raise IntegrationNotConfigured("Harness 저장·중단·재개 연결 필요")


async def report_result(state: AgentState) -> dict:
    """Backend 결과·이벤트 API 연결. 이 함수가 브라우저에 직접 SSE하지 않는다."""
    raise IntegrationNotConfigured("Backend 결과 저장·이벤트 연결 필요")
