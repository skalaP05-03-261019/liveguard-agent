# AI2-04 Harness 안전장치·실행 추적

> 상태: 구현 기준 (2026-10-20, #7). 조사 루프(#9)의 `present_data` 연결 지점과 #6 `HarnessGate`에 붙는다.

## 1. 모델 입력 안전장치 `app.harness.safety.safe_presentation`

| 항목 | 처리 |
|---|---|
| 개인정보 | 이메일 `[EMAIL]`, 주민등록번호 `[RRN]`, 카드 `[CARD]`, 휴대폰·일반전화 `[PHONE]`, 계좌·긴 숫자열 `[ACCOUNT]` |
| 식별자 | 키가 `…Id`, `…Ids`, `…Uri`, `…Version`, `version`, `verdict`, `category` 등이면 가리지 않음. 기존 기본 마스킹은 UUID의 숫자 묶음까지 가려 모델이 messageId를 잃을 수 있었음 |
| 길이 | 문자열당 12,000자, 넘으면 `[TRUNCATED]` (조사 루프가 잘린 문맥의 확정 판정을 거부) |
| 지시문 | 채팅 템플릿 제어 토큰(`<|…|>`)을 `[CONTROL_TOKEN]`으로 무력화. 데이터 속 지시를 따르지 않는 규칙은 `prompts/investigator.md`, 변경 도구 차단은 조사 루프 허용 목록이 담당 |

마스킹은 모든 개인정보 형식을 보장하지 않는다. Backend 마스킹(SP-4.1)과 함께 쓴다. 입력을 바꾸지 않고 새 값을 돌려준다.

## 2. 실행 추적 `app.harness.observability`

- `TraceRecorder`: 이벤트를 모으고 JSON 한 줄로 `liveguard.trace` 로그에 남긴다. `for_trace(traceId)`로 한 작업의 흐름을 모아 본다.
- 허용 필드만 남긴다: traceId, event, name, attempt, status, errorCode, retryable, latencyMs, delayMs, reason, inputTokens, outputTokens, outcome, modelCalls, toolCalls, activeSeconds, retryCount, reviewKind, reviewId, decisionId, threadId, taskId. 채팅 원문·정책 본문·인자·응답·API 키는 버린다.

| event | 언제 |
|---|---|
| `call` | 모델·도구 시도마다 (status ok/error, latencyMs, 모델은 토큰 수) |
| `retry` | 일시 오류 재시도 직전 (delayMs) |
| `limit` | 한도 초과 (reason: MODEL_CALLS / TOOL_CALLS:<name> / ACTIVE_TIME) |
| `outcome` | 노드 종료 (SUCCEEDED / NEEDS_REVIEW / FAILED + 예산 요약) |
| `interrupt` / `resume` | 승인 대기·재개 (`record_event`로 #10에서 기록) |

- `guarded_investigate(state, observer=…)`: #6 gate + `safe_presentation` + 추적을 함께 적용한다.
- LangSmith: `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`를 설정하면 LangGraph 실행이 자동 추적된다. `graph_run_config(thread_id=, trace_id=, task_id=)`로 실행하면 traceId가 메타데이터·태그로 붙는다. 키가 없으면 JSON 로그만 남는다.
- 실패 작업의 추적 참조는 `run_guarded` 결과 `error.traceId`와 `harness_budget`으로 State에 남는다.
