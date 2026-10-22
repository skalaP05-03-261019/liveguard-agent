> 2026-10-21 BE #40 후속: [Agent 담당 경계와 조회 전용 인수 검사](docs/AI1-54-be40-owner-boundary.md). BE 저장소를 추가 수정하지 않고 소비자 호환성·응답 정합성을 검증합니다. #40의 BE 정본 통합은 담당자 작업으로 유지합니다.

# LIVE:GUARD Agent 골격

김태관 / AI1 Agent의 조사·정책 검증·승인 흐름 연결 서비스다.
Business/RAG MCP 조회와 실제 OpenAI 검증 결과는 각 이슈의 증거와 문서를 참조한다.

## Phase 2 · 공유 기획과 태관 후보 판별 개발

[후보 API·BE 사건 검증·세원 조사 인계 계약/예제](docs/phase2/handoff-63.md)를 추가했습니다. 독립 서비스이며 기존 Worker와 승인 Graph에는 아직 연결하지 않습니다.

[기획·역할·미정 사항](docs/phase2/README.md)과 [후보 판별/평가기 실행법](docs/phase2/implementation-61.md)을 추가했습니다. 새 모듈은 독립 실험용이며 기본 서비스 API·조사/승인·공유 Inbox/Worker에 연결하지 않습니다. 요약 기술은 세원님 담당으로 진행하되 이용 역할·시점은 추후 결정합니다. 실험용 신호 선택을 고객사 정책 확정이나 실제 제재로 해석하지 않습니다.

## #10 통합 진행

2026-10-21 BE OpenAPI v1(A-9·A-16)에 따른 **PII NEEDS_REVIEW → 기존 CONTEXT 검토 interrupt**와 판정 제출 HTTP 어댑터는 [#46 변경·검증 범위](docs/AI1-46-openapi-v1-pii-context.md)를 참조한다. 판정 제출 어댑터와 전체 업무 Gateway 준비는 구분한다.

최신 Notion 문맥 승인 정책과 AI2 PR #25를 연결한 별도 실행 Graph를
`app.integration.workflow.build_moderation_graph`에 추가했다. CONTEXT 대기·보류·재개 후
Backend 처리와 별도 KICK 대기를 연결하며 공유 실행 예산/마스킹을 사용한다.
HTTP 실행기·영속 inbox·서비스 인증은 [#10 실행기 문서](docs/AI1-10-task-runtime.md)에 구현했다. 실제 HTTP Gateway의 구현·설정·검증 범위는 [#48](docs/AI1-48-http-gateway.md)을 참조한다. 기본 배포에서는 명시적으로 활성화해야 한다.
최신 BE 사건 입력 사전검사와 중단 작업 복구는 [#40 운영 준비 문서](docs/AI1-40-backend-preflight-recovery.md)를 참조한다. 사전검사는 모델·Gateway 없이도 가능하며 실행 승인을 대신하지 않는다.
실제 B1 진행 이벤트 계약용 `BackendEvents` HTTP 어댑터는 추가했으며, 전체 Spring 집행 시험과 구분한다.
[최신 명세 출처·Backend 잔여 계약](docs/AI1-10-latest-spec-and-boundary.md)을 먼저 확인한다.

## 실행

다중 API 접수는 [#58 공유 Inbox 모드](docs/AI1-10-task-runtime.md)를 사용한다.
`AGENT_INBOX_DB_URL`과 `AGENT_SERVICE_TOKEN`을 설정하면 같은 PostgreSQL Inbox에
접수·중복 판별·상태 조회를 수행하고, API 프로세스는 Graph를 시작하지 않는다.
공유 Inbox의 실행 Worker/lease는 후속 범위이므로 이 모드만으로 검사가 실행되지는 않는다.
미설정 시 기존 로컬 SQLite+Graph 실행 모드를 유지한다.

```bash
cd liveguard-agent
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

`http://127.0.0.1:8001/docs`에서 입력 형식을 확인한다. `/health`는 `readyForTasks: false`다.
기본 구성에서는 Gateway/인증 설정이 없어 run/resume는 **503**으로 차단한다. 실제 Gateway가 주입된 runtime은 Bearer 인증 후 작업을 영속 저장하고 202를 반환한다. 결과는 GET `/agent/tasks/{taskId}`로 조회한다.
Agent는 BE 전용 Bearer 인증을 사용하며 브라우저 로그인과 구분한다. 내부 서비스 주소로만 노출한다.

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

최신 BE 영속 사건 연결 점검과 운영자 카드 근거 필드는 [연동 진단·검토 근거](docs/AI1-35-operator-evidence-backend-readiness.md)를 참조한다. 조회 연결 성공과 전체 Agent 승인/집행 준비는 구분한다.

운영정책 원문(`app/rag_mcp/policies/policy-v{version}.md`, 노션 Export, 기본 v3.1)을 조항 ID(`CP/SP/MP/MM-x.y`) 헤딩 단위로 나눠 OpenAI 임베딩과 함께 pgvector `rag.policy_chunks`에 넣는다. 장 제목 등 조항 밖 텍스트는 색인하지 않는다.

두 버전 환경변수를 명시하면 정확히 일치해야 하며 다르면 시작을 거부한다. 색인 설정의 `POLICY_VERSION`이 미설정이거나 빈 문자열이면 `app/rag_mcp/config.py`의 `LIVEGUARD_POLICY_VERSION`(TBD-08 공유 변수), 그다음 `app/rag_mcp/config.py`의 `DEFAULT_POLICY_VERSION`(현재 `v3.1`, BE OpenAPI v1.0 `policyVersion`과 같은 이름표)을 사용하고, 명시한 값은 그대로 사용한다. 이 기본값이 배포 활성 정책을 확정하지는 않는다. Agent 조회·판정은 Backend 사건 응답의 `policyVersion`을 따르므로 해당 버전이 실제로 색인되어 있어야 한다. 통합 배포에서는 팀이 정한 버전을 명시하고 사건 버전·색인 버전의 일치를 확인한다.

```bash
cp .env.example .env   # RAG_DB_URL, OPENAI_API_KEY 입력. .env는 커밋하지 않는다.
uv run --env-file .env python -m app.rag_mcp.index
```

같은 `POLICY_VERSION`으로 다시 실행하면 해당 버전 행을 한 트랜잭션으로 교체한다. 정책을 고칠 때는 노션 수정 → 버전 올림 → Export한 파일을 `policy-v{version}.md`로 추가 → 재색인한다. pgvector 통합 테스트는 `RAG_TEST_DB_URL`이 있을 때만 실행되며 임시 버전으로 색인한 뒤 지운다.

검색 정확도 평가(#28): `uv run --env-file .env python -m app.rag_mcp.evaluate`. v3.1 질문 24개 기준 정답 조항 4위 안 적중 92%(유형 지정 96%), 1위 적중 58%. 결과와 해석은 [docs/AI2-01-rag-mcp-contract.md](docs/AI2-01-rag-mcp-contract.md) 9절, 원자료는 `evidence/rag-retrieval-eval-v3.1.json`.

## RAG MCP 서버 (AI2, #4)

Business MCP와 같은 구조(FastAPI `/mcp`, Streamable HTTP, Bearer 토큰)로 `search_policy`, `get_policy_section` 도구와 조항 원문 Resource `policy://{version}/{documentId}/{sectionId}`를 제공한다. 모두 조회 전용이다.

```bash
# .env에 RAG_DB_URL, OPENAI_API_KEY, RAG_MCP_AGENT_TOKEN 설정 후
uv run --env-file .env python -m app.rag_mcp   # http://127.0.0.1:8003/mcp
```

`/health`는 색인된 policyVersion 목록과 준비 여부만 보고한다. 도구 입출력·오류 코드는 [docs/AI2-01-rag-mcp-contract.md](docs/AI2-01-rag-mcp-contract.md)를 따른다.

## Harness: 강퇴 승인 interrupt/resume (AI2, #5)

`verify_result` 뒤 `review_kick → execute_kick`이 Backend의 강퇴 심사 요청(`kick_review`)이 있을 때만 Graph를 멈추고(`interrupt`) Postgres checkpointer(`agent` 스키마, `AGENT_CHECKPOINT_DB_URL`)에 저장한다. 운영자 결정 후 `POST /agent/tasks/{taskId}/resume`(`reviewKind: KICK`)이 같은 `threadId`로 재개하고, Backend 재검증이 승인일 때만 `kick_user`를 호출한다. HTTP 재개는 저장 후 비동기 실행한다. 같은 decisionId의 동일 전달은 200/duplicate로 중복 실행하지 않고, 다른 입력은 409다. Graph 자체의 중복 재개 거부도 유지한다.

Postgres 재시작 복원 테스트는 `AGENT_TEST_CHECKPOINT_DB_URL`이 있을 때만 실행된다(임시 스키마 생성 후 삭제). 계약: [docs/AI2-02-harness-kick-approval-contract.md](docs/AI2-02-harness-kick-approval-contract.md)

## Harness: 문맥 검토 interrupt/resume (AI2, #24)

`handling_route`가 `CONTEXT_REVIEW`면 `request_context_review → review_context → apply_context_decision`이 Backend에 검토 요청(reviewId)을 만들고, AI 제안이 정상이어도 운영자 결정 전까지 멈춘다. `/resume`(`reviewKind: CONTEXT`)으로 같은 `threadId`를 재개하면 Backend 저장본의 결정(CONFIRM_NORMAL / CONFIRM_VIOLATION / DEFER)을 `context_result`에 남기고, 보류면 다시 기다린다. 심사 종류가 다른 재개는 비동기 실행 중 `REVIEW_KIND_MISMATCH`로 거부하고 WAITING_REVIEW를 유지한다. Graph 연결과 결정 후 처리는 #10. 계약: [docs/AI2-06-harness-context-review-contract.md](docs/AI2-06-harness-context-review-contract.md)

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
| app/clients/rag_mcp.py | RAG 전용 인증·조회 허용 목록·버전/응답 검사·원문 Resource 읽기 |
| app/policy_evidence.py | 검색→조항→Resource 일치 검사 후 policy_evidence State patch 반환 |
| app/check_rag.py | 제공자 준비 후 사용하는 실제 서버 연결·출처 검증 명령 |
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
`load_case`와 통합 Graph는 HTTP 실행기에서 조립한다. 실제 Gateway는 [#48](docs/AI1-48-http-gateway.md)에 구현했으며 기본 배포에서는 비활성이다.
조회 시 `BUSINESS_MCP_URL`, `BUSINESS_MCP_AGENT_TOKEN`을 process environment에서 읽는다. `.env` 자동 로드는 하지 않는다.

## RAG 소비자 연결 (#8)

[AI1-08 계약](docs/AI1-08-rag-read-contract.md)에 입력·응답·원문 검증·오류·제공자 확인 항목을 기록했다.
`RAG_MCP_URL`과 별도 `RAG_MCP_AGENT_TOKEN`을 process environment에서 읽고, 사건의 정책 버전으로만 조회한다.
검색 결과·전체 조항·Resource의 ID/URI/버전/본문이 맞는 경우만 근거 State를 반환한다.
정책 버전과 서버 주소를 추정하지 않으며 빈 결과·오류를 정상 판정으로 바꾸지 않는다.
제공자 #13은 단일 text/markdown 본문을 조항 text와 정확히 같게 제공한다. 현재 wire 정책 버전은 `3.0`이며, 소비자는 계속 사건의 정책 버전을 따른다.
기존 계약 대역 테스트에 더해 실제 `app.rag_mcp.main.create_app`과 연결하는 통합 테스트를 추가했다.
이 테스트는 제어된 임베딩을 사용한다. CI는 임시 pgvector DB를 제공해 실제 SQL 검색→도구→Resource→소비자 State도 확인한다. OpenAI의 실제 임베딩 품질 검증과 구분한다.

실제 서버 준비 후 다음 명령으로 확인한다. 토큰은 환경에만 설정하고 명령 결과/문서에 넣지 않는다.

```bash
uv run --locked python -m app.check_rag \
  --policy-version '<제공자와 확인한 정확한 버전>' \
  --query '욕설 판단 조건과 정상 예외' --top-k 1
```

이 명령은 성공한 경우 정책 ID·버전·URI·원문 해시를 출력한다. 모델 조사 루프는 #9에서 이 소비자 경계와 연결한다.

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


## Agent 조사·판정 (#9)

#12 기반의 읽기 전용 조사 서브그래프를 추가했다. 모델이 조회 도구를 선택하고 각 TARGET의
판정·실제 메시지/정책 근거를 검증한다. 최상위 승인 Graph 및 /run·/resume·업무 조치는 #10 통합 범위다.
대화 모델 공급자는 사용자 확인으로 OpenAI API를 사용한다. 모델명·인증 설정 후 실제 실행을 확인하며 현재 고정 응답 모델/HTTP 대역으로 검증한다.

- [입출력·Harness 연결·검증 범위](docs/AI1-09-investigation-contract.md)
- 테스트: `uv run --locked python -m unittest discover -s tests -v`
- 실제 모델 확인: `uv run --locked python -m app.check_investigation --task-json /path/to/backend-task.json`
- 설정: `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`와 두 MCP의 URL·토큰.
  OpenAI 어댑터는 명시적 `MODEL_PROVIDER=openai`일 때만 사용하며 모델은 별도 지정한다.
  RAG 임베딩 키를 대화 모델 키로 자동 재사용하지 않는다.


## 실제 모델 검증 실행 (#8/#9)

사용자 선택 모델은 GPT-5.4 mini다. 다운로드 `.env`의 OPENAI_API_KEY를 사용하며
MODEL_API_KEY가 따로 있으면 대화 모델 키는 그 값을 우선한다. 키 파일을 저장소에 복사하지 않는다.
[연결 3건 → 평가 15건 실행법](docs/AI1-09-live-verification.md)을 따른다.
실제 OpenAI·임베딩·pgvector가 준비되지 않으면 BLOCKED로 기록하며 대역으로 성공을 대신하지 않는다.
호스트 실행은 DB 접속 제한으로 BLOCKED였지만 Docker에서 실제 OpenAI·임베딩·DB 연결에 성공했다.
연결 3/3, 마지막 평가 12/15로 판단 품질은 미완료다. [실제 평가 이력](docs/AI1-09-live-openai-results.md)을 참조한다.
## Harness: 실행 한도·오류 복구 (AI2, #6)

`app/harness/execution.py`의 `HarnessGate`가 조사 루프(#9)의 `ExecutionGate`를 구현한다. 모델 8회·같은 도구 3회·활성 120초 한도, retryable 오류만 호출당 최대 2회 재시도(시도도 한도에 포함), 카운터는 State `harness_budget`에 저장해 재시작·재개 후에도 이어 센다. `guarded_investigate(state)`는 한도 초과 시 판정 없이 `task_outcome: NEEDS_REVIEW`, 최종 실패 시 `FAILED`와 오류를 patch로 남긴다. 결과 미확인 조치는 `resolve_unknown_action`으로 상태를 먼저 조회한다. 계약: [docs/AI2-03-harness-execution-contract.md](docs/AI2-03-harness-execution-contract.md)

## Harness: 안전장치·실행 추적 (AI2, #7)

`app/harness/safety.py`의 `safe_presentation`이 조사 루프의 `present_data`를 구현한다. 본문의 전화·주민번호·카드·계좌·이메일을 가리되 messageId·policyId·URI·버전 같은 식별자는 그대로 두고, 12,000자 초과는 `[TRUNCATED]`, 채팅 템플릿 제어 토큰은 무력화한다. `app/harness/observability.py`는 모델·도구 호출, 재시도, 한도, 결과를 traceId 단위 JSON 이벤트로 남기며 원문·키는 기록하지 않는다. `LANGSMITH_TRACING=true`와 키를 설정하면 LangSmith 추적도 켜진다. 계약: [docs/AI2-04-harness-safety-observability.md](docs/AI2-04-harness-safety-observability.md)

## Docker 이미지 (AI2, #22)

루트 `Dockerfile` 하나로 Agent 서버(기본), Business MCP(`python -m app.business_mcp`), RAG MCP(`python -m app.rag_mcp`), RAG 첫 색인(`python -m app.rag_mcp.index`)을 띄운다. compose 서비스 예시와 컨테이너 간 Host 검사 설정(`RAG_MCP_ALLOWED_HOSTS`)은 [docs/AI2-05-docker-compose.md](docs/AI2-05-docker-compose.md)를 따른다.
