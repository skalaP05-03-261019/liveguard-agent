# OpenAPI v1 연결: PII 보류의 사람 결정

2026-10-21 · 김태관 · Agent #46

## 기준

- BE [PR #28](https://github.com/skalaP05-03-261019/liveguard-be/pull/28), main `882974d`의 [OpenAPI](https://github.com/skalaP05-03-261019/liveguard-be/blob/882974d/contracts/openapi.yaml).
- 같은 커밋의 [TBD-decisions.md](https://github.com/skalaP05-03-261019/liveguard-be/blob/882974d/docs/policy/TBD-decisions.md), A-9·A-16.
- 사용자가 전달한 B1의 14:16 안내: 계약은 v1.0, Java는 아직 v0.5이며 후속 enum PR에서 변경한다.

이 문서는 이전 AI1-10의 **PII NEEDS_REVIEW 종료 처리**를 갱신한다. A-1~16의 API 결정과, 이견 기한이 10/22 10:00인 나머지 TBD 제안의 확정 여부를 혼동하지 않는다.

## 구현

`workflow.submit_pii`는 PII 판정을 한 번 제출한다. 판정 중 하나라도 NEEDS_REVIEW면 응답이 `confirmationStatus=PENDING`, `reviewKind=CONTEXT`, 유효한 `reviewId`인지 확인하고 기존 `review_context`로 이동한다. 새 검토 생성 호출 없이 같은 thread에서 interrupt한다. 원래 사건 경로 PII_INVESTIGATION은 유지한다.

재개 입력은 저장 결정 조회를 위한 ID이며 승인 자체가 아니다. 기존 Backend 저장본의 case/live/snapshot/evidence/policy/thread 검증을 거친다. DEFER면 같은 reviewId로 다시 기다리고, 정상·위반 결정 후에만 기존 apply 경계로 전달한다. 모델 조사·판정 제출은 반복하지 않는다. KICK은 별도 승인을 유지한다.

PII 정상·위반 판정에는 CONTEXT 필수 승인을 추가하지 않는다. 잘못된 검토 ID·종류·확정 상태는 실패 처리하며 자동 공개하지 않는다. 새 ConfirmationStatus 값도 읽되 RULE_CONFIRMED에서 CONFIRMED_NORMAL/PENDING/CANCELLED를 위반 확정으로 인정하지 않는다. 기존 CONFIRMED 입력 호환은 유지한다.

`JudgmentHttpClient`는 `POST /internal/cases/{caseId}/judgment` 전송만 구현한다. X-Service-Token과 X-Trace-Id를 사용한다. 추가 필드가 금지된 요청에는 taskId/threadId/evidenceMessageIds를 넣지 않는다. 근거 ID는 내부 상태·checkpoint에 보존하고, 전송 reason에는 마스킹한 설명과 근거 ID를 담는다. FLOOD는 별도 경로를 만들지 않고 CONTEXT_REVIEW의 categories로 제출할 수 있다. PASS 전수 조사나 유사 도배 후보 생성은 추가하지 않았다.

응답은 판단 제출 접수 결과다. 이를 조치 적용 성공으로 바꾸지 않는다. 전송 결과가 유실되면 JUDGMENT_DELIVERY_UNKNOWN이며 자동 재제출하지 않는다. 이 클라이언트는 완전한 ModerationGateway가 아니며 AGENT_GATEWAY_FACTORY/readiness를 활성화하지 않는다.

## 버전 전환과 잔여 경계

OpenAPI의 PolicyVersion은 정확히 `v3.1`이다. 현재 RAG 기본 설정·색인은 `3.1`이다. 전송 시 접두사를 임의 추가하거나 기존 문서·색인 행을 덮어쓰지 않는다. 새 HTTP 클라이언트는 3.1 요청을 차단한다. BE·Agent·RAG가 원문/Resource/색인까지 같은 버전으로 전환한 뒤 배포해야 한다.

실제 통합 잔여 사항은 BE [#29 댓글](https://github.com/skalaP05-03-261019/liveguard-be/issues/29#issuecomment-6308718343)에 기록했다.

1. B1/B2 enum·DTO와 실제 방송 시작/종료, versioned case/context 구현 반영. 현 OpenAPI의 InternalCase에는 userId가 없고 ContextData에는 Agent가 요구하는 사건·snapshot·증거 버전·노출 필드가 없다.
2. 내부 저장 결정 조회·CONTEXT apply 계약 및 KICK validationRef → 실행 명령 연결. 미합의 경로를 새 공식 계약으로 간주하지 않는다.
3. 위 계약으로 완전한 HTTP ModerationGateway 연결 및 동일 정책 버전 색인 검증.
4. FE liveId·202 Decision·CONTEXT/KICK 카드·SSE 연결, Infra 공통 compose에서 실제 사람 결정 1건 끝까지 검증.

팀이 작업 중인 BE enum/DTO 파일은 수정하지 않았다. 기존 로컬 채팅 DB를 초기화하지 않았다. 테스트에는 별도의 임시 PostgreSQL을 사용했다.

## 검증 범위

Graph 및 HTTP 오류 주입 테스트, PostgreSQL checkpoint 재연결 후 동일 reviewId 복원·DEFER 후 재개를 검증한다. 모델/BE 판단은 통제된 fixture이며 실제 Spring 조치·브라우저 E2E 완료의 증거가 아니다. 실행 결과는 `evidence/openapi-v1-agent-verification.json`에 기록한다.
