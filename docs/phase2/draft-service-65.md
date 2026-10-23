# BE 조회 → 조사 초안 실행 서비스 (#65)

## 범위와 최신 소스

#64의 인계 계약에 기존 `ExistingInvestigator`를 실제 연결한 독립 API/CLI입니다. 세원님의 신규 조사 PR이 없으므로 기존 조사 프롬프트·정책·요약 모듈은 변경하지 않습니다. 새 모듈이 올라오면 `DraftService(business=..., investigator=...)`의 investigator만 교체할 수 있습니다.

- Agent 시작 main: `e3d0dd46d8c14778121fda77ca7ab09a16d83080`
- 확인한 BE main: `1736c2c2107debfc502dd8af1d1fc70d8e1e8b12`
- BE #58 문맥 snapshot 범위, #59 가시성 알림 변경이 병합됐습니다. 기존 MCP의 before=5/after=5는 새 0~10 계약 안에 있으며 공유 조회 코드나 BE 구현은 수정하지 않습니다.
- BE #60은 확인 시 open입니다. 운영 중 기존 스택은 최신 BE main 재배포 증거가 아닙니다.

반환은 **조사 초안**입니다. BE 판정 제출, 사건 생성, review 생성, 승인/재개, 집행, Worker #60은 호출하거나 변경하지 않습니다. 세원님의 신규 탐지 유형별 설명 품질을 구현 완료했다고 주장하지 않습니다.

## 별도 API 실행

비공개 환경 파일에 다음 설정을 둡니다. 후보 API 토큰·브라우저 세션과 구분합니다.

```dotenv
INVESTIGATION_DRAFT_API_ENABLED=true
INVESTIGATION_DRAFT_SERVICE_TOKEN=replace-with-separate-service-secret
MODEL_PROVIDER=openai
MODEL_NAME=your-approved-model
MODEL_API_KEY=replace-with-valid-key
BUSINESS_MCP_URL=http://business-mcp:8002/mcp
BUSINESS_MCP_AGENT_TOKEN=replace-with-business-mcp-token
RAG_MCP_URL=http://rag-mcp:8003/mcp
RAG_MCP_AGENT_TOKEN=replace-with-rag-mcp-token
```

주소는 Compose 내부 예시입니다. 호스트 실행에서는 도달 가능한 주소를 명시합니다. 임의 호출자가 JSON으로 MCP URL·토큰·조사 콜백을 지정할 수 없습니다.

```bash
uv run --env-file .env.draft uvicorn app.phase2_bridge.api:app --host 127.0.0.1 --port 8007
```

`POST /agent/investigations/draft`, `Authorization: Bearer <INVESTIGATION_DRAFT_SERVICE_TOKEN>`으로 호출합니다. 입력은 [draft-request.json](../../examples/phase2-handoff/draft-request.json), 형식은 [DraftRequest Schema](../../examples/phase2-handoff/schemas/DraftRequest.json)와 [DraftResponse Schema](../../examples/phase2-handoff/schemas/DraftResponse.json)를 참조합니다. 예제 UUID는 실제 BE에 존재하지 않습니다. 실제 BE 발행 작업과 그 후보 판별 입력·결과를 사용해야 합니다. 서비스 토큰은 신뢰된 내부 발신자를 인증하며 작업 JSON 자체가 BE 발행 사실을 암호학적으로 증명하지는 않습니다.

2026-10-23 회의 기준으로 유효한 판별 결과의 `selected=false`는 BE가 이미 보류한
후보의 조사를 막지 않습니다. 조사 허용 조건은 BE의 `CONTEXT_REVIEW`·`HELD`
사건과 작업·대상·증거 버전의 일치입니다. 이 독립 Draft API는 아직 판별 결과를
입력으로 요구합니다. 판별 호출 자체가 실패했다면 이 API에 가짜 음성 결과를
만들어 넣지 말고, 기존 BE→Agent Inbox/Worker 조사 경로를 유지합니다.

기본 비활성, 동시 처리 기본 1건/프로세스, 본문 최대 64 KiB입니다. 시간 상한 120초는 초기 BE 조회와 조사 전체를 포함합니다. 기존 Harness의 모델 8회·동일 도구 3회·재시도/활성 시간 한도를 재사용합니다. API는 자동 재시도하지 않습니다. 작업을 저장·접수하는 202 API가 아니며 검증된 초안을 얻은 경우에만 200입니다.

| 응답 | 의미 |
|---|---|
| 200 | 참조·버전·정책 근거를 검증한 초안. 의미적 정확도나 집행 완료를 보장하지 않음 |
| 401 | 요청 서비스 인증 실패. 본문을 읽기 전 거절 |
| 409 | 후보/사건/버전/정책 충돌. 성공 초안 없음 |
| 413 / 422 | 본문 크기 초과 / 입력 스키마 오류 |
| 429 | 동시 처리 한도 초과, 대기열 없음 |
| 502 | 모델·도구·정책 근거 출력 검증 실패 |
| 503 | 비활성/설정 누락/의존성 실패/실행 예산 소진 |
| 504 | 전체 실행 또는 조사 시간 초과 |

정상 응답은 requestId/taskId/traceId/liveId, `draft.analysis`, 실제 조회한 `policyEvidence`, 성공 시 `harnessBudget`과 항상 false인 `executionAuthorized`를 포함합니다. 내부 원문·정책이 포함되므로 FE 공개 응답이나 일반 로그로 그대로 전달하지 않습니다. 오류에는 원문·예외 문자열·상류 서비스의 임의 코드를 반영하지 않습니다.

이 경로에는 영속 작업 저장·중복 제거·실패 예산 복원이 없습니다. 같은 입력 재호출은 모델 비용을 다시 발생시킬 수 있습니다. 기존 Inbox/Worker의 재시작 복구를 대신하지 않으며 실패 후 자동 재실행을 붙이지 않습니다. `/health.configured`는 설정 유무만 나타냅니다.

## CLI: 실제 기존 사건을 조회하고 초안 생성

```bash
uv run --env-file .env.draft python -m app.phase2_bridge draft --input request.json --output /tmp/liveguard-draft-result.json
```

이 명령은 명시적 조사 실행입니다. API enable 플래그·수신 토큰은 CLI에 필요하지 않고 모델/Business/RAG 설정이 필요합니다. stdout에는 참조·판정 목록만 표시합니다. `--output`을 지정하면 초안 전체를 새 파일에 0600 권한으로 저장하며 기존 파일을 덮어쓰지 않습니다. 원문이 포함될 수 있으므로 결과 파일을 Git에 커밋하지 않습니다. `--output`을 생략하면 결과 원문 파일을 만들지 않습니다.

## 관찰 결과와 해석

검증 기록은 [evidence/phase2-draft-65.json](../../evidence/phase2-draft-65.json)에 남깁니다.

- 격리 PostgreSQL/pgvector + TCP 활성화에서 전체 **310개 통과, skip 0**.
- 통제 BE 증거 + 실제 모델 + 실행 중 RAG MCP/원문 Resource를 사용하는 TCP API smoke: 인증 없음 401 / 인증 성공 200, 모델 2회, 정책 근거 3개, VIOLATION 초안 1개, 실행 권한 false, BE 쓰기 0.
- 위 smoke는 예제 1건의 **연결·근거 검증 관찰**입니다. 위반 의미의 타당성이나 신규 혐오 정책의 승인·탐지 정확도를 평가한 결과가 아닙니다. 기존 모델이 기존 정책으로 위반을 제안할 수 있으므로 세원 조사 PR과 정책 대응표가 올라오면 동일 사례의 정상 예외/보류/오탐을 함께 평가해야 합니다.
- 실행 중 실제 Business MCP→BE의 기존 사건 조회도 성공했습니다. 조회된 사건은 이미 확정된 FLOOD_REVIEW로, 신규 CONTEXT 인계의 증거로 사용하지 않았습니다. 해당 스택은 최신 BE main 재배포가 아닙니다.
- Phase 2 후보 사건 생성, 실제 신규 CONTEXT 사건의 전체 인계, FE 카드·SSE·사람 결정·강퇴는 이번 경로에서 미실행입니다.

## 남은 합의

[BE 후보 접수 협의안](candidate-ingress-proposal.md)을 기준으로 고객사/방송/메시지 범위, 원문·마스킹, HELD 시점, 중복, 모델 실패 처리, 후보→사건 TaskEnvelope 반환을 맞춰야 합니다. 경로나 DTO를 Agent 임의로 확정하지 않습니다. 이후 세원 조사 품질 검증 → 기존 승인 Graph 통합 → FE/실제 사건 검증 순서로 진행합니다.
