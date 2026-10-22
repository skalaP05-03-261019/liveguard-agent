# BE #45 UNKNOWN 역할 대응 (Agent #56)

계약 출처: https://github.com/skalaP05-03-261019/liveguard-be/pull/45

- authorRole=UNKNOWN은 현재 멤버십을 확인할 수 없다는 뜻이며 VIEWER/공식 운영자로 추정하지 않는다.
- isOfficialOperator=false는 공식 운영자로 확인되지 않음이며 시청자/사칭자 확정이 아니다.
- 현재 역할은 사건 당시 역할을 입증하지 않는다.
- 역할 정보와 security-facts는 출처·대상·시점을 확인하고 서로 덮어쓰지 않는다. 판단에 필요한 사실이 없거나 충돌하면 NEEDS_REVIEW와 missingFacts를 사용한다. 역할이 필요 없는 다른 위반까지 자동 보류하지 않는다.

변경: 조사 system prompt와 get_stream_info 도구 설명. enum 변환, BE 업무 코드, 판정 유형, 사람 승인·강퇴 흐름은 변경하지 않았다. prompts/** 변경에도 ASGI/TCP CI가 실행되도록 경로 조건을 보강했다.

검증: 신규 2개 회귀 검사 + 기존 Business MCP/조사 검사 총40개 통과, skip0. 실제 Backend HTTP 대역 → 실제 MCP 프로토콜 → Agent 클라이언트에서 다섯 역할을 보존한다. 조사 입력에서는 UNKNOWN/false와 security facts의 세 값을 별도로 보존한다. 원자료는 evidence/unknown-role-2026-10-22.json.

한계: Backend 응답은 통제된 HTTP 대역이고 모델은 scripted model이다. BE #45 실제 서버 호출이나 실제 LLM의 사칭 판정 품질을 증명하지 않는다. 지침 보강은 의미 오해를 줄이는 조치이며 결정론적 분류 보증이 아니다. 개인정보·사칭 live 모델 재평가는 독립된 대표 사례에서 추가 검증해야 한다.
