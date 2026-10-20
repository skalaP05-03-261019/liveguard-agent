# AI2-03 Harness 실행 한도·오류 복구 계약

> 상태: 구현 기준 (2026-10-20, #6). 조사 루프(#9)의 `ExecutionGate` 연결 지점([AI1-09](AI1-09-investigation-contract.md))을 구현한다. Graph 전체 연결은 #10.

## 1. 한도 (수업 Harness 표 6-3, 기획서 10.1)

| 항목 | 기본값 | 초과 시 |
|---|---|---|
| 모델 호출 | 8회 | `EXECUTION_LIMIT` (`exhausted: MODEL_CALLS`) |
| 같은 도구 호출 | 3회 (도구 이름별) | `EXECUTION_LIMIT` (`TOOL_CALLS:<name>`) |
| 활성 실행 시간 | 120초 | `EXECUTION_LIMIT` (`ACTIVE_TIME`) |

- 재시도 시도도 호출 수에 포함한다. 한 번 소진되면 같은 작업의 이후 호출은 모두 막는다.
- 활성 시간은 연산(모델·MCP 호출, 재시도 대기)이 실행되는 동안만 센다. 운영자 승인 대기는 포함하지 않는다.

## 2. 재시도

- `AgentToolError.retryable == true`인 오류만 재시도한다. 모델 어댑터는 429·503·타임아웃·연결 오류를, MCP 클라이언트는 일시 장애를 retryable로 표시한다.
- 호출당 최대 2회(최초 포함 3회), 대기 0.5초 → 1초. SDK 자동 재시도는 0회로 두고 이 gate 하나만 재시도한다(중첩 금지).
- 인증·권한·입력·정책 버전 오류는 즉시 실패한다.

## 3. 상태 저장

State `harness_budget` (재시작·재개·재전달 후 `HarnessGate.from_state(state)`로 이어서 센다):

```json
{"modelCalls": 2, "toolCalls": {"search_policy": 1}, "activeSeconds": 3.2, "retryCount": 0,
 "exhausted": null, "lastError": null, "limits": {...}}
```

## 4. 노드 연결

```python
from app.harness.execution import guarded_investigate, run_guarded

# 조사 노드: nodes.investigate 대신
patch = await guarded_investigate(state)            # 공유 gate + harness_budget 기록

# 다른 노드도 같은 gate 규칙으로
patch = await run_guarded(state, lambda gate: step(state, gate))
```

| 결과 | patch |
|---|---|
| 성공 | 노드 결과 + `harness_budget` |
| 한도 초과 | `task_outcome: NEEDS_REVIEW`, `error{code: EXECUTION_LIMIT}`, `harness_budget`. 판정·조치 없음 |
| 최종 실패(재시도 소진·비재시도 오류) | `task_outcome: FAILED`, `error{code, retryable, traceId}`, `harness_budget` |

실패도 예외로 끝내지 않고 patch로 돌려준다. 예외로 끝나면 사용한 예산이 checkpoint에 남지 않아 재실행 시 초기화되기 때문이다.

## 5. 결과 미확인 조치

`resolve_unknown_action(result, read_status=...)`: 결과가 `UNKNOWN`이면 다시 실행하지 않고 `get_action_status`로 실제 상태를 확인한다. 강퇴(`kick_user`) 내부 재시도는 Business MCP가 하므로 gate로 다시 감싸지 않는다.
