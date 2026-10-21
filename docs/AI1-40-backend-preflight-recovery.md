# AI1 #40 — BE 사건 사전검사와 중단 작업 상태 복구

## 범위와 기준

사용자가 최신 BE에 맞춰 추가 구현을 요청했다. 기준은 BE main `9669c370cbb368530f1d4111070f9255051bd2a1`이다. #23은 ERD/TBD 문서 변경이며 Spring 런타임은 이전 `71130fa`와 같다. 문서의 제안·미정 항목을 실제 구현이나 정책 확정으로 취급하지 않는다.

현재 BE에는 실제 사건/명시적 기간 이력 조회가 있지만 검토 생성·저장 결정 조회·결정 적용 Gateway가 없다. 실제 후보 사건은 liveId/정책/route가 부족해 `409 CASE_NOT_READY`이고, 문맥 응답의 snapshot/evidence/visibility 및 사건 userId 정렬도 남았다. 따라서 이번 PR은 **Agent 연결 전 검사와 작업 실행기 복구**로 제한한다. BE/FE 업무 코드나 정책을 덮어쓰지 않는다. 전체 AI 승인 E2E 및 새로운 업무 Gateway 구현 완료를 주장하지 않는다.

이슈 → `feat/backend-preflight-safe-recovery` → 개발/검증 → PR. #39 위의 후속 PR이며 #39/#36 병합 후 base를 main으로 정렬한다.

## 1. 인증된 읽기 전용 사전검사

`POST /agent/tasks/{taskId}/preflight`

- 헤더: 기존 `Authorization: Bearer <AGENT_SERVICE_TOKEN>`; 브라우저 세션/X-Service-Token으로 접근 불가.
- 입력: 기존 `TaskEnvelope`, URL/body taskId 일치. ANALYZE/REANALYZE만 허용.
- 설정: `BUSINESS_MCP_URL`, `BUSINESS_MCP_AGENT_TOKEN`. 모델/RAG/Gateway/inbox 설정 없이도 사용 가능.
- 경로: Agent → Business MCP `get_moderation_case`, `get_chat_context` → 기존 BE `/internal/cases/{caseId}` 및 `/context`.
- 공통 loader로 사건·snapshot·evidence·target 바인딩을 검증하고 Graph와 공유하는 live/user/route·HELD 검증을 적용한다. 없는 식별자·정책 버전·가시성은 추정하지 않는다.
- 검사 결과 HTTP 200의 `READ_CONTRACT_READY` 또는 `BLOCKED`. 인증 실패 401, 미설정/검사 인프라 실패 503, URL/kind 불일치 400. 기존 TaskEnvelope 형식 오류는 422.

```json
{
  "taskId": "00000000-0000-4000-8000-000000000002",
  "traceId": "00000000-0000-4000-8000-000000000005",
  "scope": "READ_ONLY",
  "executionAuthorized": false,
  "readContractReady": false,
  "status": "BLOCKED",
  "blockers": [{"code": "CASE_NOT_READY", "retryable": false}]
}
```

이 응답은 작업 접수·운영자 승인·AI 판정이 아니다. inbox 생성/모델 호출/변경 도구/업무 DB 직접 쓰기 없음. 원문·정책 본문·토큰을 결과에 싣지 않으며 `/health`의 실행 준비 상태를 켜지 않는다. `readContractReady=true`라도 전체 Gateway 준비를 뜻하지 않는다.

## 2. 공개 작업 상태의 실패 코드

Graph의 알려진 실패가 inbox에 반영될 때 안전한 오류 코드도 보존한다. 예를 들어 실제 BE `CASE_NOT_READY`는 `FAILED + CASE_NOT_READY`로 조회된다. 응답 본문·예외 문구·토큰은 노출하지 않는다. 변경 결과 미확인은 기존처럼 UNKNOWN으로 남긴다.

## 3. 오프라인 복구

```bash
# 기본은 상태 비교만 수행한다.
uv run --env-file .env python -m app.recover_tasks --task-id UUID
# 결과를 확인한 뒤 Agent inbox 상태만 반영한다.
uv run --env-file .env python -m app.recover_tasks --task-id UUID --apply
```

배포된 worker와 같은 `AGENT_TASK_DB_PATH` 볼륨, `AGENT_CHECKPOINT_DB_URL`, Graph 코드 버전을 사용한다. 새 잠금 프로토콜을 모르는 구버전 worker는 먼저 중지한다. 이 도구는 기존 체크포인트 테이블만 읽으며 스키마 생성/마이그레이션 setup을 실행하지 않는다. 업무 DB나 체크포인트를 수정하지 않는다.

팀원 PR #42의 named volume uid 10001 쓰기 권한 보강과 병행한다. recovery lock 파일도 같은 task 볼륨에 생성되므로 동일 사용자 권한이 필요하다. #42의 Dockerfile·Harness 문서는 이번 PR에서 수정하거나 병합하지 않는다.

| 관측한 상태 | 제안하는 inbox 상태 |
| --- | --- |
| task/case/thread/snapshot/evidence와 승인 카드가 일치하는 human interrupt | WAITING_REVIEW |
| 동일 작업의 진행 노드가 없고 종료 체크포인트가 확정됨 | SUCCEEDED / FAILED / NEEDS_REVIEW |
| 체크포인트 없음·바인딩 불일치·실행 오류·미완료 노드·미확인 조치 | UNKNOWN, BE 확인 필요 |
| 최신 작업이 QUEUED 또는 이미 대기/종료 | 변경 없음 |

RESUME_REVIEW는 해당 reviewId/decisionId가 체크포인트 결과에 반영된 것까지 확인한다. 재개 작업을 claim만 하고 결정이 아직 반영되지 않은 상태를 이전 WAITING 체크포인트로 성공 처리하지 않는다. DEFER 후 같은 문맥 검토 대기는 복원 가능하지만 자동 공개하지 않는다. CONTEXT 이후 KICK의 별도 승인도 유지한다.

실행 worker는 같은 inbox 파일의 공유 OS 파일 잠금, 복구는 배타 잠금을 사용한다. 실행 중에는 `RECOVERY_WORKER_ACTIVE`; 복구 중에는 worker가 새 작업을 claim하지 않는다. 같은 호스트의 로컬 볼륨 전용이며 NFS/다중 호스트 잠금을 보장하지 않는다. 프로세스 강제 종료 시 OS가 잠금을 회수한다. 시간 경과를 근거로 잠금을 빼앗지 않는다.

복구는 Graph invoke/resume, 모델 호출, 검토 생성, 조치 재전송을 하지 않는다. UNKNOWN을 QUEUED로 바꾸지 않는다. 불명확한 작업의 자동 복구/업무 상태 조회는 후속 BE 계약이 필요하다. 기본 dry-run은 inbox 상태를 변경하지 않는다. 미확인/사용 불가 결과의 CLI exit code는 2다.

## 검증과 한계

새 테스트는 인증·readiness/실행 승인 구분·실제 형태의 409를 HTTP 어댑터/MCP wire로 전달·증거 불일치·오류 마스킹·dry-run·worker 상호 배제·자식 프로세스 강제 종료·DEFER/완료 복구·미확인 변경의 재실행 금지를 검사한다. 실제 Postgres 연결을 닫고 읽기 전용으로 재접속하는 복구 검사는 CI DB 환경에서 실행한다. 모델과 BE 변경 응답은 통제 fixture이며 실제 제재가 아니다.

이번 환경에서는 Docker socket과 Python의 로컬 TCP 연결 접근이 거부돼 기존 사용자 서비스 재빌드와 로컬 DB/TCP 시험을 실행할 수 없다. 실행 중인 Spring은 허용된 curl 읽기로 재확인했다. GitHub CI run `39533450449`, 코드 `702e284`에서 ASGI 225개(13.527초), TCP 225개(24.819초), skip 0으로 모두 통과했다. 실제 Postgres 연결 종료/재접속 복구도 통과했다. 실제 BE 관측과 통제 응답 시험을 구분하며 모델/실제 제재 성공을 의미하지 않는다.

수업 근거: PDF 실제 61쪽(비동기 task 상태), 72쪽(Guardrails 승인 중단·State/Memory 재개). 이번 범위는 그중 상태·복구 경계를 보강하며 전체 Harness 준수 판정을 대신하지 않는다.

## 다음 BE 연결 조건

1. 실제 후보 생성 시 liveId/userId/policyVersion/handlingRoute와 안정적인 증거 버전 제공.
2. `/context`에서 같은 snapshot/evidence와 메시지 visibility 제공.
3. 검토 생성의 멱등 계약, 저장된 사람 결정 조회, 결정 적용·재검증·dispatch 경로 구현.
4. 활성 정책 ID(`v3.1`/`3.1`)와 원문/색인 일치 확정.

준비되면 기존 ModerationGateway 포트를 구현해 CONTEXT 정상/위반/보류 → 동일 thread 재개 → BE 검증·반영을 연결한다. legacy `/reviews/{decisionId}/reconcile`를 새 사람 승인 API로 대체 사용하지 않는다.
