# AI1 #10 · 최신 명세 반영 및 문맥–강퇴 Graph 연결

**2026-10-21 OpenAPI v1 후속:** BE PR #28(`882974d`)의 A-9/A-16에 따라 PII NEEDS_REVIEW는 기존 CONTEXT 검토로 interrupt하도록 갱신했다. 아래 문서의 해당 과거 설명보다 [#46 구현 범위와 잔여 계약](AI1-46-openapi-v1-pii-context.md)이 우선한다. PII 정상·위반은 필수 승인으로 확대하지 않는다. 나머지 TBD 제안의 확정 여부는 별도 유지한다.

확인일: 2026-10-20. 정책 의미는 최신 팀 Notion, API의 구현 여부는 GitHub 소스를 구분한다.
이 문서는 Backend 새 API의 팀 합의를 대신하지 않는다. Graph 연결과 실제 업무 DB 집행은 별도 완료 항목이다.

## 직접 확인한 자료

Notion 앱의 문서 페이지에서 `_v0.1` 아래 과거 문서와 별도로 등록된 새 페이지를 열람했다.

- [PROJECT](https://app.notion.com/p/64861f4176b582258d2901b744ea5fd1): 사용자 추가 합의 반영본. TBD-01~12와 AC-01~21.
- [POLICY](https://app.notion.com/p/e3661f4176b582ab920d01ba690418b7): 2026-10-20 개정. 문맥 필수 사람 결정, PII 별도 AI 경로, liveId 범위.
- [CONTRACTS](https://app.notion.com/p/f8e61f4176b5829c9e1c8157b8124676): 정책 의미 확정, 신규 API/필드 이름은 초안이라고 명시.
- [ARCHITECTURE](https://app.notion.com/p/e1a61f4176b5830f966801498c154344): CONTEXT→필요 시 별도 KICK, 같은 thread 재개.
- [Slack 14:35 정책 업데이트](https://theskala.slack.com/archives/C0C6UJ4AJRK/p1792474508586939): 링크 자체 금지, 인용 욕설 포함, policyVersion 3.1 요청.
- [Slack 16:01 역할 제안](https://theskala.slack.com/archives/C0C6UJ4AJRK/p1792479710345749) 및 [16:05 태관 확인](https://theskala.slack.com/archives/C0C6UJ4AJRK/p1792479920438699): AI2 CONTEXT 노드/저장·재개, AI1 #10 Graph 연결·Backend 처리.
- Slack의 프로젝트 대화방 최근 관련 메시지를 읽었다. 전체 워크스페이스/모든 개인 DM을 읽었다고 주장하지 않는다. 16:15 PPT 제출 안내도 있으나 첨부 이미지의 상세 요건은 이번 구현 범위에서 검증하지 않았다.
- [AI2 PR25](https://github.com/skalaP05-03-261019/liveguard-agent/pull/25) head `9fbac03854c2c0aa607e2639d8177948c2b471ac`: CONTEXT 노드와 공통 resume. 로컬 통합에서 그대로 재사용하며 원 PR 자동 머지하지 않는다.
- [BE B1 PR9](https://github.com/skalaP05-03-261019/liveguard-be/pull/9) head `f1adab35d2bc0fd573c27eba3e62ba884011d5e1`: 실제 `/internal/cases/{caseId}/context`, `/channel-info`, `/events`와 서비스 토큰 구현.

## 반영한 정책 의미

| 경로 | 연결 동작 |
|---|---|
| PASS | 모델·변경 호출 없음 |
| RULE_CONFIRMED | BE의 확정 표시 필수, 모델 생략. BE가 심사를 요청하면 KICK interrupt는 수행 |
| CONTEXT_REVIEW | HELD 확인 → 공유 Harness로 조사·근거 검증 → CONTEXT interrupt. 정상/위반/보류 AI 제안 모두 동일하게 대기 |
| CONTEXT 정상/위반 결정 | decisionId로 BE 저장본 조회. case/live/snapshot/evidence/policy/thread/review/decision 참조를 확인하고 BE 처리 요청 |
| CONTEXT 보류 | 동일 reviewId로 재대기. 재조사·새 심사 생성·자동 공개 없음 |
| 후속 KICK | BE가 심사 필요/기준 사건을 결정. CONTEXT 승인과 별도로 다시 interrupt. 같은 thread에서 재개 |
| PII_INVESTIGATION | CONTEXT 필수 승인을 임의 확대하지 않고 별도 판정 제출. 미확인은 NEEDS_REVIEW |
| FLOOD_REVIEW | TBD-11 최초 확정 주체 미정. 신규 확정·제재 없이 NEEDS_REVIEW |

모델의 message_findings는 운영자 결정으로 덮어쓰지 않는다. Backend가 제재 기간/집계/예약/소진을 소유한다.
이력 조회의 30일 고정값은 제거했다(TBD-03). 강퇴 24시간 집계와 혼용하지 않는다.
본문·판정 이유는 운영자 카드에서도 마스킹하며 messageId 등 근거 식별자는 보존한다.

## 실행 진입점과 Backend 경계

`app.integration.workflow.build_moderation_graph(model=…, business=…, policy=…, backend=…, checkpointer=…)`.
기존 `app.graph.build_graph`를 몰래 교체하거나 `/run`의 501을 가짜 202로 바꾸지 않는다.
작업 영속화·서비스 인증·task 중복 처리 뒤 실제 dispatcher가 이 Graph를 주입해야 한다.
Postgres는 `app.harness.checkpoint.open_checkpointer`, 메모리 saver는 단위 시험 전용이다.

Backend snapshot은 기존 필수 증거 필드와 더불어 `liveId`, `userId`, `handlingRoute`가 필요하다.
channelId를 liveId로 복사하지 않는다. 누락은 `BACKEND_CONTRACT_INCOMPLETE`로 모델 실행 전 중단한다.
RULE_CONFIRMED는 `confirmationStatus=CONFIRMED`도 필요하다. 필드 이름은 BE와 동결할 연결 계약안이다.

`ModerationGateway` 구현은 BE2 담당 API의 실제 경로/JSON 확정 후 주입한다. 현재 메서드 경계:

- `create_review(card, trace_id)` → reviewId. card에 liveId, case/task/thread 및 증거 버전 포함. BE에서 멱등 생성.
- `get_decision(review_id, decision_id, trace_id)` → 저장된 CONTEXT 결정 및 해당 case/live/snapshot/evidence/policy/thread/review/decision 참조.
- `apply_context(request, trace_id)` → 저장된 사람 결정의 실제 처리. AI 제안은 승인으로 쓰지 않음.
- `submit_analysis(analysis, trace_id)` → PII 경로의 메시지별 제안과 liveId/handlingRoute/taskId 전달.
- `revalidate(review_id, decision_id, trace_id)` / `kick(command, trace_id)` → AI2 KickGateway. 재검증 응답의 live/review 참조를 확인하고 기존 MCP KickCommand를 검증한다. 명령에 미지원 liveId/reviewId 필드를 주입하지 않으며 case/target/decision/evidence/policy 바인딩을 검사한다. MCP 재시도를 외부에서 중복 적용하지 않음.

처리 응답의 제안 필드: 조사 참조(caseId/liveId/snapshotId/evidenceVersion/policyVersion),
`status=APPLIED|NO_ACTION|NEEDS_REVIEW`, `observation=CONFIRMED|UNKNOWN`, `kickReview.required`.
이 필드명은 신규 소비자 계약안이며 BE 구현 사실이 아니다. UNKNOWN은 성공으로 표시하지 않고 후속 강퇴를 중단한다.
조치 상태 조회·부분 성공·outbox는 아직 실제 BE 연결에서 검증해야 한다.

**실제 B1 계약에 맞춰 구현한 HTTP 어댑터는 `BackendEvents`다.**
`POST /internal/cases/{caseId}/events`, X-Service-Token 서비스 토큰, X-Trace-Id,
TOOL_START/TOOL_END/TASK_FAILED와 B1 payload 필드만 전송한다. 원문 도구 인자는 버린다.
응답 유실은 EVENT_DELIVERY_UNKNOWN이며 자동 재전송하지 않는다. 이 어댑터는 업무 변경을 수행하지 않는다.

## 남은 차이 / 완료 주장 금지

1. B1 ContextData는 messages/truncated만 제공하며 snapshot/evidenceVersion을 증명하지 않는다. 이 응답에 버전을 임의로 채우지 않는다.
2. BE main JudgmentRequest는 단일 category 중심이다. 새 메시지별 categories[] 및 CONTEXT 저장 계약과 통일 필요.
3. 최신 BE 작업 브랜치들, tools 브랜치, main OpenAPI는 서로 다르다. 합의 없이 한쪽을 최종 구현이라고 부르지 않는다.
4. 문맥 검토 생성/결정 조회/결정 반영/강퇴 재검증의 실제 BE2 HTTP 구현은 아직 이 PR에서 확인·호출되지 않았다.
5. Business MCP Compose DNS Host 421은 PR23의 미해결 사항이며 이 Graph 단위 시험으로 해결됐다고 주장하지 않는다.
6. 이번 검증은 합성 Backend/model/RAG와 실제 Graph·체크포인터. 실제 OpenAI 재평가·Spring 집행 성공과 구분한다.
7. 수업의 모든 변경 도구 사전 승인 기준과 BE 규칙 자동 집행 차이는 PROJECT TBD-12로 남는다.


## 실제 Backend 연결 확인 후속

Backend main `0387bd3`를 소스 해시 검증 후 Docker로 빌드·기동하고 임시 pgvector DB에 합성 사건을 넣어 시험했다.
Agent/MCP의 Backend 인증 헤더는 실제 B1 ServiceTokenFilter에 맞춰 **X-Service-Token**으로 수정했다.
MCP inbound Agent 인증은 기존 Bearer를 유지한다. 앞선 Bearer 기반 Backend 계약 설명은 이번 실행으로 정정한다.
문맥 조회·channel-info는 200, 진행 이벤트는 202이며 Agent→Business MCP→Spring의 실제 TCP도 통과했다.
합성 이메일의 [EMAIL] 마스킹도 실제 Spring 응답에서 확인했다. 이벤트 ACK는 SSE 화면 수신 검증과 구분한다.
사건 조회는 404, 실제 Swagger에 B2 사건/판정/강퇴/문맥 결정 경로는 없었다.
B1 문맥에는 caseId/snapshotId/evidenceVersion/visibility가 없어 현재 Agent의 증거 검증을 통과하지 못한다.
MCP get_moderation_case의 기존 view 호출도 B1에서 ContextData를 받아 사건 스키마와 맞지 않는다.
누락 필드를 생성하거나 검사 제거로 우회하지 않았다. 전체 업무 집행은 여전히 미연결이다.
재현 명령은 `app.check_backend_connection` CLI와 `evidence/backend-connection-2026-10-20.json`을 참조한다.

```bash
# 실행 환경에 BUSINESS_BACKEND_BASE_URL, BUSINESS_MCP_URL,
# BUSINESS_MCP_AGENT_TOKEN, BUSINESS_MCP_SERVICE_TOKEN 설정.
# 마지막 값은 Backend INTERNAL_SERVICE_TOKEN과 일치해야 한다. 키 값 출력 금지.
uv run --locked python -m app.check_backend_connection \
  --case-id <조회할-사건-UUID> --output evidence/backend-connection-local.json
```

기본 실행은 조회만 한다. `--publish-test-event`는 명시적으로 선택한 검증용 사건에만 사용한다.
진단 status PARTIAL은 통신 일부 성공/계약 미완성이며 전체 Agent 준비 완료를 의미하지 않는다.
