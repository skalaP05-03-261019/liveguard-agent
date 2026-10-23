# #96 선행 산출물: 실제 Spring BE 공동 통제 시험

관련: skalaP05-03-261019/liveguard-be#76 (환경),
skalaP05-03-261019/liveguard-be#78 (실제 업무 멱등성).

## 1. 연결 합의와 범위

**BE의 `deploy/integration/compose.yaml` + `normal.yaml`을 정본으로 사용한다.**
다른 BE/DB/API/Worker/LB를 중복 정의하지 않는다. Agent의
`deploy/joint-controlled.yaml`은 모델·정책 fixture 한 서비스와 Worker 시작 의존성만 추가한다.
BE 저장소 파일은 수정하지 않는다.

```text
실제 Spring 채팅 API → BE Outbox(DB polling) → 기존 API LB → API 1/2
  → 공유 Inbox/Checkpointer → Worker 1/2
    → 기존 MCP LB → Business MCP 1/2 → 실제 Spring /internal/**
    → controlled-fixture:8090/v1 (OpenAI 호환 통제 모델)
    → controlled-fixture:8090/mcp (통제 정책 조회)
운영자 seed 로그인 → 실제 CONTEXT/KICK 결정 API → BE resume Outbox → Worker
```

- Business MCP/Gateway를 가상 Backend로 대체하지 않는다. Gateway는 항상 실제 BE다.
- 정책 fixture는 저장소 `policy-v3.1.md` 조항/Resource의 동일 원문을 반환한다.
  검색·임베딩 정확도는 검증하지 않으며 외부 embedding API/DB 색인이 필요 없다.
- 통제 모델은 TARGET의 명시적 `[JOINT96:...]` 표식을 읽고 정확히 두 단계
  (`get_policy_section` → `finish_investigation`)를 실행한다.
  실제 BE messageId를 근거로 사용하고 실제 검증기·정책 Resource 조회·판정 제출을 통과한다.
  실행 한도 실패 후 생긴 수동 검토를 정상 조사 성공으로 세지 않는다.
- 모델은 실제 채팅/업무 명령을 추론하거나 운영자 승인을 대체하지 않는다.
  `JOINT_TEST_ENABLED=synthetic-only`와 인증이 필수다. 운영 환경에 배포하지 않는다.
- 일반 Agent 경로의 BE 판정/handoff 마감은 기존 `normal.yaml`의 15분이다.
  승인·재검증·fallback을 끄지 않는다. 3분 기본값/fallback 시험과 섞지 않는다.
- 이번 실행기는 **DB polling**을 사용한다. Rabbit·실제 모델 시험은 별도다.

## 2. 버전과 이미지

BE의 `deploy/integration/versions.env`에서 BE 커밋·이미지 정본을 읽는다.
현재 확인한 BE는 `acd3e98f7d2b1a74505d9f89bfb56dffe1082673`,
`liveguard-be:integration-acd3e98f7d2b`다.

Agent 기준 HEAD는 `af5730dd8948128494d76b08afcdd7d9076e03c9`이며 **이 신규 실행기는
그 커밋에 아직 포함되지 않은 작업 트리 변경**이다. 이 HEAD만 checkout하면 실행기가
있다고 오인하지 않는다. BE 담당자에게는 변경이 반영된 Agent 커밋/PR과 빌드 이미지를
함께 전달하고, 병합 뒤 BE의 버전 파일 갱신은 BE 담당자가 수행한다.

`init`은 `liveguard-agent:joint96-{HEAD 12자리}-{임의 suffix}` 태그를 생성한다.
`build`는 현재 Agent 작업 트리를 빌드하며 이미지에 다음을 기록한다.

- `org.opencontainers.image.revision`: 기준 HEAD (clean source 인증이 아님)
- `liveguard.test.source=worktree`
- `liveguard.test.source-sha256`: app/prompts/Dockerfile/manifest/lock의 fingerprint

보고서는 실제 이미지 ID와 위 fingerprint, 소스 기준 HEAD/dirty 여부,
BE 고정 revision, 정책 원문 SHA256을 기록한다. 수정 후에는 반드시 재빌드한다.
기존 BE 빌드 스크립트는 고정된 구 Agent도 함께 빌드하므로, 그 이미지를 신규 실행기로
오인하지 않고 아래 Agent `build`로 만든 이미지를 사용한다.

## 3. 준비·최소 정상 경로

필수: Docker/Compose, Git, Agent `uv` 환경. BE #76 변경이 있는 sibling checkout.
BE 고정 이미지가 없다면 먼저 BE 루트에서 `bash scripts/integration.sh build`를 실행한다.
아래 명령은 **Agent 루트**에서 실행한다.

```bash
# 저장소 밖의 본인 전용 경로를 사용한다.
ENV_FILE="$HOME/.liveguard-joint96.env"
PROJECT="liveguard-joint96-local"
BE_REPO="../liveguard-be"
REPORT="$HOME/liveguard-joint96-smoke.json"

uv run --locked python -m app.joint_test.run init \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT"
uv run --locked python -m app.joint_test.run build \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT"
uv run --locked python -m app.joint_test.run up \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT"
uv run --locked python -m app.joint_test.run run \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" \
  --suite smoke --handoff --report "$REPORT"
```

`init`은 기존 파일을 덮어쓰지 않고 mode 0600으로 독립 hex credential을 생성한다.
실제 모델 키/운영 token을 가져오지 않는다. BE 포트 18080, Agent LB 18001,
MCP LB 18002, fixture 18004는 모두 loopback이다.
동시 시험은 **고유 project와 별도 env**, 충돌하지 않는 4개 host port가 필요하다.
`init` 후 private env에서 port만 수정할 수 있다.
명령에 token/DSN 값을 인자로 넣거나 전체 Compose 렌더링을 출력하지 않는다.

필수 env는 생성 파일에 채워진다.

| 구간 | 설정 |
|---|---|
| 실제 BE/Agent DB | `BE_DB_PASSWORD`, `AGENT_DB_PASSWORD` — 독립 생성 |
| BE → Agent | `AGENT_SERVICE_TOKEN` |
| Gateway/MCP → BE | `INTERNAL_SERVICE_TOKEN` |
| Worker → Business MCP | `BUSINESS_MCP_AGENT_TOKEN` |
| Worker → 통제 정책 | `RAG_MCP_AGENT_TOKEN`, `RAG_MCP_URL=http://controlled-fixture:8090/mcp` |
| Worker → 통제 모델 | `MODEL_PROVIDER=openai-compatible`, `MODEL_BASE_URL=http://controlled-fixture:8090/v1` |
| 모델 이름/공유 quota | `MODEL_NAME=joint-controlled-v1`, `MODEL_QUOTA_GROUP=joint96-controlled` |
| fixture 인증 | 임의 생성 `MODEL_API_KEY` — 외부 공급자 키가 아님 |
| 이미지 | 생성 `AGENT_IMAGE`, BE 버전 파일의 `BE_IMAGE` |

기존 BE Worker 설정인 Graph 전역 4 / Worker별 2 / 모델 전역 4, 30 RPM,
60,000 TPM, 모델 30초 timeout을 유지한다.

### Smoke PASS의 의미

실제 seed viewer가 채팅 API에 합성 인용문 1개를 전송한다.
`꺼져`라는 공격 사전 표현은 **비대상 합성 인용문**에만 포함하여 CONTEXT_REVIEW를 유도한다.
생성된 실제 case/task/thread를 조회하고:

1. BE Outbox `ACCEPTED`와 Agent `WAITING_REVIEW`를 각각 확인
2. 실제 BE에 `NO_VIOLATION` AI 판정이 저장됐는지 확인
3. 실제 moderator 로그인으로 `CONFIRM_NORMAL` 결정
4. `--handoff`이면 이전 Worker를 중지하고 다른 Worker가 동일 thread/decision 재개함을 확인
5. Agent `SUCCEEDED`와 BE `CONFIRMED_NORMAL`을 별도로 확인

모든 시험은 미사용 seed 방송을 요구한다. 재실행 시 기존 자료를 임의 삭제/보정하지 않는다.

## 4. 합성 입력·전체 정상/KICK 경로

`--suite full`은 아래 5개 입력을 **결정 전에 먼저 생성**한다.
첫 위반 확정 후 BE timeout에 의해 다음 메시지가 거부되는 것을 우회하지 않기 위해서다.
각 메시지에 다른 suffix를 넣어 도배 사건으로 묶이지 않게 한다.

| 입력 | 통제 AI | 실제 운영자 결정 |
|---|---|---|
| context-normal | NO_VIOLATION | CONFIRM_NORMAL |
| context-defer-final | NEEDS_REVIEW | DEFER → 같은 review의 최신 version으로 CONFIRM_NORMAL |
| context-violation-1 | VIOLATION / HARASSMENT | CONFIRM_VIOLATION |
| context-violation-2 | VIOLATION / HARASSMENT | CONFIRM_VIOLATION |
| context-violation-3-kick | VIOLATION / HARASSMENT | CONFIRM_VIOLATION → **별도 KICK APPROVE** |

마지막 CONTEXT 결정 뒤에도 Agent는 KICK 승인 대기에 있어야 한다.
BE가 만든 실제 서로 다른 basis case 3개를 확인한 후 별도 KICK 결정을 저장한다.
Backend 재검증/Business MCP 집행/기존 action 조회가 끝나야 KICK 완료로 판정한다.
AI finding으로 직접 강퇴하지 않는다.

```bash
# 반드시 미사용 seed 방송을 가진 별도 시험 프로젝트/볼륨에서 실행
uv run --locked python -m app.joint_test.run run \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" \
  --suite full --handoff --model-fault model-timeout \
  --report "$HOME/liveguard-joint96-normal.json"
```

`--model-fault model429`는 최초 통제 응답에 429/Retry-After 1을 한 번 반환한다.
`model-timeout`은 한 번 35초 지연해 기존 30초 timeout을 실제로 넘긴다.
둘 다 기존 Harness의 재시도·공유 admission을 통과해야 판정이 저장된다.
실제 공급자 성능/429 대응 인증은 아니다.

## 5. 실제 BE 적용 후 응답 유실

미사용 프로젝트를 시작할 때와 실행할 때 **모두** `--action-loss`를 지정한다.
`up`에 이 옵션을 빼면 MCP는 Spring에 직접 연결된다.

```bash
uv run --locked python -m app.joint_test.run up \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" --action-loss
uv run --locked python -m app.joint_test.run run \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" \
  --suite full --handoff --action-loss --model-fault model429 \
  --report "$HOME/liveguard-joint96-loss.json"
```

선택적 `joint-action-loss.yaml`만 MCP의 BE 주소 앞에 **투명 시험 프록시**를 놓는다.
실제 Spring에 서비스 token·trace와 원래 요청을 전달하고, Spring 성공 응답을 받은 뒤
KICK 응답 body를 중간에 끊는다. 가짜 action/승인/업무 응답을 생성하지 않는다.
원문·명령·credential은 프록시 보고서에 저장하지 않는다.

기대 결과:

- 실제 BE의 KICK action은 `SUCCEEDED`, 동일 decision의 KICK action 수 1
- 프록시 execute 호출 1회, 유실 1회
- Agent는 `UNKNOWN` 또는 이후 `NEEDS_REVIEW/ACTION_CONFIRMED_REQUIRES_REVIEW`
- action을 실행한 MCP를 중지하고 **다른 MCP**로 동일 action 상태 확인
- 상태 확인이 성공해도 전체 Graph 실행을 `SUCCEEDED`로 재분류하지 않음

보고서 최상위 PASS는 **이 안전 조건을 충족했다는 뜻**이다.
Agent 실행/BE 조치 성공을 한 필드로 합치지 않는다.
BE #78의 업무 중복 방지 전체 보장은 이 한 번의 관측으로 대체하지 않는다.

## 6. 추가 장애 주입과 미완료 범위

동일 private env/project를 사용해 다음 명령으로 본인의 Worker/MCP만 제어할 수 있다.

```bash
uv run --locked python -m app.joint_test.run observe \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT"
uv run --locked python -m app.joint_test.run fault \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" \
  --target worker-1 --fault kill
# lease 30초가 만료된 뒤 다른 Worker가 회수하는지 observe / 보고서로 확인
# 다시 기동: --target worker-1 --fault start
```

실행 중 RUNNING의 실제 소유 Worker를 `observe`에서 먼저 확인한다.
모델 timeout 주입은 실행 중 강제 종료를 넣을 시간 창도 제공한다.
`kill`은 SIGKILL, `stop`은 유예 종료, `start`는 재기동이다.
target은 Worker 1/2 또는 MCP 1/2로 제한한다. DB/전체 호스트를 종료하지 않는다.

이번 선행 산출물에서 자동 검증하지 않은 #96 잔여 항목:
동일 operation 동시 접수/BE 응답 유실 재전송, 같은 thread ANALYZE/resume 경합,
Worker 실행 중 강제 종료·lease 만료·늦은 결과 fencing, 다중 방송 공정성·대량 유입·
용량 초과 후 소진·처리량 병목, BE fallback/마감 경합, Rabbit 및 실제 모델 별도 실행.
기존 단위 검증을 위 실제 BE 시나리오의 통과로 간주하지 않는다.
이슈 #96 전체 완료/운영 부하 인증을 주장하지 않는다.

## 7. 안전한 결과 수집·종료

`run --report`는 식별자, 상태·오류 코드, Worker ID, 시각·대기/소요 시간,
이미지 ID와 정책 hash, 통제 호출/오류 횟수만 저장한다.
`waitSeconds`/`lastAttemptSeconds`는 Inbox의 최신 admission 시각 기준 값이다.
원문 채팅, 모델 messages, tool 인자, 명령 본문, token/DSN/lease token은 저장하지 않는다.
`observe`도 고정 SELECT allowlist만 출력한다. HTTP/프로세스 실패는 본문 없이 코드로 기록한다.

```bash
uv run --locked python -m app.joint_test.run down \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT"
```

`down`은 본인 프로젝트 컨테이너·네트워크만 정리하며 DB 볼륨은 증거 보존을 위해 남긴다.
미사용 방송으로 다시 시험할 때는 새 project를 쓰거나, 결과 보존 후 소유자가
본인 프로젝트의 **정확한 두 볼륨 이름**을 확인해서 제거한다.
`docker system prune`, 전체 볼륨 삭제, 업무 DB 수동 보정은 사용하지 않는다.
private env는 재현/재기동에 필요하므로 종료 전 임의 삭제하지 않는다.

## 8. 2026-10-23 실제 실행

실제 BE 고정 이미지 + 위 Agent 작업 트리 이미지로 두 차례 전체 실행:

| 시험 | 입력/결과 | 실행 시간(기동 제외) |
|---|---|---|
| 직접 Spring 연결 + 모델 timeout 1회 + Worker handoff | 5개 사건 모두 예상 판정/결정, 최종 KICK SUCCEEDED | 61.338초 |
| Spring 투명 프록시 + 모델 429 1회 + Worker handoff + KICK 응답 유실 | 5개 사건 예상 결과, 변경 1회, 다른 MCP 상태 확인, Agent NEEDS_REVIEW | 41.963초 |

두 실행 모두 CONTEXT 5회·DEFER 재개 1회·별도 KICK 승인 1회를 실제 BE에서 처리했다.
AI 호출은 각 11회(정상 10회 + 주입 실패/지연 1회)였고 외부 모델은 호출하지 않았다.
Agent image ID:
`sha256:f7a7ffc68e3a28da20c6bb00e90a387354399d5474e4f1f4b88ff3dae9445c47`.
이후 실행기 수정/재빌드 시 새 ID를 보고서에서 확인한다.
