# 후보 API · BE 사건 검증 · 조사 인계 (#63)

## 구현 경계

태관의 독립 후보 API와 세원님의 조사 모듈 사이에서 사용할 Agent 계약입니다. **BE가 합의한 OpenAPI나 배포 완료 선언이 아닙니다.** 시작 Agent main은 `7db3f7b18c83a4a37ec8576e5722edd960ff0930`이며, 합의된 `phase1-end` 태그는 아직 없습니다.

확인한 BE main은 `8f0d4d080a5bfce2b47b0c92d63c30fc9464f2f6`입니다. #40은 병합됐고 사건·snapshot 조회를 재사용합니다. 현재 `contracts/openapi.yaml`에는 Phase 2 후보를 접수하여 사건을 생성하는 경로가 없습니다. 후보 응답을 받은 뒤 누가 어떤 조건으로 HELD 사건과 TaskEnvelope를 만들지는 BE와 합의해야 합니다. Agent가 임의의 사건 생성 API를 호출하거나 PUBLIC 메시지를 숨기지 않습니다. BE #58의 문맥 범위 변경은 확인 당시 미병합이므로 실제 배포 전 다시 확인합니다.

```text
인증된 서비스 → 후보 API → 신호/selected
                     ↓ BE 후보 접수·사건 생성은 후속 합의
BE가 만든 TaskEnvelope → 기존 MCP 사건·문맥 조회 → prepare_handoff
 → 세원 investigate(handoff) → 기존 AnalysisResult 검증 → 조사 초안
```

이번 경로는 초안 반환에서 끝납니다. 기존 Graph의 판정 제출·CONTEXT/KICK interrupt·운영자 결정·BE 재검증·집행 흐름은 변경하지 않습니다. 새 API가 검토 카드를 만들거나 실제 제재를 수행하지 않습니다.

## 후보 API 실행

기존 `app.main`/Inbox/Worker #60과 별도 프로세스입니다. 다음 환경변수는 서버의 비공개 환경 파일로 설정합니다.

```dotenv
CANDIDATE_API_ENABLED=true
CANDIDATE_SERVICE_TOKEN=replace-with-separate-service-secret
MODEL_PROVIDER=openai
MODEL_NAME=your-approved-model
MODEL_API_KEY=replace-with-valid-key
```

```bash
uv run --env-file .env.candidate uvicorn app.candidate_detection.api:app --host 127.0.0.1 --port 8006
```

`POST /agent/candidates/screen`에 `Authorization: Bearer <CANDIDATE_SERVICE_TOKEN>`과 JSON을 보냅니다. 브라우저 로그인 비밀번호와 다르며 FE에 토큰을 배포하지 않습니다. 기본값은 비활성입니다. `/health`의 `configured`는 설정 여부이며 모델 연결이나 처리 품질을 증명하지 않습니다.

- 입력: [screen-request.json](../../examples/phase2-handoff/screen-request.json)
- 출력 형식: [screen-result.json](../../examples/phase2-handoff/screen-result.json)
- JSON Schema: [schemas](../../examples/phase2-handoff/schemas)

`selected`는 실험 설정에 따른 조사 후보 여부입니다. 위반 확정·고객사 정책·차단 승인이 아닙니다. `executionAuthorized`는 항상 false입니다. 신호 종류를 기존 BE categories로 자동 치환하지 않습니다.

| 상태 | 의미 |
|---|---|
| 200 | 검증된 후보 판별 결과. selected=false도 정상 응답 |
| 401 | 서비스 인증 실패. 본문/모델을 읽기 전에 거절 |
| 413 / 422 | 32 KiB 초과 / 입력 계약 오류 |
| 429 | 프로세스 동시 처리 한도 초과. 기본 2건, 대기열 없음 |
| 502 | 모델 출력 형식·근거·허용 도구 검증 실패 |
| 503 | 비활성·인증 미설정·모델 의존성 실패 |
| 504 | 후보 모델 호출 시간 초과. 기본 8초 |

원문과 예외 문자열은 오류 응답에 반영하지 않습니다. 정상 응답의 인용·조사 인계에는 채팅이 포함되므로 내부 인증 서비스 간에만 전달하고 로그로 남기지 않습니다. 동시 처리 한도는 프로세스별입니다. 분산 속도 제한·결과 캐시·중복 모델 호출 방지는 후속 범위이며 requestId만으로 멱등성을 보장하지 않습니다. 자동 재시도하지 않습니다.

## 세원님이 연결할 인터페이스

먼저 [investigation-request.json](../../examples/phase2-handoff/investigation-request.json)과 [investigation-draft.json](../../examples/phase2-handoff/investigation-draft.json)을 사용하면 됩니다. 모든 예시는 직접 작성한 통제 데이터이며 실제 BE/모델 출력이나 UnSmile 데이터가 아닙니다.

```python
from app.phase2_bridge.contracts import InvestigationDraft, InvestigationHandoff
from app.phase2_bridge.handoff import run_readonly_investigation

class Investigator:
    async def investigate(self, request: InvestigationHandoff) -> InvestigationDraft:
        # request.screening.assessment: 후보 신호. 정책 결론으로 간주하지 않음
        # request.snapshot / request.messages: 검증된 BE 사건·동결 문맥
        # 기존 조사 모듈로 분석하고 실제 조회한 정책 근거를 함께 반환
        ...

draft = await run_readonly_investigation(handoff, Investigator())
```

`prepare_handoff(task=..., request=..., result=..., business=...)`는 기존 `get_moderation_case`와 `get_chat_context`만 호출합니다. 서비스 인증과 BE 발행 작업을 신뢰할 수 있는 내부 수신 경로에서 받은 뒤 호출해야 합니다. 임의 JSON의 UUID만으로 발행 주체를 인증하지 않습니다. 다음 조건을 만족해야 조사에 넘깁니다.

- 최초 requestId·실험 설정·liveId·대상 messageId와 판별 결과가 일치
- 작업과 사건의 caseId·snapshotId·evidenceVersion 및 MCP traceId 일치
- CONTEXT_REVIEW이고 미확정인 사건, 명시된 대상 전부 HELD
- 후보 대상이 BE의 대상에 포함되고 인용이 동결 문맥에 실제 존재

후보 원문으로 BE 증거를 교체하지 않습니다. 문맥/가시성 변경이나 stale evidence는 거절합니다. 조사 출력은 기존 정책 버전·ID·대상·enum·인용 검증을 통과해야 합니다. 새 혐오 신호에 승인된 정책·제재 유형이 없으면 예시처럼 **NEEDS_REVIEW, categories=[]**로 남깁니다.

선택적으로 `ExistingInvestigator(model=..., business=..., policy=...)`가 기존 `guarded_investigate`와 실행 예산을 재사용합니다. 기존 프롬프트는 변경하지 않았으므로 신규 후보 힌트를 직접 읽도록 확장한 조사 모델은 아직 아닙니다. 세원님은 위 인터페이스의 `screening.assessment`를 활용하는 조사 구현을 연결할 수 있습니다. 콜백은 신뢰된 서비스 코드이며, 실행 시간 상한은 120초입니다. 분석 초안을 기존 제출 Gateway에 연결하는 작업은 별도 검증 후 진행합니다.

## 실행 가능한 통제 예제

```bash
uv run python -m app.phase2_bridge example --fixture examples/phase2-handoff/controlled-flow.json
```

로컬 가짜 BE 2회 조회 → 인계 검증 → 가짜 조사 NEEDS_REVIEW → 기존 출력 검증까지 실행합니다. 실제 모델·BE 사건 생성·운영자 승인·집행을 입증하는 예제가 아닙니다.

실제 기존 사건을 **조회만** 검사하려면 기존 Business MCP 인증·주소 환경변수를 설정하고, 해당 사건의 실제 입력/결과/작업 파일로 실행합니다. 예제 UUID로 실제 BE 조회가 성공한다고 가정하지 않습니다.

```bash
uv run --env-file .env python -m app.phase2_bridge inspect --task task.json --request request.json --result result.json
```

출력은 원문 없이 참조 ID·버전·상태만 표시합니다. 조사/판정 제출/승인은 실행하지 않습니다.

스키마 재생성은 빈 출력 디렉터리에 실행합니다.

```bash
uv run python -m app.phase2_bridge schemas --out-dir /tmp/liveguard-phase2-schemas
```

## 검증과 남은 작업

검증 기록: [evidence/phase2-handoff-63.json](../../evidence/phase2-handoff-63.json). 격리된 PostgreSQL/pgvector와 TCP 활성화 환경에서 전체 **296개 통과, skip 0**을 확인했습니다. 별도 실제 모델 TCP smoke에서는 인증 없는 요청 401, 인증한 요청 200·신호 1건·selected=true·executionAuthorized=false를 관찰했습니다. 입력은 직접 작성한 예제 1건으로 정확도나 처리량 근거가 아닙니다. BE 쓰기는 하지 않았습니다.

`tests/test_phase2_bridge.py`는 인증·스트리밍 본문 제한·오류 원문 비반영·동시 처리·타임아웃·실제 TCP·BE 버전/문맥/trace 불일치·초안 위조·기존 조사 어댑터를 검사합니다. 실제 모델 품질은 #61 개발 진단과 별도입니다. 이 PR로 고객사 스트리밍 처리량이나 실서비스 탐지율을 측정하지 않았습니다.

후속 순서는 BE 후보 접수 계약 합의 → 세원 조사 연결·정책 근거 평가 → 기존 승인 Graph 연결 → 역할별 FE/실제 사건 통합 테스트입니다. 요약 기술의 사용 역할·시점은 계속 미정입니다.
