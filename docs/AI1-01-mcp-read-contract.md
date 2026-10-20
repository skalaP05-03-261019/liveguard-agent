# AI1-01 Agent 조회 연결 — 구현 제안

관련 이슈: [#1](https://github.com/skalaP05-03-261019/liveguard-agent/issues/1). 기준: 2026-10-20 Notion 통합 CONTRACTS 및 BE2-01 서버 코드. **소비자 구현과 가상 Backend 연결 단계이며 BE1/BE2 공동 확정·실제 Spring 통합 전**이다. 기존 서버 계약의 미확정 항목을 팀 승인으로 바꾸지 않는다.

## 입력과 출력

- `/agent/tasks/{taskId}/run`, `/resume`의 입력은 Notion 예시대로 `eventId`, `taskId`, `caseId`, `kind`, `threadId`, `snapshotId`, `expectedEvidenceVersion`, `decisionId?`, `traceId`를 사용한다.
- `kind`: ANALYZE/REANALYZE/RESUME_REVIEW/RECONCILE. RESUME_REVIEW는 decisionId가 필요하다. 구 expectedCaseVersion·attempt·NOTIFY를 거부한다. 이전 생산자는 동시 수정이 필요하며 양쪽 버전을 자동 추측하지 않는다.
- 새 `AnalysisResult`는 caseId/snapshotId/evidenceVersion/policyVersion 및 메시지별 messageFindings를 표현한다. TARGET별 판정·categories·policyIds·evidenceMessageIds·reason·missingFacts 구조이며 durationSeconds는 금지한다. 실제 조회 근거와 정책의 의미 검증은 다음 조사 PR의 책임이다.
- 미연결 Graph의 기존 JudgmentOutput/RequestedAction은 legacy다. 새 AnalysisResult는 아직 HTTP 응답이나 모델 출력으로 생성되지 않는다.
- `/run`, `/resume`는 입력 검증 후에도 501이다. 조회 노드 구현을 작업 접수·저장·모델 실행 완료로 표시하지 않는다.

## MCP 조회

`BUSINESS_MCP_URL` 기본값은 `http://127.0.0.1:8002/mcp`. Agent는 `BUSINESS_MCP_AGENT_TOKEN` Bearer를 사용한다. Backend 서비스 토큰은 Agent 조회 클라이언트가 사용하지 않는다. `.env` 자동 로드는 없다.

조회 7개만 allowlist로 허용한다. MCP 서버가 변경 도구를 함께 제공해도 이 클라이언트는 호출을 거부한다. 도구마다 새 세션을 열고 닫으며 연결/협상/호출/종료를 합쳐 10초 제한이다. 자동 도구 재시도는 없다. SDK의 프로토콜 탐색과 legacy 협상은 업무 도구 재실행과 구분한다.

`load_case`는 다음 두 호출을 수행한다. 공통 입력은 caseId와 작업 traceId이며, get_chat_context의 before/after는 각각 5다.

| 도구 | 필수 data 응답 |
| --- | --- |
| get_moderation_case | caseId, snapshotId, evidenceVersion, policyVersion, targetMessageIds, targetVersions |
| get_chat_context | caseId, snapshotId, evidenceVersion, messages[] |

응답은 `{data: {...}, traceId: ...}`다. 대상/스냅샷/증거 버전은 요청과 일치해야 한다. targetVersions는 각 TARGET의 양수 버전을 포함한다. messages의 항목은 messageId/authorAlias/text/sentAt/visibility/replyToId?이며 시각은 timezone을 포함한다. TARGET 본문도 이 응답 안에 있어야 한다. before/after 5개 범위로 TARGET 전체를 제공할 수 없다면 Backend의 snapshot 조회 계약을 먼저 보완해야 한다.

**이번 소비자 구현에서 추가로 요구하는 조건:** 주변 대화에도 caseId/snapshotId/evidenceVersion을 포함해 첫 조회 이후 증거가 바뀌었는지 검증한다. BE2 서버는 Backend 응답을 그대로 감싸므로 BE1/BE2가 같은 snapshot으로 고정한 대화를 반환하도록 확인해야 한다. 현재 MCP 입력에는 snapshotId가 없으며, 향후 입력 확장 여부는 제공자와 합의한다. 현재 구현은 응답 버전 불일치를 거부한다.

가상 요청/응답 예시는 [fixture](../tests/fixtures/agent-case-v0.5.json). `fixture-policy-v1`은 테스트용이며 배포 정책이 아니다.

## State와 실패

- State에 식별자·증거/정책 버전·사건 snapshot·TARGET/CONTEXT 대화를 분리해 저장한다. 노드가 반환하는 메모리 상태이며 영속 체크포인트는 아니다.
- 일반 tool_observations에는 toolName과 traceId만 기록한다. 원문은 조사 State에만 남기고 토큰·원문을 오류 메시지나 일반 로그에 복제하지 않는다. Backend의 마스킹은 별도 계약으로 확인한다.
- 잘못된 사건/대상 누락/중복 메시지는 INVALID_EVIDENCE, snapshot 또는 증거 버전 불일치는 STALE_EVIDENCE, 필드 누락/잘못된 envelope는 INVALID_TOOL_RESPONSE다.
- 인증 실패는 UNAUTHENTICATED/FORBIDDEN, 제한 시간과 연결 실패는 TEMPORARY_UNAVAILABLE로 전달한다. Backend의 알려진 tool 오류 code/retryable을 유지하며 실패를 정상 판정으로 바꾸지 않는다.
- 판단·제재·결과 보고·RAG·체크포인트·강퇴는 이번 구현 범위 밖이다. 승인/재개와 기간표에 대한 기존 미결정 상태를 유지한다.

## 검증

기본 명령: `uv run --locked python -m unittest discover -s tests -v`.

기본값은 httpx2 ASGITransport로 실제 FastAPI Business MCP 앱, 인증 미들웨어, SDK Streamable HTTP 직렬화와 Agent 클라이언트를 함께 시험한다. Backend만 가상이다. `LIVEGUARD_TEST_TCP=1`이면 같은 테스트를 Uvicorn 임시 loopback 포트로 수행한다. 기본 테스트 성공은 TCP·실제 Spring·모델·RAG 연동 성공을 의미하지 않는다.
