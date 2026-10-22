# Agent HTTP 실행기·작업 저장·인증 (#10)

## #58 공유 Inbox / API 전용 모드 (2026-10-22)

`AGENT_INBOX_DB_URL`을 설정하면 API는 PostgreSQL `agent_inbox` 스키마의
`threads`/`jobs`만 사용한다. 모든 API 인스턴스가 같은 DB를 사용해야 한다.
이 모드에서는 `AGENT_GATEWAY_FACTORY`, 모델, MCP, checkpointer 설정 없이도 접수와
상태 조회가 가능하며, API가 `TaskRunner`나 Graph를 생성/시작하지 않는다.
`/health.readyForTasks`는 **접수 DB와 서비스 인증의 준비 상태**이며 실행 Worker의 준비 상태가 아니다.

- run 중복 키는 `run:{taskId}`, resume 중복 키는 `resume:{decisionId}`다.
- operation 단위 PostgreSQL transaction advisory lock과 DB unique 제약으로
  서로 다른 프로세스/호스트의 동시 접수를 직렬화한다.
- 같은 논리 요청은 저장된 상태와 200/duplicate=true, 신규는 커밋 후 202/duplicate=false다.
  eventId/traceId 변경만으로 새 작업을 만들지 않는다. 다른 payload는 409다.
- thread의 taskId/caseId/snapshotId/evidenceVersion 바인딩은 변경하지 않는다.
  resume 접수 시 thread 행을 잠그고 최신 작업이 WAITING_REVIEW인지 확인한다.
  서로 다른 결정이 동시에 도착해도 하나만 QUEUED가 된다.
- GET은 어느 API에서도 같은 최신 operation을 조회한다. 응답에는 envelope/원문/인증정보를 넣지 않는다.
- DB 연결/트랜잭션 실패는 503/TASK_STORE_UNAVAILABLE로 반환하며 성공 영수증을 만들지 않는다.
  커밋 응답 유실로 결과가 불확실하면 동일 요청을 재전송해 중복 판별한다.
- DB I/O는 thread pool에서 실행해 API event loop를 막지 않는다.

배포 예시: 비밀값 관리 방식으로 `AGENT_INBOX_DB_URL`과 `AGENT_SERVICE_TOKEN`을 주입하고
기존 `uvicorn app.main:app` 명령으로 여러 API를 실행한다.
시작 시 스키마를 트랜잭션으로 준비하며 최초 동시 시작도 advisory lock으로 보호한다.
DB 계정에는 해당 스키마의 DDL/DML 권한이 필요하다. 스키마 준비 실패 시 시작을 중단한다.

**범위 경계:** PostgreSQL Inbox의 claim/lease/실행/복구 Worker는 후속 이슈다.
이 모드 단독으로는 작업이 QUEUED에 남으며, 기존 SQLite TaskRunner가 이를 소비하지 않는다.
Worker 준비 전에는 실제 운영 트래픽을 전환하지 않는다. BE Outbox/RabbitMQ는 변경하지 않는다.
별도 결과 백엔드나 Celery/Taskiq도 추가하지 않는다.

`AGENT_INBOX_DB_URL` 미설정 시 아래의 기존 SQLite+Graph 결합 모드를 유지한다.
새 공유 모드가 실패해도 SQLite로 자동 대체하지 않는다.
기존 SQLite 파일의 작업/승인 대기를 자동 이관하지 않는다. 전환 전에 접수를 멈추고
QUEUED/RUNNING/UNKNOWN/WAITING_REVIEW 및 checkpoint를 확인해 drain/명시적 이관을 계획해야 한다.
기존 DB 파일을 삭제하거나 두 저장소를 동시에 정본으로 사용하지 않는다.

검증: `AGENT_TEST_INBOX_DB_URL`을 격리 PostgreSQL DB로 설정하고
`PYTHONPATH=tests uv run --locked python -m unittest test_shared_inbox -v`를 실행한다.
테스트마다 고유 스키마를 만들고 해당 스키마만 정리한다. CI의 기존 PostgreSQL 서비스에서도 실행한다.
실제 PostgreSQL을 사용하는 두 API 동시 접수, 응답 유실 후 다른 API 재전송,
별도 API 프로세스의 비정상 종료 후 영속성, resume 경쟁/바인딩 충돌,
DB 실패 및 트랜잭션 롤백을 검증한다. Graph 실행과 실제 BE 집행 증거는 아니다.

---

2026-10-21, 김태관(AI1). 기준 main `cd86bc8`. 작업 브랜치 `feat/agent-10-task-runtime`.

## 변경 범위

`app.main.create_app`의 lifespan이 작업 저장소·Graph·worker의 수명을 관리한다. `runtime.bootstrap`은 기존 AI1 조사 모델/Business·RAG MCP 소비자, AI2 Postgres checkpointer 및 통합 Graph를 조립한다. Graph 노드·정책·CONTEXT/KICK 승인 의미는 변경하지 않는다. 모델의 정상 제안은 공개 명령이 아니며, CONTEXT 결정 뒤 KICK은 별도 승인이다. Backend가 보관한 decisionId를 기존 resume 함수로 검증한다.

기본 배포에서 실제 Backend Gateway가 없으므로 `readyForTasks=false`다. 구현되지 않은 HTTP 경로나 증거 버전을 생성하지 않는다. 운영자 결정 저장/집행/복구를 대역 성공으로 대체하지 않는다.

## HTTP 계약 — BE와 연결할 소비자 변경

| 경로 | 인증 | 의미 |
| --- | --- | --- |
| GET `/health` | 없음 | runtime worker와 서비스 인증 구성 기준 준비 여부. 모든 외부 서비스의 실시간 health를 보장하지 않음 |
| POST `/agent/tasks/{taskId}/run` | Bearer `AGENT_SERVICE_TOKEN` | ANALYZE/REANALYZE envelope 영속 저장 뒤 202 |
| POST `/agent/tasks/{taskId}/resume` | 동일 | RESUME_REVIEW + reviewKind/reviewId/decisionId 저장 뒤 202. 실행 결과는 비동기 조회 |
| GET `/agent/tasks/{taskId}` | 동일 | 최근 작업/결정의 operationId·상태·안전한 오류 코드. 원문·토큰·예외 본문은 반환하지 않음 |

기존 `/resume`의 동기 결과 응답은 비동기 접수·조회로 바뀐다. Backend 호출부가 아직 없으므로 연결 전에 이 변경을 공유해야 한다. 같은 taskId/threadId를 재개에도 유지하며 caseId/snapshotId/expectedEvidenceVersion은 원 작업과 같아야 한다. 새로운 snapshot 재조사는 새 taskId/threadId를 사용한다. RECONCILE은 별도 계약 전까지 400이다.

새 접수는 202, 동일 논리 요청 재전송은 실행을 반복하지 않고 저장된 상태와 200/duplicate=true를 반환한다. eventId/traceId만 달라진 전달도 중복으로 취급한다. run은 taskId, resume는 decisionId를 중복 키로 사용하며 동일 키의 다른 논리 입력은 409다. 다른 task의 thread 재사용·다른 snapshot의 결정은 409다. 미인증은 401, 토큰/실행기 미구성 또는 저장 실패는 503이다. 재개는 대기 중인 thread에서만 접수한다. review 종류 불일치는 worker가 기존 resume 검증에서 거부하고 WAITING_REVIEW + 오류 코드를 남긴다. 결정 참조 오류를 수정하려면 새 decisionId를 사용한다.

Backend→Agent는 Bearer, Agent→MCP도 각각 별도 Bearer, MCP→Spring은 기존 `X-Service-Token`이다. Agent 수신 토큰을 다른 서비스 자격증명과 재사용하지 않는다. 서비스 인증은 운영자 권한 검증을 대체하지 않는다.

## 저장·실행·재시작

Agent 소유 inbox는 표준 라이브러리 SQLite WAL 파일을 사용한다. 업무 DB를 수정하지 않는다. 외부 DB 계약 없이 진행하기 위한 로컬 배포 선택이며 수업 지정 기술이 아니다. `AGENT_TASK_DB_PATH`는 반드시 영속 볼륨에 두고, 같은 호스트의 worker들이 동일 파일을 공유한다. 여러 호스트/네트워크 파일시스템 배포는 이 구현 범위 밖이다. RabbitMQ 보너스 구현으로 주장하지 않는다.

상태: QUEUED → RUNNING → WAITING_REVIEW / SUCCEEDED / FAILED / NEEDS_REVIEW / UNKNOWN. 접수·중복 판별과 claim은 SQLite 트랜잭션, thread별 RUNNING은 unique index로 보호한다. inbox에는 envelope와 상태만 저장한다. 조사/판정/승인 상태는 기존 Postgres `agent` checkpointer에 둔다. 상태 조회의 SUCCEEDED는 Graph 결과이며, 대역 시험은 실제 Spring 집행 근거가 아니다.

QUEUED는 재시작 후 실행한다. WAITING_REVIEW는 자동 재실행 없이 동일 thread의 저장된 승인 대기를 유지한다. 명시적인 resume만 재개한다. 취소/예외로 실행 여부를 확정하지 못하면 UNKNOWN으로 남기며 자동 재시도하지 않는다. 강제 종료(kill -9)로 RUNNING이 남으면 재시작 후에도 재claim하지 않는다. 운영자가 기존 프로세스 종료·체크포인트·Backend action 상태를 대조하기 전에는 해당 thread를 재실행하지 않는다. RUNNING/UNKNOWN의 자동 복구·상태 확인 API는 BE action-status 계약 이후 후속 작업이다. 이 때문에 전 구간 exactly-once나 무인 장애 복구를 보장한다고 주장하지 않는다.

BE Gateway 구현 후 `AGENT_GATEWAY_FACTORY=package.module:open_gateway`로 신뢰할 수 있는 서버 코드의 async context manager를 지정한다. 이 함수는 기존 ModerationGateway를 yield하고 자체 연결을 정리한다. 미구현 Gateway를 주입해 운영 readiness를 켜지 않는다. 필수 설정은 `.env.example`의 AGENT_SERVICE_TOKEN, AGENT_TASK_DB_PATH, AGENT_CHECKPOINT_DB_URL, 모델·Business/RAG 설정이다. 토큰이 없으면 worker도 시작하지 않는다. 공유 Docker 이미지의 non-root uid 10001이 볼륨 파일을 쓸 수 있어야 한다.

## BE 최신 소스 대조

GitHub main `a8d3ce724576bfc086322815c80fce206540d521`를 다시 조회했다.

- `BusinessToolController.java` blob `8f7d0b8`: `/internal/cases/{caseId}/context`를 선언하고 조회·집행은 BusinessToolNotImplementedException(501)이다.
- `InternalCaseController.java` blob `4bd640c`: 같은 GET `/internal/cases/{caseId}/context` 선언. view는 매핑 조건이 아니라 인자이므로 중복 위험이 남는다. 이번 작업에서 Spring 기동 재현은 하지 않았다.
- `InMemoryCandidateSink.java` blob `81e4fd5`: 새 case UUID·메모리 요청 보관만 수행한다.
- BE PR #11은 미병합이며 정책 분기 합의를 대신하지 않는다. 이번 PR은 BE/팀원 코드를 수정하지 않는다.

필요한 연동: 실제 사건/snapshot/live/user/증거·정책 버전 조회, 판정 저장, 검토 생성·저장 결정 조회·문맥 적용, KICK 재검증·실행·상태, finding 취소/복구. 기존 `ModerationGateway`의 메서드·버전 바인딩을 충족하는 실제 응답을 받은 뒤 HTTP adapter를 추가한다. 이전 [경계 문서](AI1-10-latest-spec-and-boundary.md)를 유지한다.

## 수업·검증 경계

PDF 실제 61쪽의 비동기 접수/결과 구분, 72~73쪽의 상태·승인 재개·실행 안전 경계를 적용한다. 팀 정책의 모든 자동 조치가 수업의 모든 변경 전 interrupt 조건을 충족한다는 주장은 하지 않는다. 이번 범위는 HTTP 실행기 기반이며 실채팅→Spring 제재 완료 기준 때문에 #10은 열어 둔다.

동일 thread 재개와 interrupted node 재실행 의미는 [LangGraph 공식 문서](https://docs.langchain.com/oss/python/langgraph/interrupts)를 대조했다. 기존 resume/승인 노드를 재사용해 중복 구현하지 않는다.

워커가 멈춰도 인증된 GET은 저장된 상태를 반환한다. 신규 run/resume는 준비 상태 검사에서 차단한다.
구현 참고: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/), [Python sqlite3 트랜잭션](https://docs.python.org/3.11/library/sqlite3.html#transaction-control).

검증 결과와 명령은 `evidence/task-runtime-2026-10-21.json`에 기록한다. 실제 모델 호출·실제 Spring 조치/복구·UI SSE 수신은 이번 검증에 포함하지 않는다.
