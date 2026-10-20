# LIVE:GUARD Agent 골격

김태관 / ⑤ AI 1·Agent의 Python 서비스 시작 구조. 현재 범위는 **API·스키마·State·Graph 경계**다.
Agent 작업 실행기의 실제 모델 판단·MCP/Backend 연결, 작업 저장, 제재, checkpoint/resume는 미구현이다.

## 실행

```bash
cd liveguard-agent
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

`http://127.0.0.1:8001/docs`에서 입력 형식을 확인한다. `/health`는 `readyForTasks: false`다.
run/resume는 유효한 요청에도 **501**을 반환한다. 작업을 저장하지 않으므로 접수 완료 202를 반환하지 않는다.
현재 인증이 없으므로 로컬 개발 주소만 사용한다.

## Business MCP 개발 서버 (BE2-01)

Business MCP는 FastAPI 호스트의 `/mcp`에 Streamable HTTP MCP를 제공한다.
Backend 내부 API와 Agent 인증 토큰이 설정되지 않으면 MCP 요청을 받을 수 없다.

```bash
BUSINESS_MCP_AGENT_TOKEN='replace-with-agent-token' \
BUSINESS_MCP_SERVICE_TOKEN='replace-with-backend-service-token' \
uv run python -m app.business_mcp
```

`BUSINESS_BACKEND_BASE_URL`은 기본 `http://127.0.0.1:8080`이다. 서비스는 Backend `/internal/**` API를 호출하며 업무 DB에 직접 연결하지 않는다. `/health`는 credential 설정 여부만 보고하고 비밀값을 노출하지 않는다. `BUSINESS_MCP_URL`은 Agent 측 MCP 접속 URL용이며, 서버 listen 설정은 `BUSINESS_MCP_HOST`와 `BUSINESS_MCP_PORT`다.
이 서버는 process environment를 읽는다. `.env.example`은 설정 참고 파일이며 자동 로드되지 않는다.

## 파일과 다음 작업

| 파일 | 역할 / 다음 작업 |
|---|---|
| app/schemas.py | camelCase JSON, 작업 입력·판단·후보 조치 검증 |
| app/state.py | 조사·실행 상태; Harness 담당과 저장 형식 합의 |
| app/graph.py | 초기 분석 단계·판단 분기; 각 노드는 아직 연결 필요 오류 발생 |
| app/nodes.py | 단계별 구현 위치. 첫 작업은 load_case와 investigate |
| app/clients/ports.py | Backend·조회 MCP·실행 MCP 인터페이스 초안 |
| app/business_mcp/ | BE2-01 Business MCP 도구, Backend API adapter 및 독립 실행 ASGI app |
| prompts/investigator.md | 조사 규칙 초안; 아직 모델에 연결되지 않음 |
| tests/fixtures/D01.json | 기존 개발 입력의 D01. 정답은 포함하지 않음 |

BE2-01 Business MCP와 Agent 사이의 업무 데이터 계약 초안은 [docs/BE2-01-agent-business-mcp-contract.md](docs/BE2-01-agent-business-mcp-contract.md)에서 관리한다. BE1·AI1 합의 전 제안이며 구현 규격은 아니다.

Graph: load_case → investigate → validate_judgment → 정상 보고 / 검토 대기 / 계획 요청 → 실행 → 확인 → 보고.
검토 노드는 현재 실행 불가이며 실제 interrupt·결정 재검증·재개 분기는 후속 구현한다.

첫 목표는 D01에서 **실제 모델의 도구 선택 → mock 응답 조회 → 근거 검증 → 판단**이다.
fixture는 HTTP 요청 자체가 아니다. POST에는 taskEnvelope만 넣고 모델에는 선택한 도구 응답만 제공한다.
seedMetadata·seedMessages를 도구에서 얻은 사실처럼 모델에 넘기지 않는다.

## 기준·미확정

- 기준: main 문서 v0.3, 정책 0.3-draft, 로컬 Agent 연결 계약 v0.1. 출력·포트는 팀 검토용이다.
- 0.4-review의 5개 유형·이력 기반 최대 7일 규칙은 반영하지 않았다.
- P1~P6 중 출력 형식, task context, 전체 사건 증거, 이벤트, Harness 연결은 팀 합의 필요.
- Agent runner의 모델·MCP 접속 설정과 Backend 내부 API 계약은 다음 작업이다. Business MCP 서버의 env 설정은 구현됐지만, Agent의 MCP 접속은 아직 연결되지 않았다.
- 수업 PDF 실제 72쪽은 변경 전 승인을 요구하지만 기획서는 최초 자동 조치다.
  현재 실행 기능은 없으며, 구현 전 팀·강사 확인 결과에 맞춰 실행 직전 경계를 확정한다.
- PDF 74쪽 Agent 설계·구현: Graph/State, 모델 도구 선택·근거, 저장·재개·진행 이벤트 증거가 필요하다.
  현재 골격이나 import 확인을 해당 기능의 완료 증거로 사용하지 않는다.

실행 검증 범위는 evidence/scaffold-check.json에 기록한다.

분리 전 원본: taetegwgn/skala-capstone-P05_03의 agent/ (10a8e5d).
공통 기획서·정책은 [Backend의 docs/live-guard/v0.3](https://github.com/skalaP05-03-261019/liveguard-be/tree/main/docs/live-guard/v0.3)에서 관리합니다.

[Backend](https://github.com/skalaP05-03-261019/liveguard-be) · [Frontend](https://github.com/skalaP05-03-261019/liveguard-fe)
