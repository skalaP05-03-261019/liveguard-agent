# 실제 Compose 통합 수정 — 2026-10-21

Issues: #30, #31. Runtime PR #27 위의 변경이며 승인 정책과 Graph는 변경하지 않습니다.

- Docker 네트워크의 `business-mcp:8002`로 실제 MCP 호출 시 421 `Invalid Host header`를 재현했습니다. `BUSINESS_MCP_ALLOWED_HOSTS`로 신뢰하는 Host를 명시하고 DNS 재바인딩 보호를 유지합니다. 기본값은 localhost/127.0.0.1이며 Compose에서만 `business-mcp:*`를 추가합니다. 미허용 Host 421, 잘못된 토큰 401, 허용 Host initialize 200을 회귀 검증합니다.
- BE PR #12 이후 context의 view는 chatContext만 허용합니다. 내부 selector는 유지하되 HTTP 어댑터를 전용 경로에 정렬했습니다: moderationCase → `/internal/cases/{id}`, streamInfo/officialLinks → `/channel-info`, securityFacts → `/security-facts`, chatContext → `/context?before=&after=`, 사용자 이력 → `/user-history`.
- 501 `NOT_IMPLEMENTED`와 404 `NOT_FOUND`를 실제 MCP를 통해 확인했습니다. 오류를 정상 데이터로 변환하거나 재시도 가능한 성공으로 표시하지 않습니다.

검증: 실제 pgvector/Postgres를 둔 전체 테스트 **ASGI 188개(5.637초), TCP 188개(15.904초), skip 0, 모두 통과**. 모델은 통제된 fixture이며 실제 OpenAI 호출/제재는 수행하지 않았습니다. 추가 회귀 테스트는 두 개입니다(Host allowlist, 전용 경로/501 보존). 별도 Compose의 Agent SDK → Business MCP → 실제 Spring Backend도 검증했습니다.

남은 연동: BE 사건 조회·사용자 이력·검증 사실·실행 API는 여전히 501입니다. KICK 명령의 최신 basis/liveId 계약과 기존 Agent 스키마 차이, CONTEXT HTTP Gateway·BE dispatch/resume 미구현은 이 변경에서 임의로 해결하지 않았습니다. [BE 통합 이슈 #17](https://github.com/skalaP05-03-261019/liveguard-be/issues/17)과 인프라 `docs/full-stack.md`에 연결 경계를 기록했습니다. 모델 판정/사람 결정 구분과 필수 승인 중단을 보존합니다.
