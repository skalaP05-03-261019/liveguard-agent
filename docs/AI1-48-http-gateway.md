# #48 실제 HTTP Gateway와 통합 검증 (2026-10-21)

Issue: https://github.com/skalaP05-03-261019/liveguard-agent/issues/48

BE OpenAPI v1과 후속 계약 DTO PR #32를 기준으로 판정 제출, 저장된 사람 결정 조회, 같은 thread의 CONTEXT 적용, KICK 재검증 및 승인 뒤 Business MCP 실행을 연결한다. 모델에는 변경 도구를 제공하지 않는다.

## 설정

`AGENT_GATEWAY_FACTORY=app.integration.http_gateway:configured_gateway`, `BACKEND_BASE_URL`, `BACKEND_SERVICE_TOKEN`과 기존 모델·MCP·Postgres 설정을 명시한다. 기본 factory는 비어 있으며 자동으로 활성화하지 않는다. 내부 결정 조회/적용 경로는 BE #29 통합 제안이며 기본 v1 계약 전체의 구현 완료를 의미하지 않는다.

색인은 `LIVEGUARD_POLICY_VERSION`을 우선 사용하며 `POLICY_VERSION`도 함께 지정했다면 정확히 일치해야 한다. `v3.1`은 저장·조회 버전으로 그대로 유지한다. 기존 원문 파일 `policy-v3.1.md`를 찾을 때만 접두어를 처리한다. 빈 레거시 버전의 기본값은 여전히 3.1이며 배포 정책 승인을 뜻하지 않는다.

## 실패와 승인

변경 POST 응답 유실은 결과 미확인으로 처리하고 자동 재시도하지 않는다. 저장된 결정과 case/live/snapshot/evidence/policy/review/task/thread 참조를 다시 확인한다. 조사 실행 한도에 도달하거나 모델 판정의 근거 검증이 실패하면 사실을 지어내거나 한도를 초기화하지 않고 부족 사실을 담은 NEEDS_REVIEW를 운영자에게 전달한다. 사람 결정 전 HELD는 공개하지 않는다.

## 검증

- PostgreSQL을 사용하는 ASGI 전체 251개 통과, skip 0.
- 실제 TCP MCP를 사용하는 전체 251개 통과, skip 0.
- 별도 6서비스 환경에서 실제 OpenAI 모델과 정책 29조항 색인, Business/RAG MCP 호출을 사용했다.
- 실제 브라우저: 일반 채팅 전파, HELD 비작성자 격리, AI 검토 카드, DEFER 동일 검토 유지, 사람 정상 확정 뒤 PUBLIC 복구, 사람 위반 확정 뒤 HIDDEN 및 TIMED_OUT 403(until 포함).
- 전체 KICK 화면 E2E와 finding 취소·복구는 이 실행 증거에 포함하지 않는다. BE 통합 코드는 별도 PR에서 미정 정책 시험 설정을 켠 경우에만 활성화한다.

브라우저 증거는 Infra `evidence/v1-browser/report.json`과 캡처를 참조한다. 통과 수는 시험 범위이며 모델 정확도나 전체 기능 완성률이 아니다.
