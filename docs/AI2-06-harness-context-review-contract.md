# AI2-06 Harness 문맥 검토(CONTEXT) interrupt/resume 계약

> 상태: 구현 기준 (2026-10-20, #24). 2026-10-21 HTTP 재개 방식 갱신(#27 비동기 접수). 팀 문서 CONTRACTS §2·§5·§8·§11, ARCHITECTURE §3 기준. Graph 전체 연결·Backend 처리·후속 KICK 연결은 #10(AI1).

## 1. 책임 경계

| 담당 | 하는 일 |
|---|---|
| Backend | 문맥 후보 분류(`CONTEXT_REVIEW`, HELD), 검토 요청 저장(reviewId, 멱등), 운영자 결정 저장·권한·위반 유형/증거 검증, HELD 해제·조치 |
| Harness(#24) | 문맥 경로면 검토 요청 생성 → `interrupt()` + checkpoint, 같은 `threadId`로 재개, 저장된 결정을 `context_result`로 남김, 보류면 다시 대기 |
| Agent(#10) | `handling_route` 입력, `ContextReviewGateway` 실제 구현, 노드를 전체 Graph에 연결, `context_result`에 따라 Backend 처리와 필요 시 KICK 심사 연결 |

AI 제안이 정상(NO_VIOLATION)이어도 문맥 경로에서는 공개 명령이 아니다. 결정 내용은 재개 메시지가 아니라 업무 DB 저장본으로 확인한다. 사람 결정으로 `message_findings`(모델 제안)를 덮어쓰지 않는다.

## 2. 노드와 Graph 위치

```text
… investigate → validate_judgment → request_context_review → review_context → apply_context_decision ─┬→ (#10: Backend 처리 → 필요 시 review_kick)
                                                     ↑                                                     │
                                                     └──────────────── DEFERRED (보류: 다시 대기) ───────────┘
```

| 노드 | 실행 조건 | 하는 일 | State 출력 |
|---|---|---|---|
| `request_context_review(state, gateway=)` | `handling_route == "CONTEXT_REVIEW"`이고 `context_review_id` 없음 | `gateway.create_review(card=, trace_id=)` 1회 | `review_kind="CONTEXT"`, `context_review_id` / 실패 시 `context_result.status=INVALID_REVIEW_REQUEST` |
| `review_context(state)` | `context_review_id` 있고 최종 결과 없음 | 운영자 카드로 `interrupt()` | `context_decision_id` / 잘못된 재개 값이면 `INVALID_DECISION` |
| `apply_context_decision(state, gateway=)` | 새 `context_decision_id` | `gateway.get_decision(review_id=, decision_id=, trace_id=)` | `context_result` (아래 5) |
| `route_context_decision(state)` | 조건 분기 | `DEFERRED`면 `"review_context"`, 아니면 `"done"` | – |

- 비문맥 경로(`RULE_CONFIRMED`, `PII_INVESTIGATION` 등)는 `request_context_review`를 그대로 통과한다.
- 예외: 개인정보 조사 결과가 `NEEDS_REVIEW`면(팀 결정 A-16, #47) 통합 Graph의 `submit_pii`가 같은 CONTEXT 검토를 만들고 `context_review_id`를 넣은 뒤 `review_context`로 보낸다. 이후 대기·재개·보류는 문맥 경로와 같고, 카드의 `handlingRoute`는 원래 경로(`PII_INVESTIGATION`)를 유지한다.
- 검토 요청 생성은 interrupt 노드와 분리돼 있어 재개·재시작 때 다시 호출되지 않는다.
- 단독 실행·증거용 Graph: `app.harness.context_review.build_context_review_graph(gateway, checkpointer=…)`

## 3. 입력 State

| 필드 | 값 |
|---|---|
| `handling_route` | `CONTEXT_REVIEW`일 때만 동작 (Backend 사건의 처리 경로) |
| `case_id`, `task_id`, `thread_id`, `trace_id`, `snapshot_id`, `evidence_version`, `policy_version` | 카드·요청에 그대로 실음 |
| `target_messages`, `context_messages`, `message_findings`, `policy_evidence` | 운영자 카드 내용 |

## 4. 운영자 문맥 카드 (interrupt payload, `create_review`의 `card`도 같은 모양·reviewId만 null)

```json
{
  "type": "CONTEXT_DECISION_REQUIRED", "reviewKind": "CONTEXT", "reviewId": "…",
  "caseId": "…", "taskId": "…", "threadId": "…", "traceId": "…",
  "policyVersion": "3.1", "snapshotId": "…", "evidenceVersion": 1, "handlingRoute": "CONTEXT_REVIEW",
  "targetMessages": [{"messageId": "…", "authorAlias": "…", "text": "<마스킹>"}],
  "contextMessages": […],
  "findings": [{"messageId": "…", "verdict": "NO_VIOLATION", "categories": [], "policyIds": […], "reason": "…"}],
  "policyEvidence": [{"policyId": "CP-3.2", "title": "…", "resourceUri": "policy://…"}],
  "allowedDecisions": ["CONFIRM_NORMAL", "CONFIRM_VIOLATION", "DEFER"]
}
```

대기 중인 카드는 재시작 후에도 `pending_context_review(graph, threadId)`로 읽는다.

## 5. 결과: State `context_result`

| status | decision | 의미 / #10 처리 |
|---|---|---|
| `DECIDED` | `CONFIRM_NORMAL` | 정상 확정. Backend가 자기 HELD 원인만 해제(`release_message`) |
| `DECIDED` | `CONFIRM_VIOLATION` | 위반 확정. `categories`는 Backend 저장본. 조치 후 필요 시 KICK 심사 |
| `DEFERRED` | `DEFER` | 보류. 확정 아님, HELD 유지, 같은 reviewId로 다시 대기 |
| `DECISION_NOT_VERIFIED` | – | Backend 저장본 없음·불일치. 아무 조치 안 함 |
| `INVALID_REVIEW_REQUEST` / `INVALID_DECISION` | – | 입력 계약 위반 |

함께 기록: `reviewId`, `decisionId`. State 필드 `context_review_id`, `context_decision_id`, `review_kind`도 채운다.

## 6. `ContextReviewGateway` (#10에서 구현)

```python
async def create_review(*, card, trace_id) -> dict
    # Backend 문맥 검토 요청 생성 (caseId·threadId 기준 멱등). {"reviewId": "uuid"}
async def get_decision(*, review_id, decision_id, trace_id) -> dict
    # 저장된 결정 조회 (POST /api/context-reviews/{id}/decision 으로 저장된 값)
    # {"status": "DECIDED", "decision": "CONFIRM_NORMAL|CONFIRM_VIOLATION|DEFER", "categories": [...]}
    # | {"status": "INVALID", "reason": "..."}
```

## 7. 재개 `POST /agent/tasks/{taskId}/resume`

```json
{"kind": "RESUME_REVIEW", "reviewKind": "CONTEXT", "reviewId": "…", "decisionId": "…", "threadId": "<중단 당시와 동일>", "caseId": "…", "…": "…"}
```

#27 이후 HTTP 재개는 **저장 후 비동기 실행**이다. 전체 HTTP 계약은 [AI1-10-task-runtime.md](AI1-10-task-runtime.md).

| 단계 | 응답 |
|---|---|
| 접수 | 202: 작업을 저장만 하고 실행은 worker가 한다. 같은 decisionId 재전송은 200 `duplicate: true`(다시 실행 안 함), 같은 키의 다른 입력은 409 |
| 접수 거부 | 400 `reviewKind`·`reviewId` 없음, 401 Bearer(`AGENT_SERVICE_TOKEN`) 불일치, 503 실행기·인증 미구성 |
| 결과 조회 | GET `/agent/tasks/{taskId}`: 상태(WAITING_REVIEW / SUCCEEDED / FAILED / NEEDS_REVIEW / UNKNOWN)와 오류 코드 |

worker는 아래 `resume_review`로 Graph를 재개한다. 검증에 걸리면 Graph를 바꾸지 않고, 대기가 남아 있으면 작업을 WAITING_REVIEW로 둔 채 오류 코드를 남긴다(대기가 없으면 FAILED). 올바른 결정은 새 decisionId로 다시 보낸다.

| `ResumeError` 코드 | 의미 |
|---|---|
| `THREAD_NOT_FOUND` | 해당 threadId checkpoint 없음 |
| `NO_PENDING_APPROVAL` | 대기 중인 심사 없음(이미 결정됨). 중복 결정으로 실행을 반복하지 않음 |
| `REVIEW_KIND_MISMATCH` | 대기 중인 심사 종류가 다름. 문맥 결정으로 강퇴를 재개하거나 그 반대가 되지 않음 |
| `CASE_MISMATCH` / `REVIEW_MISMATCH` | 대기 중인 사건·심사와 불일치 |

Python: `resume_review(graph, review_kind=, thread_id=, review_id=, decision_id=, case_id=)`, `resume_context_review(...)`, 기존 `resume_kick_review(...)` 유지.

## 8. 제출 증거 (`tests/test_harness_context.py`)

| 사례 | 테스트 |
|---|---|
| 비문맥 경로 통과 | `test_non_context_route_passes_without_review` |
| AI 정상 제안이어도 대기 | `test_normal_ai_suggestion_still_waits_for_operator` |
| 정상 확정, 모델 제안 보존 | `test_confirm_normal_keeps_model_findings` |
| 위반 확정 | `test_confirm_violation_carries_stored_categories` |
| 보류 후 재결정, 요청 재생성 없음 | `test_defer_waits_again_then_final_decision` |
| 중복 결정 1회 반영 | `test_duplicate_decision_is_not_applied_twice` |
| 저장되지 않은 결정 불신 | `test_unsaved_decision_is_not_trusted` |
| KICK 결정으로 문맥 재개 거부 | `test_kick_decision_cannot_resume_context_review` |
| 재시작 후 대기 복원 (Postgres) | `test_waiting_context_review_survives_restart` |
