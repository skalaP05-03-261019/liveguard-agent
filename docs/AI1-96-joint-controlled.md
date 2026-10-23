# #96 실제 Spring BE 공동 통제 시험

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
- 기본 전송은 **DB polling**이다. 잔여 장애 실행기는 BE의 `rabbit.yaml`도 그대로
  조합한다. Rabbit은 BE 발송 알림 큐이며 Agent가 이 큐를 직접 소비하지 않는다.
  외부 실제 모델 시험은 별도다.

## 2. 버전과 이미지

BE의 `deploy/integration/versions.env`에서 BE 커밋·이미지 정본을 읽는다.
현재 확인한 BE는 `acd3e98f7d2b1a74505d9f89bfb56dffe1082673`,
`liveguard-be:integration-acd3e98f7d2b`다.

선행 실행기는 Agent `e720ce65f859a0312c4083e17b39974cf42c512d`에 포함되어 있다.
아래 잔여 장애 실행기는 **그 커밋에 추가한 작업 트리 변경**이다. 해당 HEAD만
checkout하면 잔여 실행기가 있다고 오인하지 않는다. BE 시험 자산은
`9a03d1a96dcafe7797a97c7e7d3560c55d3a4c99`를 확인했으며 업무 이미지는 위 고정 BE다.
BE 담당자에게는 변경이 반영된 Agent 커밋/PR과 빌드 이미지를
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

## 6. 잔여 장애 자동 시험

추가 실행기 `python -m app.joint_test.remaining`은 실제 Spring 채팅/승인 API로만
입력을 만든다. 매 suite는 **비어 있는 새 프로젝트/DB**를 요구한다.
BE DB 시각·상태·업무 row를 수정하거나 가상 접수/업무 응답을 생성하지 않는다.
기존 BE 스크립트의 고정 Agent 커밋/clean-source guard를 완화하지 않는다.
새 Agent 변경이 커밋·고정되기 전에는 아래 실행기를 직접 사용한다.

| suite | 실제 장애/검증 |
|---|---|
| `receipt` | 실제 API 202 영속화 뒤 HTTP 응답 절단 → BE 자동 GET → ACCEPTED. POST 1회, GET 1회 이상, Inbox 1개 |
| `race` | Spring 판정 커밋 뒤 응답을 잠시 보류. ANALYZE RUNNING 중 실제 승인 → resume QUEUED, run/resume 동시 중복 8건, 최종 job 2개·CONTEXT 1개 |
| `stale` | 모델 호출 중 소유 Worker SIGSTOP. 30초 lease 만료 후 다른 Worker 회수. 보류 응답 반환 및 SIGCONT 후 이전 프로세스가 살아 있어도 job/checkpoint/pending writes/blob 변경 없음 |
| `load` | 두 실제 live에 PII 정상 입력 24건 burst 또는 초당 1건 steady, 공정 admission·전량 소진, Graph 전체 4/Worker 2/모델 4 상한 및 대기·latency 관측 |
| `deadline` | 실제 3분 judgment fallback + KICK handoff. 결정 전 늦은 AI는 기존 검토에 반영, 결정 후 도착은 거절, 늦은 KICK 재개는 기존 실행 결과 재사용 |

`joint-extended-faults.yaml`은 BE → 실제 Agent API와 Worker Gateway → 실제 Spring
구간에만 투명 프록시를 넣는다. Business MCP는 그대로 실제 Spring에 연결한다.
BE/Gateway는 경로 접두사 없는 origin을 요구하므로 fixture의 `/agent/tasks/**`,
`/internal/**`가 원래 경로 그대로 중계한다. 인증 헤더는 실제 대상에서 검증된다.
제어 API는 별도 fixture 키가 필요하며 origin을 임의 입력으로 바꾸지 못한다.
판정 gate는 8초, 모델 gate는 240초로 유한하다.
프록시는 대상의 `Retry-After`도 보존한다. 기한은 호스트의 대기 시간만으로 추정하지
않고 실제 BE의 사건/검토 생성 시각과 승인/실행 시각 차이가 각각 180초 이상인지
검증한다. Worker를 중지해 기다리는 동안 RUNNING job이 0개인지도 확인한다.

BE는 `delivery_uncertain`을 OR 방식의 **이력**으로 유지한다. 접수 응답 유실 뒤
성공적으로 GET 조정되어도 `ACCEPTED` + `delivery_uncertain=true`가 정상이다.
이 필드를 false로 만들거나 다시 POST하는 것을 복구 조건으로 삼지 않는다.

```bash
# suite마다 새 PROJECT, 저장소 밖 새 ENV_FILE/REPORT를 지정한다.
PROJECT="liveguard-joint96-receipt-db"
ENV_FILE="$HOME/.liveguard-joint96-receipt-db.env"
REPORT="$HOME/liveguard-joint96-receipt-db.json"
TRANSPORT="db"       # Rabbit 조합은 rabbit. Agent 실행 대기열은 계속 공유 Inbox.
SCENARIO="normal"    # deadline suite만 fallback (3분), 그 외 normal (15분)
SUITE="receipt"     # receipt | race | stale | load | deadline

uv run --locked python -m app.joint_test.run init \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT"
uv run --locked python -m app.joint_test.run build \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT"
uv run --locked python -m app.joint_test.run up \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" \
  --extended-faults --transport "$TRANSPORT" --be-scenario "$SCENARIO"
uv run --locked python -m app.joint_test.remaining \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" \
  --suite "$SUITE" --transport "$TRANSPORT" --be-scenario "$SCENARIO" \
  --report "$REPORT"
uv run --locked python -m app.joint_test.run down \
  --be-repo "$BE_REPO" --env-file "$ENV_FILE" --project "$PROJECT" \
  --extended-faults --transport "$TRANSPORT" --be-scenario "$SCENARIO"
```

`init`은 Rabbit 전용 임의 `RABBITMQ_PASSWORD`도 만든다. 구 env에는 없으므로 새 env를
생성한다. Rabbit 관리 포트를 외부 공개하거나 별도 Agent Rabbit 큐를 추가하지 않는다.
`--input-count`로 부하 입력 수를 8~120 사이에서 지정한다(기본 24).
`--load-pattern burst`는 Worker를 잠시 중지해 전량 영속 접수한 뒤 실행한다.
`--load-pattern steady`는 Worker가 실행 중인 상태에서 약 1초 간격으로 입력하며
생산과 동시 관측을 진행한다. 생산 실패는 즉시 시험 실패로 전파한다.
부하는 정상 30 RPM/60k TPM을 유지하므로 24건도 quota 대기가 발생할 수 있다.
최대 480초 소진 한도를 넘기거나 실패·UNKNOWN이 발생하면 PASS로 덮지 않는다.
소량 유한 부하의 관측이며 장시간 soak/운영 SLO 인증이나 모델 품질 증거가 아니다.
`remaining --report`는 기존 보고서를 덮어쓰지 않는다. report와 private env는
서로 다른 경로로 지정하고 새 시험에는 새 보고서명을 사용한다.

### 수동 장애 주입

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

BE #78의 기존 검증은 동시 중복·업무 멱등성·MCP 장애·SIGKILL 회수·두 방송 공정성·
capacity 2/503 재시도 소진을 담당한다. 위 suite는 그 결과를 대역 시험으로 대체하지 않고
빠졌던 접수 유실/살아 있는 이전 소유자/실제 기본 기한을 보완한다.
마감 전후의 가능한 모든 interleaving, 장시간 soak, 대규모 운영 부하,
Rabbit broker 자체 장애와 실제 외부 모델 호출/품질 검증은 별도 범위로 남는다.

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

## 9. 2026-10-23 잔여 통제 시험 최종 실행

BE 시험 자산 `9a03d1a96dcafe7797a97c7e7d3560c55d3a4c99`의 공동 Compose와
고정 업무 이미지 `acd3e98f7d2b`를 재사용했다. 매 시험은 독립 프로젝트/새 DB에서
실행했고, 업무 DB 수정이나 가상 Backend 없이 실제 API·Worker·MCP를 사용했다.
최종 소스 기준 7개 실행이 모두 PASS다.

| 시험 | 관측 결과 | 기동 제외 전체 시간 |
|---|---|---|
| DB 접수 응답 유실 | 실제 202 뒤 절단, POST 1회 → 자동 GET 조정, Inbox 1개, Agent SUCCEEDED | 5.321초 |
| Rabbit 접수 응답 유실 | 기존 BE Rabbit 알림/HTTP 발송 경로로 같은 복구·중복 방지 확인 | 5.357초 |
| ANALYZE/조기 resume | ANALYZE RUNNING 중 실제 승인, resume QUEUED, 동시 중복 8건, 총 job 2개/CONTEXT 1개 | 4.966초 |
| 살아 있는 이전 Worker | SIGSTOP 후 다른 소유자 attempt 2, 지연 응답 반환·SIGCONT 뒤 job/checkpoint/pending writes/blob 불변, 이전 프로세스 생존 | 41.238초 |
| 두 방송 burst | 24/24 SUCCEEDED, Graph 최대 4/Worker 최대 2, 첫 4 admission에 두 방송 포함 | 78.653초 |
| 두 방송 steady | 약 초당 1건, 24/24 SUCCEEDED, 같은 동시성/공정성 조건 유지 | 72.586초 |
| 기본 3분 기한 | fallback·승인 전후 늦은 조사·KICK handoff/늦은 재개, 업무 효과 불변 | 201.130초 |

부하 경로의 생산/소진 측정은 인증·기동 전처리를 제외한다.
burst P95 latency는 67.569초, 최대 admission 대기는 66.580초였다.
steady P95는 46.684초, 최대 대기는 42.230초였다.
통제 모델은 즉시 응답하므로 실제 HTTP 동시 호출 관측 최대는 1이었다.
이는 4개 동시 모델 호출을 포화시켰다는 증거가 아니며 sLLM/실제 모델 처리량으로
환산하지 않는다. 모델 30 RPM/60k TPM과 Graph/Worker 제한은 완화하지 않았다.

DB에 기록된 fallback 검토 생성은 사건 생성 **187.394955초 후**,
KICK 실행 시작은 승인 **180.282773초 후**였다. 둘 다 정확한 180초 하한을 검증했다.

- fallback 검토가 아직 미결정이면 늦은 조사가 기존 검토에 판정을 붙이고 정상 재개했다.
- 사람이 먼저 확정한 사건은 늦게 실행된 ANALYZE가 모델 호출 전
  `FAILED/STALE_EVIDENCE`로 종료했고 `CONFIRMED_NORMAL`을 유지했다.
  이 기대된 거절을 Agent 성공으로 바꾸지 않는다.
- BE가 넘겨받아 실행한 KICK은 효과 1개였다. 뒤늦은 Agent resume이 성공해도 기존
  action ID/결과/효과 개수와 실행 시각은 바뀌지 않았다.

최종 Agent 이미지(작업 트리 기준, 아직 별도 커밋 아님):

- tag: `liveguard-agent:joint96-e720ce65f859-75e698`
- ID: `sha256:3fc3e01950cd364008da2e1781e5b402410f23e6ee69fd9682b7ee3d1f9aa13f`
- source fingerprint: `3bf4f86ba91976b85a3970caf4c1e419fd16d68f5ca87a61005ec65b28c74c7c`

관련 fixture/Inbox/Worker/복구/MCP 회귀는 임시 실제 PostgreSQL로 **96개 통과,
skip 0개**였다. Python compileall과 diff whitespace 검증도 통과했다.
Pylance 구문 도구는 timeout이어서 정적 분석 통과로 기록하지 않는다.
7개 보고서는 동일 이미지/source fingerprint인지 확인했고, credential·원문 채팅·
envelope·DSN·lease token이 없는지 검사한 안전한 JSON 증거로 묶었다.
시험 소유 컨테이너/네트워크/임시 DB 볼륨은 정리했으며 운영 리소스는 건드리지 않았다.
외부 실제 모델, 장시간 soak/운영 용량 인증, Rabbit broker 자체 장애는 실행하지 않았다.
