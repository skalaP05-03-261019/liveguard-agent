# BE #40 대응: Agent 소비자 경계 (2026-10-21)

사용자 김태관은 Agent 담당이며 BE 담당자에 대한 개입을 최소화한다. 이번 변경은 Agent 저장소만 수정한다. BE #40은 인희(iihlolo) 담당으로 유지하고, 해당 브랜치·DB·Flyway·정책 필터·사건 저장/조회 로직을 별도로 변경하거나 되돌리지 않는다. 기존 통합 실행 증거를 BE 구조 정리 완료로 해석하지 않는다.

## 책임

| 담당 | 기준 |
|---|---|
| BE1 | 메시지 필터·handlingRoute·ruleId·초기 visibility·작성자 사유 코드 |
| BE2 #40 | 사건/증거 snapshot/확정 finding/이력/강퇴 근거의 공통 저장·조회 정본과 원자성 |
| Agent 김태관 | 주어진 사건·문맥·정책으로 조사, 부족한 사실 표시, interrupt/checkpoint/동일 thread 재개, 저장된 결정 조회와 서버 발급 명령 소비 |

Agent가 같은 업무의 필터/사건 DB를 재구현하지 않는다. 신규 endpoint·테이블을 BE에 요구하지 않고 현재 contracts/openapi.yaml 및 기존 MCP Tool을 사용한다. 내부 저장 구조(기존 CaseQueryService 또는 통합 서비스)에 따라 분기하지 않는다. snapshot·버전·원문·승인을 스스로 보충하지 않는다.

## 수정한 소비자 문제

기존 #53의 확정 FLOOD 단위 시험은 HELD만 사용해 실제 HIDDEN 응답을 놓쳤다. BE confirmationStatus=CONFIRMED_VIOLATION인 FLOOD는 HELD(고정 조사 증거) 또는 HIDDEN(확정된 증거)을 소비하고 모델로 다시 확정하지 않는다. PUBLIC은 거부한다. 미확정 FLOOD의 HIDDEN은 계속 거부하며, HELD는 기존 문맥 검토를 유지한다. 3건 집계·강퇴 자격·예약·소진은 BE 소유이며 KICK은 별도 운영자 승인과 서버 재검증을 유지한다. 새로운 위반/제재 규칙은 없다.

CaseSnapshot은 BE가 제공한 ruleId/filterVersion/matchingTags를 보존하며 빠진 값을 생성하지 않는다. 이 필드의 영속화는 BE #40 담당이다. frozen 문맥과 현재 target metadata를 다르게 제공할 새 계약은 필드 의미를 BE와 확정한 후 어댑터에 반영한다. 불일치를 임의 무시하지 않는다.

## 조회 전용 인수 검사

실제 실행할 Agent와 같은 환경에서 다음을 실행한다. 임의 테스트 사건을 DB에 삽입하거나 과거 사건의 metadata를 SQL로 보정할 필요가 없다. BE가 정상 메시지 전송 흐름으로 만든 caseId를 사용한다.

```bash
python -m app.check_backend_connection \
  --case-id <실제-caseId> \
  --output evidence/be40-read-contract.json \
  --require-workflow-ready
```

명시적 환경변수: BUSINESS_BACKEND_BASE_URL, BUSINESS_MCP_SERVICE_TOKEN, BUSINESS_MCP_URL, BUSINESS_MCP_AGENT_TOKEN. 값은 .env에서 주입하며 토큰은 채팅/출력에 쓰지 않는다. --publish-test-event 옵션은 사용하지 않는다.

검사는 GET 및 조회 MCP Tool만 호출한다. 채팅 발송·새 사건·판정 제출·결정·resume·apply·kick·DB 쓰기를 하지 않는다. 원문·자격증명을 결과에 저장하지 않는다.

- readContractReady: 인증·schema·task/snapshot/target binding 및 직접 HTTP와 MCP의 사건/문맥 결과 일치.
- workflowPreflightReady: 위 조건과 현재 처리 경로/확정 상태/visibility의 Agent 사전조건 통과.
- executionAuthorized: 항상 false. 실행/제재 허가가 아니다.
- blockers: history/securityFacts/kickEligibility 등 미구현 조회도 별도로 기록한다. 위 두 ready가 전체 Gateway·모델·승인 E2E 완료를 뜻하지 않는다.
- BACKEND_READ_DIVERGED: 같은 사건이라도 HTTP/MCP의 policyVersion/snapshot/route/targets/문맥·가명이 다르면 차단한다. Agent는 어느 응답이 맞는지 선택해 합치지 않는다.

팀 변경 후에는 이 검사 → Agent ASGI/TCP 테스트 → 기존 실제 승인 E2E 순서로 검증한다. #40 구현을 Agent 쪽에서 병행 재작성하지 않는다. 과거 DB migration 이관은 BE 담당 검토 대상으로 남긴다.

## 이번 실제 확인

실행 중인 BE #39의 기존 확정 FLOOD 사건을 GET/MCP로 재조회한 결과는 evidence/be40-read-contract.json에 보관한다. 그 결과는 현재 통합 경로의 소비자 호환성 관찰이며, 아직 완료되지 않은 BE #40의 두 저장 경로 통합을 증명하지 않는다. 통제 테스트는 응답 불일치, 미확정 HIDDEN, 확정 PUBLIC 차단 및 별도 사람 승인 유지에 사용한다.
