# BE2-01 Agent ↔ Business MCP 업무 계약 초안

> 상태: 협의 초안 (2026-10-20). 구현 규격이 아니다. Architecture 통합 초안과 Contract Draft v0.5를 바탕으로 작성했으며, BE1·AI1 합의 후 버전과 예시를 고정한다.

## 1. 목적과 책임 경계

- Agent는 작업을 받고 조회 MCP를 호출해 조사한다. 업무 DB를 직접 읽거나 변경하지 않는다.
- Business MCP는 Agent에 업무 도구를 제공하고, 인증된 Spring Boot `/internal/**` API를 호출한다.
- Backend는 권한, 사건 증거와 버전, 정책 버전, 현재 상태, 조치 기간, 멱등성을 최종 검증한다.
- 조회 도구만 조사 모델에 노출한다. 변경 도구는 코드 실행 경로에서 Backend가 발급한 `ActionCommand`로만 호출한다.
- 현재 코드 초안은 MCP Python SDK `mcp>=2,<3`의 Streamable HTTP transport를 FastAPI `/mcp`에 마운트한다. SDK/transport는 BE1·AI1과 최종 합의가 필요하다. Backend 내부 API의 실제 경로와 인증 방식도 확인 전이다.

## 2. 공통 표현

- ID는 UUID 문자열, 시간은 UTC ISO 8601, 기간은 정수 seconds로 표현한다.
- JSON 필드명은 camelCase로 고정한다. Python 내부 모델은 snake_case로 변환한다.
- 도구 성공 데이터는 아래 구조를 기준으로 한다. MCP SDK가 wire format을 구성하며, `traceId`는 Agent 작업에서 전달된 값을 사용한다.
- 각 tool은 optional `traceId` 입력을 받는다. 생략 시 Business MCP가 새 UUID를 생성한다. 실제 Agent 연결에서는 작업 envelope의 `traceId`를 항상 넘기는 것이 원칙이다.

```json
{
  "data": {},
  "traceId": "uuid-trace"
}
```

- 도구 오류는 성공 응답처럼 반환하지 않는다. MCP 도구 오류로 표시하고 의미상 다음 구조를 제공한다.

```json
{
  "error": {
    "code": "STALE_EVIDENCE",
    "message": "Evidence version is no longer current",
    "retryable": false
  },
  "traceId": "uuid-trace"
}
```

- 공통 오류 후보: `UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `STALE_EVIDENCE`, `STALE_TARGET`, `POLICY_MISMATCH`, `INVALID_EVIDENCE`, `OUT_OF_POLICY`, `IDEMPOTENCY_CONFLICT`, `TEMPORARY_UNAVAILABLE`.
- 사용자 원문, 계정·전화번호 등 불필요한 PII, 세션 및 API 키는 MCP 결과·일반 로그·trace에 넣지 않는다. 조회 결과는 최소화하고 필요한 개인정보는 마스킹한다.

## 3. Agent 작업 수신 계약 (Backend → Agent)

Contract Draft v0.5의 HTTP 우선 경계를 기준으로 한다.

| Method / path | 목적 |
|---|---|
| `POST /agent/tasks/{taskId}/run` | 신규 분석·재분석·조정 작업 |
| `POST /agent/tasks/{taskId}/resume` | 저장된 검토 작업 재개 |

요청 본문 초안:

```json
{
  "eventId": "uuid-event",
  "taskId": "uuid-task",
  "caseId": "uuid-case",
  "kind": "ANALYZE",
  "threadId": "case:uuid-case:analysis:1",
  "snapshotId": "uuid-snapshot",
  "expectedEvidenceVersion": 1,
  "decisionId": null,
  "traceId": "uuid-trace"
}
```

- `kind` 후보: `ANALYZE`, `REANALYZE`, `RESUME_REVIEW`, `RECONCILE`.
- `RESUME_REVIEW`는 저장된 `decisionId`와 기존 review `threadId`를 사용한다. 새 thread를 만들지 않는다.
- `taskId` URL과 본문 값은 일치해야 한다. 동일 작업 재전달은 중복 조치를 발생시키지 않는다.
- 첫 통합은 HTTP. 이후 RabbitMQ 메시지에도 같은 envelope를 사용한다는 안이 있으나, ACK/DLQ 및 retry 규칙은 별도 합의한다.
- 수신 성공·오류 HTTP status, 중복 event 처리, 응답 body와 작업 완료 통지 방식은 확정 필요하다.

## 4. Agent → Business MCP 도구 계약

아래는 계약 초안이다. 입력 필수성·응답 세부 필드는 BE1 실제 API와 테스트 fixture 대조 후 확정한다. UUID는 별도 표기가 없는 한 문자열이다.

### 4.1 조회 도구

| Tool | 입력 초안 | 결과 핵심 |
|---|---|---|
| `get_moderation_case` | `{ "caseId": "uuid-case", "traceId?": "uuid-trace" }` | `caseId`, `snapshotId`, `evidenceVersion`, `policyVersion`, `targetMessageIds`, 대상의 현재 버전 식별 정보 |
| `get_chat_context` | `{ "caseId": "uuid-case", "before": 5, "after": 5, "traceId?": "uuid-trace" }` | 주변 채팅 목록: `messageId`, `authorAlias`, `text`(마스킹), `sentAt`, `visibility`, 선택적 `replyToId` |
| `get_user_history` | `{ "caseId": "uuid-case", "days": 30, "traceId?": "uuid-trace" }` | 기간 내 유형별 확정 건수 및 판단 근거 식별자. 기본 기간 30일 |
| `get_stream_info` | `{ "caseId": "uuid-case", "traceId?": "uuid-trace" }` | 방송 상태·시간 및 사건과 관련된 공식 이벤트 정보 |
| `get_official_links` | `{ "caseId": "uuid-case", "traceId?": "uuid-trace" }` | 등록된 공식 도메인·링크와 확인 결과 |
| `get_security_facts` | `{ "caseId": "uuid-case", "traceId?": "uuid-trace" }` | PII 실체·공식 여부의 `TRUE` / `FALSE` / `UNKNOWN`, 검증 출처 식별자 |
| `get_action_status` | `{ "actionId": "uuid-action", "traceId?": "uuid-trace" }` | 실제 조치 상태 및 가능한 경우 실행 결과·시각. 후보 상태: `PENDING`, `APPLIED`, `FAILED`, `UNKNOWN` |

조회 예시 (`get_moderation_case`):

```json
{
  "data": {
    "caseId": "uuid-case",
    "snapshotId": "uuid-snapshot",
    "evidenceVersion": 1,
    "policyVersion": "0.4-review",
    "targetMessageIds": ["uuid-message"],
    "targetVersions": { "uuid-message": 3 }
  },
  "traceId": "uuid-trace"
}
```

### 4.2 변경 도구

변경 요청은 Agent가 직접 구성한 명령이 아니라 Backend가 계획 검증 후 발급한 `ActionCommand`를 전달한다.

```json
{
  "actionId": "uuid-action",
  "planId": "uuid-plan",
  "caseId": "uuid-case",
  "type": "HIDE",
  "targetId": "uuid-message",
  "expectedEvidenceVersion": 1,
  "expectedTargetVersion": 3,
  "policyVersion": "0.4-review",
  "idempotencyKey": "uuid-action",
  "durationSeconds": null
}
```

| Tool | 입력 초안 | 결과 핵심 |
|---|---|---|
| `hide_message` | `{ "actionCommand": { ...ActionCommand, "type": "HIDE" }, "traceId?": "uuid-trace" }` | `actionId`, 적용 상태, 실제 변경된 target/version |
| `timeout_user` | `{ "actionCommand": { ...ActionCommand, "type": "TIMEOUT" }, "traceId?": "uuid-trace" }`; Backend 계획에 포함된 기간 | `actionId`, 적용 상태, 최초 `startedAt`, `expiresAt` |
| `release_message` | `{ "actionCommand": { ...ActionCommand, "type": "RELEASE_HELD" }, "traceId?": "uuid-trace" }` | `actionId`, 해당 HELD 해제 결과 및 현재 visibility |
| `kick_user` | `{ "actionCommand": { ...ActionCommand, "type": "KICK", "decisionId": "uuid-decision", "violationIds": ["uuid-1", "uuid-2", "uuid-3"] }, "traceId?": "uuid-trace" }` | `actionId`, 강퇴 결과 |
| `reconcile_review` | `{ "caseId": "uuid-case", "decisionId": "uuid-decision", "traceId?": "uuid-trace" }` | 취소·감경·복구 결과, 실제 복구 상태 |

- MCP 변경 tool은 `actionCommand` 객체를 받고, tool별로 `type`을 고정한다(`HIDE`, `TIMEOUT`, `RELEASE_HELD`, `KICK`). Backend는 실행 시에도 계획·권한·대상 상태가 유효한지 다시 검증한다.
- `kick_user` 명령은 승인된 운영자 `decisionId`와 최소 3개의 서로 다른 위반 ID를 요구한다. 단, 현재 포함·최근 24시간·해제/소진 제외 조건과 승인 유효성은 Backend가 실행 직전에 재검증한다. 누적 계산을 MCP에서 수행하지 않는다.
- 퇴장 실패가 retryable이면 최초 실행에 더해 최대 2회 재시도한다. 모든 시도에서 같은 `actionId`와 `idempotencyKey`를 사용한다. 재시도 소진 시 `{ "status": "FAILED", "attempts": 3, "errorCode": "..." }`를 반환한다. 비재시도 오류(예: 권한·결정·증거 만료)는 즉시 MCP tool 오류로 돌려보낸다.
- 3회 위반 대기, 운영자 interrupt/checkpoint, 같은 `threadId` 재개는 Agent Graph/Harness 책임이며 MCP tool이 수행하지 않는다. `kick_user`는 승인 후 호출되는 코드 실행 경로에만 연결하고, LLM 조사 tool 목록에는 노출하지 않는다.
- timeout 기간은 Agent나 모델이 정하지 않는다. 재검토 감경도 Backend가 검증한 결과만 실행한다.
- 같은 `actionId`의 재요청은 같은 idempotency key와 같은 payload로만 허용한다. 다른 payload는 `IDEMPOTENCY_CONFLICT`다.
- 응답 유실 등으로 적용 여부가 불명확하면 새 변경 요청을 보내지 말고 `get_action_status`를 조회한다. 성공한 timeout 재시도로 만료 시각을 연장하지 않는다.

## 5. Business MCP → Backend 내부 API 초안

Business MCP는 아래 책임을 수행할 내부 API 경계를 사용한다. 경로는 Contract Draft v0.5의 최소안이며 실제 Spring 컨트롤러와 대조해야 한다.

| Backend API 후보 | Business MCP 사용처 |
|---|---|
| `GET /internal/cases/{id}/context` | 사건 snapshot 및 채팅·방송·링크·보안 사실 조회 |
| `GET /internal/cases/{id}/history` | 확정 제재 이력 조회 |
| `POST /internal/cases/{id}/plans` | Agent 판단·후보 조치를 검증하고 서버 발급 계획/명령 획득 |
| `POST /internal/actions/{id}/execute` | 발급된 명령을 멱등하게 실행 |
| `GET /internal/actions/{id}` | 실행 결과 및 UNKNOWN 상태 확인 |
| `POST /internal/cases/{id}/reviews/{decisionId}/reconcile` | 저장된 재검토 결정의 취소·감경·복구 실행 (신규 후보 경로, 확정 필요) |

- 실제 인증 헤더/서비스 identity, 요청·응답 JSON, 상태 코드, 오류 코드, 권한 범위는 BE1과 확인한다.
- 현 코드에서 조회는 `/context?view=moderationCase|chatContext|streamInfo|officialLinks|securityFacts`와 `/history?days=...`를 사용한다. `view` 쿼리 규격은 통합을 위해 둔 임시 가정이며 Backend와 일치시켜야 한다.
- Inbound Agent 연결은 `BUSINESS_MCP_AGENT_TOKEN` Bearer token, Backend 호출은 `BUSINESS_MCP_SERVICE_TOKEN` Bearer token을 사용하도록 했다. 실제 서비스 인증 계약 확정 전 제안이다.
- 정보 조회와 변경 API 모두 내부 서비스 인증을 적용한다. 변경 API는 대상 권한, 현재 상태, evidence/target version, policy version, 조치 허용 범위 및 idempotency를 재검증한다.

## 6. 연결 흐름

1. Backend가 Agent에 task envelope를 전달한다.
2. Agent는 `get_moderation_case`로 snapshot/evidence version을 확인하고 필요한 조회 MCP만 호출한다.
3. Agent는 실제 tool 응답의 근거 ID와 정책 버전을 이용해 판단한다. 임의 생성한 ID나 seed fixture를 조회 사실처럼 취급하지 않는다.
4. 조치 후보가 있으면 Backend plan API 검증을 거쳐 발급된 `ActionCommand`만 변경 MCP에 전달한다.
5. 실행 결과가 `UNKNOWN`이면 상태 조회 후 복구 경로를 결정한다. 완료 결과·이벤트는 Agent가 Backend에 보고하며 브라우저 SSE는 Backend가 담당한다.

## 7. BE1·AI1과 확정할 항목

1. Business MCP의 배치 위치, FastAPI MCP SDK/version, transport 및 인증 방식.
2. 각 조회 tool의 필드·마스킹 규칙·페이지 제한·기본값과 D01 포함 최소 응답 fixture.
3. Agent에 노출할 tool과 코드 전용 tool 구분, `ActionCommand` 타입별 JSON Schema 및 `reconcile_review` 입력.
4. Backend 내부 API 실제 경로, request/response JSON, 오류 코드와 HTTP 상태 코드.
5. Agent task envelope를 v0.5 예시와 일치시킬지, task 접수·완료·실패 통지 및 재시도 의미.
6. traceId 전달 위치, 로그에서 제외할 필드, 서비스 인증 및 운영 비밀값 보관 방식.

## 8. 기존 Agent 골격과의 차이

현재 [Agent `TaskEnvelope`](../app/schemas.py)는 `expectedCaseVersion`을 요구하고 `snapshotId`·`expectedEvidenceVersion`을 받지 않으며, `kind`에 `NOTIFY`가 있고 `RECONCILE`은 없다. 위 초안의 v0.5 envelope와 다르므로, BE1·AI1이 필드 및 enum을 합의하기 전까지 양쪽 구현을 임의로 맞추지 않는다.

현재 Agent 포트([`ports.py`](../app/clients/ports.py))의 generic `dict` 입출력은 구현 전 초안이다. 본 계약이 확정되면 MCP별 Pydantic 입력·출력 모델 및 Backend 응답 모델로 구체화한다.
