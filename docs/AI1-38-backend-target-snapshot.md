# BE targets[] snapshot 어댑터 (#38)

2026-10-21. BE main `3a33ee8`의 `InternalCase` Java DTO와 OpenAPI는 `targets[{messageId, visibility, version}]`를 사용하지만 Agent 로더는 `targetMessageIds`와 `targetVersions`를 요구했습니다. HTTP 조회 경로를 맞춘 뒤에도 응답 형식 차이로 실패할 수 있는 경계입니다.

이 변경은 BE가 명시한 대상 ID·버전·가시성만 읽어 기존 Agent snapshot 형식으로 정규화합니다. 두 형식을 동시에 받으면 ID 집합과 버전이 정확히 일치해야 합니다. 중복 대상, 빈 배열, 누락·잘못된 가시성, 0/문자열/bool 버전, 상충하는 기존 형식은 거부합니다. 입력 객체를 변경하지 않습니다.

실제 context에 같은 snapshot/evidenceVersion의 대상이 있는지 확인하고, targets의 명시적 가시성과 다르면 STALE_EVIDENCE로 중단합니다. 기존 사건·task·snapshot·대상 바인딩 검증을 재사용하며, 기존 응답 형식도 계속 읽습니다. 새 nested DTO는 State에 직접 저장하지 않고 JSON snapshot에만 저장합니다.

**보완하지 않는 값:** liveId/userId/handlingRoute/policyVersion과 context의 caseId/snapshotId/evidenceVersion/visibility는 누락 시 추정하지 않습니다. `channelId`는 `liveId`를 대체하지 않습니다. 새 어댑터가 현재 BE의 501이나 context metadata 누락을 없애지는 않습니다. 필수 CONTEXT/KICK 승인·집행 경로는 변경하지 않습니다.

## 추가 확인한 BE 변경

- BE PR #18: JDBC 사건/후보/근거 영속화. 실제 런타임 적용과 200/501 경계는 #35의 증거를 참조합니다.
- BE PR #19, main `3a33ee8`: `docs/policy/` 원본 우선. 이전 정책 충돌을 정리했으며 런타임 변경은 없습니다.
- BE PR [#23](https://github.com/skalaP05-03-261019/liveguard-be/pull/23), head `df53ccda`: **열린 문서 PR, 미병합**. lives·hold reasons·confirmationStatus·CONTEXT/KICK 테이블과 LIVE 범위·도배 묶음을 제안합니다. OpenAPI/enum/마이그레이션 구현은 후속이라고 명시합니다. 따라서 새 경로/enum/상태를 실제 서버가 지원한다고 가정하지 않습니다.
- TBD-08 `v3.0` 활성 제안과 기존 RAG `3.0` 원문의 의미 차이는 [Agent 이슈 #37](https://github.com/skalaP05-03-261019/liveguard-agent/issues/37)에서 추적합니다. 코드 기본값을 과거 정책으로 되돌리지 않았습니다.

## 검증과 구현 한계

추가 테스트는 실제 MCP ASGI/TCP framing을 통과하는 controlled Backend fixture로 targets[]를 읽고, 기존 형식과 일치/충돌·중복·누락·가시성 변경·원래 snapshot 바인딩을 검증합니다. 연동 진단의 실제 loader도 새 형식을 받아 확인합니다. 이를 실제 BE 사건 API 200 또는 전체 AI 승인 E2E 증거로 표시하지 않습니다. 현재 해당 API는 여전히 501입니다.

이 PR은 운영자 근거/독립 진단 PR #36 위에 쌓았습니다. #36 병합 후 base를 main으로 조정해야 합니다. 전체 회귀 결과는 `evidence/backend-target-adapter-regression-2026-10-21.json`에 기록합니다.

최종 회귀: 실제 격리 Postgres/pgvector를 둔 ASGI 206개(5.461초), TCP 206개(17.484초), skip 0, 모두 통과. 실제 영속 사건의 읽기 전용 HTTP/MCP 재조회는 `evidence/backend-readiness-recheck-2026-10-21.json`에 별도로 기록했습니다. context/channel-info 200과 case/user-history/security-facts/kick-eligibility 501을 다시 확인했으며, 재조회 중 메시지/판정/제재를 생성하지 않았습니다.
