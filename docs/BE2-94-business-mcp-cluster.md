# Business MCP 다중 인스턴스 운영 (#94)

## 전송과 상태 경계

잠금 SDK의 `streamable_http_app(stateless_http=True, json_response=True)`를 사용한다.
HTTP 요청마다 새 MCP transport가 생성되며 서버 측 MCP session ID를 발급·저장하지 않는다.
Agent는 호출마다 초기화하고 닫지만 동일 논리 세션의 HTTP 요청도 서로 다른 인스턴스로
갈 수 있다. **Sticky session, 공유 session 저장소, SSE 재연결/이벤트 replay를 사용하지 않는다.**

현재 도구는 요청/응답만 사용한다. 서버 주도 sampling/elicitation, resource 구독,
opaque request-state를 이용한 다중 단계 도구는 이 구성에서 지원하지 않는다.
그 기능이 필요해지면 공유 키/상태 또는 affinity 설계를 먼저 변경해야 한다.
서버를 stateful 모드로 바꾸고 현재 round-robin 설정을 유지하면 안 된다.

업무 상태·승인·멱등성은 Backend가, 작업·checkpoint·lease는 Agent가 관리한다.
MCP 메모리에는 연결 pool, 순간 요청 수, lifecycle 상태만 둔다.
검사 대기열/Graph/LLM은 MCP 안에 두지 않는다.

## 두 인스턴스 + 공통 주소

저장소 루트에서 실제 프로세스 환경 또는 접근 제한된 `.env`를 설정한다.
토큰은 명령행 인자나 저장소에 쓰지 않는다. 로컬 Backend를 사용한다면 컨테이너에서
접근할 수 있는 `http://host.docker.internal:8080` 등을 사용한다.

```bash
docker compose --env-file .env -f deploy/business-mcp.compose.yaml config --quiet
docker compose --env-file .env -f deploy/business-mcp.compose.yaml up --build -d
curl --fail http://127.0.0.1:8002/ready
```

`mcp-1`, `mcp-2`는 호스트에 직접 노출하지 않는다. HAProxy의 `business-mcp:8002`가
공통 주소이며 호스트에는 `127.0.0.1:8002`로만 연다.
같은 Compose 네트워크의 Graph Worker는 `BUSINESS_MCP_URL=http://business-mcp:8002/mcp`,
호스트 Worker는 `http://127.0.0.1:8002/mcp`를 사용한다.
다른 Compose 프로젝트라면 Infra가 공통 내부 네트워크에 연결해야 한다.
외부 배포는 TLS/내부 네트워크와 실제 Host allowlist를 적용하고 무제한 `*`를 쓰지 않는다.
기존 단일 인스턴스 Compose 예시를 대체하는 선택적 overlay가 아니라 독립 실행 파일이다.

LB는 HTTP 요청별 round-robin, `/ready` 매초 probe, 연속 실패 2회 후 제외,
성공 2회 후 복귀, Docker DNS 갱신을 사용한다.
**프록시 retry는 0**이다. 장애 직후 탐지 전 요청이나 진행 중 요청은 실패할 수 있다.
한 요청의 실패를 숨겨 다른 인스턴스에 변경 요청을 재전송하지 않는다.

## 제한과 lifecycle

| 경계 | 기본값 / 설정 |
|---|---|
| Agent 호출 동시성 | endpoint별 프로세스/event loop 8, `BUSINESS_MCP_CLIENT_CONCURRENCY` |
| 호출 전체 deadline | 슬롯 대기·초기화·schema 조회·도구·정리 포함 10초, `BUSINESS_MCP_CALL_TIMEOUT` |
| 세션별 HTTP pool | 최대 2연결, connect 3초 / pool 대기 1초 / HTTP timeout 10초 |
| MCP 인스턴스 HTTP 요청 | 16, `BUSINESS_MCP_MAX_REQUESTS`, 초과 시 503 + Retry-After: 1 |
| MCP → Backend pool | 16, `BUSINESS_MCP_BACKEND_CONNECTIONS`, pool 대기 1초 / connect 3초 |
| Backend timeout | 10초, `BUSINESS_MCP_BACKEND_TIMEOUT` (HTTP 단계별 제한) |
| Uvicorn drain | 20초, `BUSINESS_MCP_SHUTDOWN_TIMEOUT`; Compose 종료 유예 30초 |
| HAProxy | global 128연결 / 인스턴스 16 / connect 3초 / queue 1초 / client·server 15초 |

HTTP 요청 수는 도구 외 초기화·schema 조회도 포함한다. 응답 body 종료까지 슬롯을 잡는다.
Agent 호출 슬롯을 API 인스턴스 수만큼 곱한 값은 클러스터 전역 한도가 아니다.
Graph/모델 전역 제한은 #89 공유 Inbox admission을 유지한다.
동일 프로세스에서 같은 endpoint의 동시성 설정은 일치해야 한다.
설정 변경 시 Agent/LB/MCP timeout과 종료 유예도 함께 조정한다.

`/health`는 liveness용 HTTP 200과 제한된 readiness 상태를 반환한다.
`/ready`는 credential 설정 + lifecycle 시작 + drain 아님이면 200, 아니면 503이다.
Backend 연결을 검사하지 않으므로 BE 장애는 실제 도구 오류로 드러난다.
수신 인증도 두 credential이 모두 설정되기 전에는 503으로 차단한다.

운영은 `python -m app.business_mcp`를 사용한다. SIGTERM/SIGINT 시 즉시 drain 표시 후
Uvicorn이 새 연결을 막고 진행 요청을 기다린다. 유예 만료 시 취소하고 lifespan에서
MCP manager와 Backend pool을 닫는다. 직접 `uvicorn app.business_mcp.main:app` 실행은
동일한 조기 signal hook을 거치지 않으므로 권장하지 않는다.

## 조회와 변경의 실패 정책

- 조회: 연결/session/HTTP 오류를 성공으로 바꾸지 않고 `TEMPORARY_UNAVAILABLE`로 노출한다.
  인증 오류는 별도 코드다. MCP 클라이언트는 자동 재시도하지 않는다.
  Harness의 기존 호출당 최대 2회 추가 재시도·budget/backoff만 사용한다.
- 변경: `kick_user`를 포함해 MCP 내부 자동 재시도가 없다. SDK 자동 도구 재전송을
  피하기 위해 먼저 schema를 조회하고 low-level session API로 한 번 호출한다.
- Backend POST의 timeout, write/read/protocol/connect 오류, 성공 응답 JSON/형태 오류는
  `ACTION_OUTCOME_UNKNOWN` / nonretryable이다. 접속 실패도 보수적으로 불명확으로 분류한다.
  유효한 BE 업무 오류 코드는 보존한다.
- Agent의 trusted 변경 경계는 MCP 전송 오류·전체 deadline·잘못된 결과·retryable
  tool 오류를 `UNKNOWN`으로 저장한다. 예외 문자열/응답 본문은 외부에 전달하지 않는다.
- **같은 actionId의 `get_action_status`부터 조회**한다. `APPLIED`면 재집행하지 않는다.
  `PENDING`/`UNKNOWN`/조회 실패는 대기·복구·사람 확인 대상이며 새 action을 만들지 않는다.
  재시도가 별도 승인된 경우에도 원래 actionId·idempotencyKey를 유지한다.
  `FAILED`라는 상태만으로 자동 재집행을 허용하지 않는다.

## 장애와 로그

한 인스턴스를 종료해도 새 호출은 probe 수렴 후 남은 인스턴스로 간다.
진행 중 변경 응답을 잃으면 **해당 action 상태 확인**이 우선이다.
두 인스턴스가 모두 unavailable이면 LB/도구 오류를 명시적으로 반환하며 요청을 버리거나
업무 성공으로 간주하지 않는다. Agent Inbox 작업 복구 규칙은 그대로다.

```bash
docker compose --env-file .env -f deploy/business-mcp.compose.yaml stop mcp-1
curl --fail http://127.0.0.1:8002/ready
docker compose --env-file .env -f deploy/business-mcp.compose.yaml start mcp-1
```

토큰, 원문 채팅, tool 인자, 예외 본문은 health나 로그에 남기지 않는다.
Uvicorn access log와 HAProxy 요청 로그는 끄고, SDK/HTTP dependency 로그는
`dependency_event`로 대체한다(로거명·level 유지, traceback 제거).
Agent의 안전한 traceId/error-code 관측 이벤트로 업무 실패를 추적한다.
원문 확인을 위해 SDK DEBUG/HTTP body 로그를 운영에서 활성화하지 않는다.

## 검증과 한계

```bash
PYTHONPATH=tests LIVEGUARD_TEST_TCP=1 uv run --locked python -m unittest \
  test_business_mcp_cluster test_business_mcp test_agent_read test_http_gateway -q
```

cluster 테스트는 실제 두 ASGI MCP app과 SDK를 요청 단위 round-robin fixture로 연결한다.
같은 세션의 인스턴스 교차, session ID 없음, drain 제외, 전송 단절, 용량/슬롯 deadline,
Backend 적용 후 응답 유실 → UNKNOWN → 기존 action 조회(POST 1회), 민감값 로그 비노출을
검증한다. 기존 TCP 테스트는 실제 Uvicorn/SDK HTTP 연동을 검증한다.
실제 Backend/강퇴 외부 효과를 호출하는 시험은 아니다.

이번 회귀 실행은 격리 PostgreSQL + MCP TCP 모드에서 총 446개 중 440개 통과,
6개 skip이었다. skip은 로컬에 pgvector 확장이 없는 RAG DB 통합 시험이다.
Python compile, dependency 일관성, whitespace, Compose 환경변수 치환 검증도 통과했다.
Pylance 진단은 도구 timeout으로 완료하지 못했으므로 정적 타입 검사 통과로 보고하지 않는다.

### 실제 Docker 통합 검증 (2026-10-23)

Docker Engine 29.6.1 / Compose 5.3.0에서 공용 Dockerfile로 빌드한 Python 3.11
MCP 2대와 실제 `haproxy:3.0-alpine`, 가상 Backend를 실행해 검증했다.
운영 Compose를 그대로 사용하고 시험 전용 override로 임시 프로젝트·무작위 호스트 포트·
가상 credential·가상 Backend를 분리한다. MCP restart만 `no`로 바꿔 강제 종료 직후
자동 재시작이 장애 전환 관측을 방해하지 않게 한다.

```bash
PYTHONPATH=tests LIVEGUARD_TEST_DOCKER=1 uv run --locked python -m unittest \
  test_business_mcp_docker -v
```

| 시나리오 | 실제 컨테이너 결과 |
|---|---|
| 공통 주소를 통한 정상 조회 12건 | Backend가 관측한 MCP source IP 기준 6:6 분산 |
| 변경 적용을 기록한 뒤 응답 보류 → 처리 중 MCP SIGKILL | Agent `UNKNOWN`, Backend execute POST **1회** |
| 한 대 강제 종료 후 새 조회 | health check 수렴 후 살아 있는 MCP가 12/12 처리 |
| 응답을 잃은 기존 action 상태 조회 | 살아 있는 MCP를 통해 `APPLIED` 확인, 원래 actionId·멱등 키 유지 |
| 종료한 MCP force-recreate 후 복귀 | 다시 조회 12건이 6:6 분산 |
| 진행 중 조회에 SIGTERM을 보내는 graceful stop | 종료가 요청 완료를 기다리고 정상 응답 반환, 이후 생존 MCP가 12/12 처리 |
| 두 MCP 모두 종료 | 성공 대신 `TEMPORARY_UNAVAILABLE` 오류 |
| MCP·HAProxy 컨테이너 로그 | 가상 credential·변경 멱등 키 비노출 |

첫 시험과 진행 중 요청 drain을 추가한 재시험 모두 통과했다.
최종 통합 시나리오 1개는 약 37초에 완료됐고, 임시 컨테이너·네트워크·이미지 태그는
시험 종료 시 제거한다. 일반 unittest 실행에서는 Docker 시험 1개를 명시적으로 skip한다.
무관한 기존 Docker 리소스는 건드리지 않는다.

실제 Spring Backend나 외부 강퇴 서비스는 호출하지 않았다.
운영 부하·LLM quota·네트워크 분할·컨테이너 IP 변경 후 DNS 갱신을 검증한 것은 아니므로,
staging에서는 실제 BE 계약과 업무 효과의 멱등성까지 별도로 확인해야 한다.
