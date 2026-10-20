# 실제 OpenAI 검증 결과 · 2026-10-20

## 결론

**OpenAI·임베딩·정책 DB·MCP 연결은 확인했으나, 모델 판단 품질 평가는 FAIL이다.**
연결 smoke 3/3 PASS, 마지막 full 12/15. Full은 실제 모델 조사 13건과 검증용 규칙 분기 2건으로 구성된다.
모든 결과가 실제 Backend 전체 동작을 뜻하는 것은 아니다. Spring 업무 의존성은 통제된 합성 fixture다.
조치 실행·채팅 상태 변경은 0건이다.

호스트 Python의 접속 제한 때문에 실행이 막혔던 이전 기록은 그대로 보존했다.
이번에는 Docker 안에서 uv.lock으로 설치한 Python 3.11 환경을 사용해 실제 접속에 성공했다.
다운로드의 .env를 읽기 전용으로 mount했고 키는 저장소·이미지·명령 인자·보고서에 복사/출력하지 않았다.
임시 검증 컨테이너와 DB는 실행 후 정리했다.

- 모델 `gpt-5.4-mini`, 실제 OpenAI tool calling.
- 임베딩 `text-embedding-3-small`, 실제 1536차원 벡터 생성.
- pgvector: 전용 `liveguard_eval` DB, v3.1 **29개 조항** 색인.
- MCP: 생산 Business/RAG 앱과 SDK, Bearer 인증, ASGI HTTP 전송. Docker TCP 배포 시험과 구분한다.
- 색인/검색/조항 조회/Resource 동일성 검사와 TARGET 판정 검증을 함께 실행했다.

## 고정 사례 평가 이력

입력·기대값·사례 SHA-256을 바꾸지 않고 프롬프트/도구를 수정한 뒤 재실행했다.
실패 보고서를 삭제하지 않았고, 마지막 결과를 현재 결과로 사용한다.

| 실행 | 연결 3건 | 전체 15건 | 변경 |
| --- | --- | --- | --- |
| 최초 | 3/3 | 10/15 | main 구현 그대로 실제 API 실행 |
| R2 | 3/3 | 10/15 | 사실 조회·도배·위험 범위 확인 지침, 생산 마스킹 사용 |
| R3 | 3/3 | 13/15 | get_policy_section을 모델 조회 도구로 연결, 정확한 예외 원문 확인 |
| R4 (마지막) | 3/3 | **12/15** | 보류 missingFacts 출력과 대상 불명확 처리 지침 보강 |

같은 고정 평가셋을 개선에도 사용했으므로 미관측 데이터에 대한 정확도나 서비스 준비도를 주장하지 않는다.
R3보다 R4 결과가 낮아진 점은 모델 변동성과 프롬프트만으로 지침이 강제되지 않는 한계를 보여 준다.

## 마지막 실패 3건과 다음 작업

1. **관리자 사칭**: 기대 VIOLATION, 실제 NEEDS_REVIEW.
   가상 서버에 역할 검증 사실이 있지만 모델이 충분히 확인하지 않고 보류한다.
   다음: 역할·방송 사실 조회 경로와 근거 수용 조건을 실제 Backend 계약에 맞춰 연결한다.
2. **대상 불명확 부정 발언**: 기대 NEEDS_REVIEW, 실제 NO_VIOLATION.
   모델이 문맥에 없는 방송/게임 평가 대상을 추정했다.
   다음: CP-2.2/CP-3.1 경계의 기대 판정을 팀과 확정하고, 조회할 맥락과 부족한 사실을 구조적으로 검사한다.
   이번에는 PASS를 만들기 위해 기대값을 수정하지 않았다.
3. **상충하는 개인정보 사실**: 기대 NEEDS_REVIEW, 실제 PII VIOLATION.
   작성자의 주장만으로 확정하는 응답이 다시 나왔다.
   다음: #10의 실제 get_security_facts 응답 계약을 확인하고, 필수 조회·UNKNOWN/충돌 처리 검사를 runtime에서 강제한다.
   프롬프트 요구만으로 일관성을 보장하지 않는다.

실제 사용자에게 적용하지 않았으며, 위반 제안은 Backend 확정/운영자 결정을 대체하지 않는다.
#9는 판단 품질과 Harness 통합이 남아 계속 열린 상태로 관리한다.

## 코드 개선

- 모델 조회 목록에 제공자의 기존 get_policy_section을 추가했다. 모델은 policyId만 선택하고
  policyVersion·Resource URI는 사건/서버가 결정한다. 조항/Resource가 다르면 상태에 저장하지 않는다.
- 검증 실행기에 safe_presentation 연결. 식별자 보존과 개인정보 마스킹을 함께 사용한다.
- 보고서에 promptSha256를 추가하고, full 실행은 같은 프롬프트의 PASS smoke 보고서만 받는다.
- 합성 평가 보고서에만 마스킹한 짧은 판정 이유와 실패 출력 형식 진단을 기록한다.
  내부 추론·원문·키는 일반 trace에 기록하지 않는다.

## 증거

`evidence/live-openai-2026-10-20/summary.json`과 각 smoke/full JSON, 프롬프트 사본을 참조한다.
기대값과 실제 verdict/category/policyIds, 호출 수, 토큰, 시간, 오류를 구분해 기록했다.
이전 `evidence/live-smoke-2026-10-20.json`은 호스트 접속 제한 당시의 BLOCKED 기록이며 최신 결과가 아니다.

실행 환경 이미지는 `ghcr.io/astral-sh/uv@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36`.
네트워크가 허용된 환경에서는 기존 `docs/AI1-09-live-verification.md` 명령을 그대로 사용한다.
Docker에서는 저장소를 /workspace:ro, 키 파일을 /run/secrets/openai.env:ro,
보고서 디렉터리를 /results로 연결하고 UV_PROJECT_ENVIRONMENT를 컨테이너 /tmp로 둔다.
검증 DB 컨테이너와 네트워크를 공유해 DB 주소는 127.0.0.1:5432/liveguard_eval로 사용했다.
