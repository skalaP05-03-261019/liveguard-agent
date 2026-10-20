# #9 실제 GPT-5.4 mini 검증

## 검증 범위

통제된 가상 사건 → 생산 Business MCP 앱 → 실제 OpenAI GPT-5.4 mini → 생산 RAG MCP 앱 →
실제 질의 임베딩·pgvector SQL·조항 Resource → 판정 근거 검증.

Business 의존성은 합성 fixture이며 실제 Spring Backend가 아니다. MCP 앱과 SDK는 실제로 사용하고,
HTTP 전송은 인증된 ASGI 인프로세스 전송이다. 이 문서의 검증만으로 TCP 배포·실제 Backend 연결을 주장하지 않는다.
실제 모델·임베딩·DB가 없으면 대역으로 대체하지 않고 BLOCKED로 종료한다. 조치는 실행하지 않는다.
기존 테스트 84개와 이번 실행기의 오프라인 회귀 검증은 실제 모델 품질 평가와 구분한다.

- 모델: `gpt-5.4-mini` (사용자 선택), 현재 Chat Completions function calling 유지.
- 임베딩: `text-embedding-3-small`, 1536차원, 색인과 검색 동일 모델.
- 정책: PR #16 head `fd3988fffcc85d5a56cdfdd9054377dc284ecf85`의 v3.1 원문 스냅샷.
- 원문 SHA-256: `c34ae086df1254ee32190f3a02839aaa4edda2e6e9450b4b9b814dd3787e8f2d`.
- #16이 미병합인 동안 검증 fixture로만 보존하며, production 정책 기본값을 변경하지 않는다.
  #16의 CP-4.2→FLOOD, CP-5.2→PHISHING 추가 매핑만 검증 chunk에 적용한다.
- policyVersion=3.1로 29조항을 전용 DB에 색인한다. 공용 DB는 사용하지 않는다.

## 실행

다운로드 `.env`는 `OPENAI_API_KEY`만 있어도 된다. 모델 호출은 비어 있지 않은 `MODEL_API_KEY`를 우선하고,
없으면 `OPENAI_API_KEY`를 사용한다. 임베딩은 `OPENAI_API_KEY`를 사용한다. 키 파일을 복사/커밋/출력하지 않는다.
기존 `app.check_investigation`도 이 fallback을 지원하며 MODEL_PROVIDER/NAME은 여전히 명시해야 한다.
이번 검증 CLI는 사용자 선택 모델/임베딩/정책을 고정하며 환경변수로 모델을 몰래 바꾸지 않는다.

전용 임시 DB 준비(예시 비밀번호는 로컬 검증용, 실제 비밀값 아님):

```bash
docker run --rm -d --name liveguard-eval-9 \
  -e POSTGRES_USER=ragverify -e POSTGRES_PASSWORD=local-verification-only \
  -e POSTGRES_DB=liveguard_eval -p 127.0.0.1:15434:5432 pgvector/pgvector:pg16
docker exec liveguard-eval-9 pg_isready -U ragverify -d liveguard_eval
```

저장소 루트에서:

```bash
uv sync --locked
uv run --locked --env-file "$HOME/Downloads/.env" python -m app.verification.run \
  --mode smoke \
  --db-url postgresql://ragverify:local-verification-only@127.0.0.1:15434/liveguard_eval \
  --output evidence/live-smoke.json

# 위 결과 PASS인 경우에만 실행. 모델·정책·사례 해시가 같은 PASS 보고서 필수.
uv run --locked --env-file "$HOME/Downloads/.env" python -m app.verification.run \
  --mode full --smoke-report evidence/live-smoke.json \
  --db-url postgresql://ragverify:local-verification-only@127.0.0.1:15434/liveguard_eval \
  --output evidence/live-full.json

# 임시 DB와 컨테이너를 제거한다(--rm, 별도 영속 볼륨 없음).
docker stop liveguard-eval-9
```

DB 이름은 `liveguard_eval` 접두사가 필수다. 파서 검사를 통과한 전용 DB만 DDL/색인을 실행한다.
색인은 실행마다 버전 3.1을 교체한다. **OpenAI 임베딩/모델 실제 호출 비용이 발생**한다.
401/403/모델 접근 불가/DB 접속 차단 시 상태를 기록하고 무한 재시도하지 않는다.
모델 SDK 자동 재시도는 0회다. 임베딩 SDK는 기존 제공자 기본 정책을 유지한다.

## 사례와 결과 판단

15건의 입력과 기대값은 `tests/fixtures/live-verification/cases.json`에 실행 전에 고정했다.
정답은 모델이나 도구 응답에 넣지 않는다. 의미 판단의 테스트 기대값은 자체 정책에 기반한 평가 기준이며 팀 최종 판정과 구분한다.

- smoke 3건: 정상 감탄, 표적 괴롭힘, 개인정보 실체 미확인. 첫 실패에서 중단한다.
- full 15건: 정상 5, 위반 제안 4(괴롭힘/사칭/도배/가상 PII), 판단 보류 4, 규칙 경로 2.
- 규칙 경로는 금지 욕설 fixture 신호와 링크 신호로 AI를 건너뛴다. **실제 Backend 욕설 사전·정규식 구현을 검증한 것이 아니다.**
- 도배는 동일 가상 작성자·동일 문구 3건을 4초 창 안에 배치한다. 개인정보는 실제 번호 없이 가상 식별자만 쓴다.
- v3.1의 문맥 판정은 AI의 제안이다. 정상도 운영자 결정 전 자동 공개하지 않으며 이 실행기는 HELD/업무 상태를 변경하지 않는다.
- 각 사례는 verdict 일치, 위반/정상 categories 일치, 지정 정책 중 하나 이상 참조를 비교한다.
  NEEDS_REVIEW는 의심 유형을 적을 수 있어 categories 배열을 확정 위반처럼 비교하지 않는다.
- 별도로 기존 validation이 TARGET·메시지·정책 ID·버전·정상/위반 근거를 검사한다.
- 15건은 작은 고정 평가셋이다. PASS는 이 사례들의 성공이지 일반적인 정확도나 실서비스 안전 보장이 아니다.

보고서는 모델/정책/사례 해시, 실제 실행 여부, 사례별 예상·실제 verdict·policyIds,
모델/도구 호출 수, 토큰 사용량, 소요 시간, 안전한 오류 코드를 기록한다.
기본 trace에는 원문/판단 이유/키/내부 추론을 쓰지 않는다. 규칙·도구·모델·DB 대역 여부를 명시한다.
PASS=종료코드0, FAIL=1, BLOCKED=2. 실패를 없애기 위해 기대값을 바꾸거나 테스트를 제외하지 않는다.
모델/프롬프트 수정 후 재실행은 기존 보고서를 덮지 않는 새 파일에 보관한다.

## 현재 실행 기록

2026-10-20 `evidence/live-smoke-2026-10-20.json`:
다운로드 키 로딩 확인, Docker 컨테이너 pg_isready 정상. 그러나 Python의 loopback DB 접속은
Operation not permitted로 거부되어 `DATABASE_UNREACHABLE`, BLOCKED, 실제 색인/사례 실행 0건이다.
추가 OpenAI models.retrieve 사전 확인도 APIConnectionError(응답 status 없음)로 실패했다.
키 유효성/권한/잔액 부족으로 단정하지 않는다. 아직 실제 모델/검색 품질 PASS 증거가 없다.
임시 컨테이너는 정리했다. 네트워크가 허용된 팀 개발 환경에서 위 명령으로 재실행해야 한다.
#8/#9는 열어두고 #10 실제 Backend·승인·조치 통합은 별도 진행한다.


## 후속 실행 결과 (동일 날짜)

Docker 환경에서 실제 OpenAI·임베딩·pgvector 실행에 성공했다. 연결 3/3 PASS,
마지막 고정 평가 14/15 FAIL. [실패를 포함한 전체 이력](AI1-09-live-openai-results.md)과
`evidence/live-openai-2026-10-20/`을 최신 증거로 사용한다. 위 BLOCKED는 호스트 환경의 과거 기록이다.
보고서는 promptSha256도 고정하므로 프롬프트 변경 후에는 smoke부터 새로 실행해야 한다.
합성 평가 보고서에만 마스킹한 짧은 판정 이유를 기록하며, 일반 trace의 원문/이유 미기록 규칙은 유지한다.
