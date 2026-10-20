# AI2-02 Harness 강퇴 승인 interrupt/resume 계약

> 상태: 구현 기준 (2026-10-20, #5). 팀 문서 POLICY §8~10, CONTRACTS §6·§9·§11, ARCHITECTURE §5·§7 기준. Graph 전체 연결은 #10(AI1).

## 1. 책임 경계

| 담당 | 하는 일 |
|---|---|
| Backend | 통합 사건 집계, 심사 필요 여부, 기준 사건(`basisCaseIds`) 고정·예약, 결정 저장과 소진, 실행 직전 재검증, KICK 명령 발급 |
| Harness(#5) | 심사 요청이 있으면 `interrupt()`로 멈추고 checkpoint 저장, 같은 `threadId`로 재개, 재검증 결과에 따라 kick 호출 여부 결정 |
| Business MCP | `kick_user` 실행과 일시 오류 재시도(최초 + 2회) |
| Agent(#10) | `kick_review` 입력을 State에 넣고, `KickGateway` 실제 구현과 checkpointer가 붙은 Graph를 실행기에 연결 |

모델은 강퇴 여부·기간·기준 사건을 정하지 않는다. 승인 여부는 재개 메시지가 아니라 업무 DB 저장본으로 확인한다.

## 2. Graph 위치

```text
… execute_plan → verify_result → review_kick → execute_kick → report_result
```

- `review_kick`: `kick_review.required`가 아니면 통과. 요청이 있으면 운영자 카드 payload로 `interrupt()`.
- `execute_kick`: 재개 후 `KickGateway.revalidate()` 결과가 `APPROVED`일 때만 `kick()` 1회.
- 단독 실행·증거용 Graph: `app.harness.kick_review.build_kick_review_graph(gateway, checkpointer=…)`

## 3. 입력: State `kick_review` (Backend 집계 결과)

```json
{"required": true, "reviewId": "uuid-review", "basisCaseIds": ["uuid-case-1", "uuid-case-2", "uuid-case-3"]}
```

- `required=false`면 interrupt 없이 통과 (AC-12).
- `reviewId`가 UUID가 아니거나 `basisCaseIds`가 서로 다른 UUID 3개 미만이면 멈추지 않고 `kick_result.status=INVALID_REVIEW_REQUEST` (AC-17 방어).

## 4. 운영자 강퇴 카드 (interrupt payload)

```json
{
  "type": "KICK_APPROVAL_REQUIRED", "reviewKind": "KICK", "reviewId": "…",
  "caseId": "…", "taskId": "…", "threadId": "…", "traceId": "…", "policyVersion": "3.0",
  "basisCaseIds": ["…"],
  "targetMessages": [{"messageId": "…", "authorAlias": "…", "text": "<마스킹>"}],
  "contextMessages": […],
  "findings": [{"messageId": "…", "verdict": "VIOLATION", "categories": […], "policyIds": […], "reason": "…"}],
  "policyEvidence": [{"policyId": "CP-2.1", "title": "…", "resourceUri": "policy://…"}]
}
```

기간·실행 명령은 넣지 않는다. 대기 중인 카드는 재시작 후에도 `pending_kick_approval(graph, threadId)`로 읽을 수 있다(AC-13).

## 5. 재개

`POST /agent/tasks/{taskId}/resume` (CONTRACTS §11 envelope)

```json
{"kind": "RESUME_REVIEW", "reviewKind": "KICK", "reviewId": "…", "decisionId": "…", "threadId": "<중단 당시와 동일>", "caseId": "…", "taskId": "…", "…": "…"}
```

| 응답 | 의미 |
|---|---|
| 202 `{taskId, kickResult}` | 재개 완료. `kickResult`는 아래 6 |
| 400 | `reviewKind` 또는 `reviewId` 없음 |
| 409 `REVIEW_KIND_MISMATCH` | 대기 중인 심사가 문맥 검토(CONTEXT) 등 다른 종류 (#24) |
| 404 `THREAD_NOT_FOUND` | 해당 threadId checkpoint 없음 |
| 409 `NO_PENDING_APPROVAL` | 승인 대기가 없음(이미 재개됨). 중복 승인으로 kick 반복하지 않음 (AC-18) |
| 409 `CASE_MISMATCH` / `REVIEW_MISMATCH` | 대기 중인 사건·심사와 불일치 |
| 501 | checkpointer가 붙은 Graph가 `app.state.agent_graph`에 연결되지 않음 (#10에서 주입) |

## 6. 결과: State `kick_result`

| status | kicked | 의미 |
|---|---|---|
| `APPLIED` / `SUCCEEDED` | true | 재검증 통과 후 강퇴 실행 (AC-14) |
| `REJECTED` | false | 운영자 거절. 기존 HIDE·TIMEOUT 유지, 소진은 Backend (AC-15) |
| `REVALIDATION_FAILED` | false | 취소·시간창 이탈·타 심사 소진 등. kick 호출 안 함 (TBD-06) |
| `UNKNOWN` | null | 응답 미확인. 실패로 단정하지 않음 (#6에서 상태 조회) |
| `FAILED` | false | Business MCP 재시도 소진 |
| `INVALID_REVIEW_REQUEST` / `INVALID_DECISION` | false | 입력 계약 위반 |

함께 기록: `reviewId`, `decisionId`, `actionId`. State 필드 `kick_review_id`, `kick_decision_id`, `basis_case_ids`도 채운다.

## 7. `KickGateway` (#10에서 구현)

```python
async def revalidate(*, review_id, decision_id, trace_id) -> dict
    # POST /internal/kick-reviews/{id}/revalidate
    # {"status": "APPROVED", "actionCommand": {...KICK}} | {"status": "REJECTED"} | {"status": "INVALID", "reason": "..."}
async def kick(action_command, *, trace_id) -> dict
    # Business MCP kick_user 결과 data ({"actionId", "status", ...})
```

## 8. Checkpointer

- `app.harness.checkpoint.open_checkpointer(AGENT_CHECKPOINT_DB_URL)`: LangGraph `AsyncPostgresSaver`, `agent` 스키마 자동 생성.
- 복원 허용 타입은 `app.schemas`의 계약 모델·enum으로 제한(임의 클래스 역직렬화 차단).
- 문맥 결정(`reviewKind: CONTEXT`) interrupt는 #24에서 같은 checkpointer·resume 구조로 확장했다: `docs/AI2-06-harness-context-review-contract.md`.
