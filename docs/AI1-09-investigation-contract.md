# AI1-09 조사·판정 연결 (#9)

김태관 / 2026-10-20. #12 위에 쌓은 PR이며, #12 병합 후 main 대상으로 변경한다.
완료 선언이 아니라 구현·검증 범위 기록이다. 실제 모델 실행은 공급자/모델/인증 설정 후 별도 확인한다.

## 실행 경계

`build_investigation_graph(model=..., business=..., policy=...)`:
사건 조회 → 모델의 조회 도구 선택 → 검색/조항/Resource 검증 → TARGET별 판정 검증 → 종료.
`app/nodes.py`의 investigate/validate_judgment가 이 모듈을 사용한다.
최상위 graph.py/state.py/main.py는 변경하지 않는다. 기존 최상위 Graph의 legacy judgment 분기,
업무 조치 및 결과 보고 연결은 #10에서 #14 변경과 함께 정리해야 한다.
따라서 /run·/resume가 실행 가능해졌다는 의미가 아니다.

모델에게 제공하는 기능:
- search_policy(query, category?, topK=2): #8 소비자로 원문까지 검증. 동일 버전 고정.
- get_policy_section(policyId): 알려진 조항을 직접 조회하고 Resource 원문·버전을 검증. 모델이 버전/URI를 선택하지 않는다.
- get_chat_context / get_user_history / get_stream_info / get_official_links / get_security_facts:
  모델 입력은 빈 객체. runtime이 현재 caseId·traceId를 주입한다. 이력은 30일이며 강퇴 집계가 아니다.
- finish_investigation(messageFindings): TARGET마다 정확히 하나의 판정 제출.

직접 변경 도구, 외부 URL 방문, DB 쓰기, 다른 사건 조회, 제재 기간 생성은 제공하지 않는다.
chat 재조회가 원 사건 snapshot/evidenceVersion 또는 메시지 내용과 다르면 중단한다.

## 판정 검증

모든 finding은 NO_VIOLATION / VIOLATION / NEEDS_REVIEW를 사용한다.
정상 categories는 현재 Python 계약의 빈 배열이다. BE의 필드 생략 요구와 최종 wire 계약은 #10에서 맞춘다.
정상·위반 모두 조회한 정책과 자신의 TARGET 메시지를 근거로 포함한다.
주변 CONTEXT는 근거로만 사용할 수 있다. 정책/증거 ID를 만들거나 중복 판정을 제출하면 실패한다.
위반 categories는 인용 정책의 유형에 포함돼야 한다. 공통(category=null) 정책만으로 위반 유형을 확정하지 않는다.
검색 결과 없음은 NO_POLICY_EVIDENCE이며 정상 판정으로 취급하지 않는다. 부족한 사실을 적은 검토 보류로 끝낼 수 있다.
버전 불일치·도구 장애·권한 오류·stale·모델 거절/잘림/비정상 응답은 판정 성공으로 바꾸지 않는다.
상태 업데이트는 검증 후 원자적으로 반환하며 입력 상태는 변경하지 않는다.

이 검사는 참조의 실재 여부와 형식을 검사한다. 문장의 의미가 옳은지, 정상 예외를 제대로 해석했는지,
PII 실체를 실제로 입증했는지까지 증명하지 않는다. 프롬프트의 미확인 PII 보류 규칙과
Backend의 최종 정책·사실 검증은 별도로 필요하다. 실제 모델 평가가 남아 있다.

## #6/#7 연결 지점

`run_investigation(..., gate=..., present_data=...)`:
- gate.run(name, async operation)는 모델 1회 및 실제 MCP search/section/Resource 호출 각각을 감싼다.
- #6은 Graph 전체 공유 gate를 전달하고 재시도·지속 시간·실패 기록을 소유한다. 별도 local gate를 중첩하지 않는다.
- standalone 기본값은 8 모델 호출 / 같은 도구 3회 / 활성 120초의 최소 차단, 재시도 0회다.
  기존 turn_count·tool_observations를 초기 카운터에 반영한다. 승인 대기를 다루지 않는 읽기 전용 루프다.
  영속적인 전체 Graph 시간/재시도 예산은 #6을 대체하지 않는다.
- Resource는 read_policy_resource 이름으로 계수한다. topK 최대 3(기본 2)이라 한 번의 검색만으로 같은 도구 한도를 넘지 않도록 한다.
- present_data는 모델에 보내기 전 채팅/도구 결과를 처리한다. 기본 구현은 전화/이메일/연속 숫자 마스킹과
  문자열 12,000자 제한 및 [TRUNCATED] 표시다. 잘린 문맥에서는 확정 판정을 거부한다.
  JSON 메시지 48,000자 초과는 실패한다. #7의 마스킹/예산/관측 구현으로 교체할 수 있다.
- 기본 마스킹은 모든 개인정보 형식을 검출하지 않는다. Backend 마스킹과 #7 검증은 계속 필요하다.
- 일반 관측에는 도구명, traceId, 모델 호출 수, 제공된 토큰 수만 기록한다. 원문과 API 키를 로그에 넣지 않는다.
  지연/비용/복구/승인 관측은 #6/#7과 연결한다. 모델 미제공 usage는 빈 객체다.

## 대화 모델

`InvestigationModel.complete(messages, tools, trace_id=...) -> ModelTurn`을 구현하면 공급자를 교체할 수 있다.
현재 추가한 OpenAI 어댑터는 명시적으로 MODEL_PROVIDER=openai를 설정해야만 활성화된다.
사용자가 OpenAI API 사용을 확정했다. 모델명은 별도 설정이며 기본값은 없다. RAG 임베딩과 대화 모델은 별개다.
모델은 Chat Completions function calling을 지원해야 한다. SDK 자동 재시도는 0회이고
API 오류의 원문을 밖으로 노출하지 않는다. 토큰 잘림·거절·도구 없는 답변은 실패한다.
응답은 한 번에 한 도구만 허용하고 tool_call_id를 후속 결과와 일치시킨다.

실제 모델/실제 MCP 확인 (환경변수는 shell에서 공급; 비밀값 커밋 금지):

```bash
# MODEL_PROVIDER, MODEL_NAME, MODEL_API_KEY
# BUSINESS_MCP_URL, BUSINESS_MCP_AGENT_TOKEN, RAG_MCP_URL, RAG_MCP_AGENT_TOKEN 필요
uv run --locked python -m app.check_investigation --task-json /path/to/backend-task.json
```

입력은 실제 Backend에 저장된 ANALYZE/REANALYZE 작업 envelope다. 테스트 fixture ID를 실제 사건처럼 사용하지 않는다.
CLI는 verdict/참조 ID/호출 수만 출력하고 원문·reason·키는 출력하지 않는다. 제재는 실행하지 않는다.
설정 부족이면 종료 코드 2, 도구/모델 오류면 종료 코드 1이다. fixture fallback은 없다.

## 검증

```bash
uv run --locked python -m unittest discover -s tests -v
```

고정 응답 모델로 정상 인용/위반/보류·복수 TARGET/유형·없는 정책/메시지·중복·stale·변경 도구 요청 차단,
모델 호출 ID 연계·모델/도구/시간 제한·마스킹·잘림·원자적 상태 반환을 확인한다.
생산 RAG 앱 연결 시험은 실제 SDK/앱/도구/Resource를 쓰되 저장소/임베딩/모델은 대역이다.
OpenAI 어댑터는 실제 SDK+HTTP MockTransport로 API 요청·토큰 수·429 단일 시도·거절·잘림·JSON 오류를 확인한다.
이 증거는 실제 모델의 분류 정확도, 실제 OpenAI 호출, 실제 BE 연결을 입증하지 않는다.

공식 API 확인: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) (2026-10-20).


2026-10-20 후속: 실제 OpenAI 연결을 확인했고 모델 품질 평가 실패도 발견했다.
[실제 검증 결과](AI1-09-live-openai-results.md)를 최신 증거로 확인한다.
