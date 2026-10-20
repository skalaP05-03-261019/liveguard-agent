# LIVE:GUARD Agent 골격

김태관 / ⑤ AI 1·Agent의 Python 서비스 시작 구조. 현재 범위는 **API·스키마·State·Graph 경계**다.
Agent의 작업 입력 스키마와 Business MCP 조회 클라이언트, `load_case`를 구현했다.
실제 모델 판단·RAG 연결·전체 작업 실행·작업 저장·제재·checkpoint/resume는 미구현이다.

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

## RAG 정책 색인 (AI2, #3)

운영정책 v3.0 원문(`app/rag_mcp/policies/policy-v3.0.md`, 노션 Export)을 조항 ID(`CP/SP/MP/MM-x.y`) 헤딩 단위로 나눠 OpenAI 임베딩과 함께 pgvector `rag.policy_chunks`에 넣는다. 장 제목 등 조항 밖 텍스트는 색인하지 않는다.

```bash
cp .env.example .env   # RAG_DB_URL, OPENAI_API_KEY 입력. .env는 커밋하지 않는다.
uv run --env-file .env python -m app.rag_mcp.index
```

같은 `POLICY_VERSION`으로 다시 실행하면 해당 버전 행을 한 트랜잭션으로 교체한다. 정책을 고칠 때는 노션 수정 → 버전 올림 → Export한 파일을 `policy-v{version}.md`로 추가 → 재색인한다. pgvector 통합 테스트는 `RAG_TEST_DB_URL`이 있을 때만 실행되며 임시 버전으로 색인한 뒤 지운다.

## RAG MCP 서버 (AI2, #4)

Business MCP와 같은 구조(FastAPI `/mcp`, Streamable HTTP, Bearer 토큰)로 `search_policy`, `get_policy_section` 도구와 조항 원문 Resource `policy://{version}/{documentId}/{sectionId}`를 제공한다. 모두 조회 전용이다.

```bash
# .env에 RAG_DB_URL, OPENAI_API_KEY, RAG_MCP_AGENT_TOKEN 설정 후
uv run --env-file .env python -m app.rag_mcp   # http://127.0.0.1:8003/mcp
```

`/health`는 색인된 policyVersion 목록과 준비 여부만 보고한다. 도구 입출력·오류 코드는 [docs/AI2-01-rag-mcp-contract.md](docs/AI2-01-rag-mcp-contract.md)를 따른다.

## Harness: 강퇴 승인 interrupt/resume (AI2, #5)

`verify_result` 뒤 `review_kick → execute_kick`이 Backend의 강퇴 심사 요청(`kick_review`)이 있을 때만 Graph를 멈추고(`interrupt`) Postgres checkpointer(`agent` 스키마, `AGENT_CHECKPOINT_DB_URL`)에 저장한다. 운영자 결정 후 `POST /agent/tasks/{taskId}/resume`(`reviewKind: KICK`)이 같은 `threadId`로 재개하고, Backend 재검증이 승인일 때만 `kick_user`를 호출한다. 중복 승인은 409로 막는다. 실행기 연결 전에는 `/resume`이 501이다.

Postgres 재시작 복원 테스트는 `AGENT_TEST_CHECKPOINT_DB_URL`이 있을 때만 실행된다(임시 스키마 생성 후 삭제). 계약: [docs/AI2-02-harness-kick-approval-contract.md](docs/AI2-02-harness-kick-approval-contract.md)

## 파일과 다음 작업

| 파일 | 역할 / 다음 작업 |
|---|---|
| app/schemas.py | Notion 작업 입력·메시지별 AnalysisResult. 기존 JudgmentOutput은 미연결 Graph의 legacy 모델 |
| app/state.py | 조사·실행 상태; Harness 담당과 저장 형식 합의 |
| app/graph.py | 초기 분석 단계·판단 분기; 각 노드는 아직 연결 필요 오류 발생 |
| app/nodes.py | load_case 조회 연결 완료. investigate 이후 단계는 아직 미구현 |
| app/clients/ports.py | Backend·조회 MCP·실행 MCP 인터페이스 초안 |
| app/clients/business_mcp.py | 인증된 조회 전용 MCP 클라이언트, 도구당 10초 제한, 자동 재시도 없음 |
| app/case_loader.py | 사건/대화의 snapshot·evidence 검증 및 TARGET/CONTEXT State 저장 |
| app/business_mcp/ | BE2-01 Business MCP 도구, Backend API adapter 및 독립 실행 ASGI app |
| app/rag_mcp/ | 운영정책 조항 파서·pgvector 색인, RAG MCP 서버(search_policy, get_policy_section, policy:// Resource) |
| app/harness/ | Postgres checkpointer, 강퇴 승인 interrupt/resume 노드와 재개 함수 (#5) |
| prompts/investigator.md | 조사 규칙 초안; 아직 모델에 연결되지 않음 |
| tests/fixtures/agent-case-v0.5.json | 이번 조회 연결용 요청·응답 제안. 가상 정책 버전이며 활성 정책 아님 |
| tests/fixtures/D01.json | 역사 자료인 v0.3 fixture. 현재 TaskEnvelope 입력과 호환되지 않음 |

BE2-01 서버 계약 초안은 [Business MCP 계약](docs/BE2-01-agent-business-mcp-contract.md), 이번 Agent 소비자 구현의 필수 응답·호환성은 [AI1-01 조회 계약](docs/AI1-01-mcp-read-contract.md)을 참고한다. Backend 합의 전 구현 제안이며 실제 Spring 연동 완료를 뜻하지 않는다.

## 조회 연결 검증

```bash
uv run --locked python -m unittest discover -s tests -v
# TCP 포트를 열 수 있는 환경에서 동일 테스트를 실제 loopback HTTP로 실행:
LIVEGUARD_TEST_TCP=1 uv run --locked python -m unittest discover -s tests -v
```

기본 테스트는 실제 Business MCP ASGI app·인증·MCP SDK/HTTP 직렬화를 사용하며 Backend만 가상이다. TCP 모드는 Uvicorn을 임시 로컬 포트에 띄운다. 테스트 범위와 결과는 [검증 기록](evidence/agent-read-check.json)에 구분한다.
`load_case`는 Graph 노드와 직접 호출로 연결했지만 `/run`에는 전체 실행기가 아직 연결되지 않았다.
조회 시 `BUSINESS_MCP_URL`, `BUSINESS_MCP_AGENT_TOKEN`을 process environment에서 읽는다. `.env` 자동 로드는 하지 않는다.

Graph: load_case → investigate → validate_judgment → 정상 보고 / 검토 대기 / 계획 요청 → 실행 → 확인 → 보고.
검토 노드는 현재 실행 불가이며 실제 interrupt·결정 재검증·재개 분기는 후속 구현한다.

다음 목표는 **실제 모델의 도구 선택 → 조회/RAG → 근거 검증 → 메시지별 판단**이다.
fixture 전체는 HTTP 요청이 아니다. POST에는 새 fixture의 `task`만 사용한다. 모델 연결 시 seed나 평가 정답을 도구에서 얻은 사실처럼 넘기지 않는다.

## 기준·미확정

- 기준: [팀 Notion 통합 문서](https://app.notion.com/p/3fe61f4176b580af9a07c5a10a9e6745)의 PROJECT/POLICY/CONTRACTS/ARCHITECTURE. 각 문서의 미확정 표시는 유지한다.
- 입력은 snapshotId·expectedEvidenceVersion 기반이다. 구 expectedCaseVersion·attempt·NOTIFY는 더 이상 받지 않는다.
- AnalysisResult는 5개 유형의 메시지별 판정을 표현하며 제재 기간은 포함하지 않는다. 기존 단일 판단/300초 후보는 아직 미구현인 legacy Graph에만 남아 있으며 다음 PR에서 교체한다.
- 활성 정책 버전·기간표, 실제 Backend 응답/결과 통지, 모델·RAG·Harness 연결은 합의/후속 구현 대상이다.
- Agent의 Business MCP 접속은 연결했으며 실제 Spring 내부 API 계약과의 통합 검증은 남아 있다.
- 수업 PDF 실제 72쪽은 변경 전 승인을 요구하지만 기획서는 최초 자동 조치다.
  현재 실행 기능은 없으며, 구현 전 팀·강사 확인 결과에 맞춰 실행 직전 경계를 확정한다.
- PDF 74쪽 Agent 설계·구현: Graph/State, 모델 도구 선택·근거, 저장·재개·진행 이벤트 증거가 필요하다.
  현재 골격이나 import 확인을 해당 기능의 완료 증거로 사용하지 않는다.

기존 골격 검증은 evidence/scaffold-check.json, 이번 조회 연결 검증은 evidence/agent-read-check.json에 기록한다.

분리 전 원본: taetegwgn/skala-capstone-P05_03의 agent/ (10a8e5d).
이전 [Backend docs/live-guard/v0.3](https://github.com/skalaP05-03-261019/liveguard-be/tree/main/docs/live-guard/v0.3)는 역사 자료로 보존합니다. 최신 프로젝트 기준은 위 Notion 통합 문서입니다.

[Backend](https://github.com/skalaP05-03-261019/liveguard-be) · [Frontend](https://github.com/skalaP05-03-261019/liveguard-fe)
