"""노드 경계만 정의한다. 미연결 기능은 성공으로 위장하지 않고 중단한다."""

from app.state import AgentState


class IntegrationNotConfigured(RuntimeError):
    pass


async def load_case(state: AgentState) -> dict:
    """작업 메타데이터·정책 버전·사건 스냅샷 로드."""
    raise IntegrationNotConfigured("Backend/MCP 사건 조회 연결 필요")


async def investigate(state: AgentState) -> dict:
    """모델이 조회 도구를 선택하는 유한 루프. 변경 도구는 노출하지 않는다."""
    raise IntegrationNotConfigured("모델 + 조회 MCP + 공통 실행 예산 연결 필요")


async def validate_judgment(state: AgentState) -> dict:
    """JSON 검증 외에 실제 조회한 증거 ID·정책 버전·허용 조치를 검사."""
    raise IntegrationNotConfigured("조회 근거와 판단 출력의 대조 검증 구현 필요")


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
