# AI1-08 · Agent의 RAG MCP 소비자 계약

작성: 2026-10-20 · 김태관(AI1) · [이슈 #8](https://github.com/skalaP05-03-261019/liveguard-agent/issues/8)

## 범위와 현재 상태

AI2의 [#4](https://github.com/skalaP05-03-261019/liveguard-agent/issues/4) 계약으로 검색·조항·원문 Resource를 읽고 검증하는 Agent 소비자 구현이다. 정책 파싱·임베딩·pgvector·RAG 서버는 AI2 책임이며 이 PR에 구현하지 않는다. 실제 조사 모델·Graph 연결은 #9, 실제 조치·승인/재개는 #10이다. `/run`·`/resume`는 계속 501이며 실제 작업을 접수했다고 응답하지 않는다.

초기 검증은 계약 기반 test double 서버였다. 이후 #13의 실제 RAG FastAPI 앱·도구·Resource를 소비자에 연결하는 테스트를 추가했다. 아래 통합 기록처럼 실제 제공자 코드 검증과 제어된 임베딩/저장소를 구분한다. OpenAI 실제 임베딩을 통한 사용자 환경의 실행은 아직 미검증이다.

## 설정과 호출 경계

- `RAG_MCP_URL`: 제공자가 알려준 Streamable HTTP `/mcp` 주소. 기본 주소를 추정하지 않는다.
- `RAG_MCP_AGENT_TOKEN`: RAG 전용 Bearer 토큰. Business MCP 토큰으로 대체하지 않는다. process environment에서 읽으며 `.env` 자동 로드는 하지 않는다.
- 요청의 `policyVersion`은 `load_case`가 검증한 사건의 `policy_version`을 사용한다. `v3.0`, `3.0` 등의 wire 값을 임의로 고정하거나 다른 버전으로 자동 전환하지 않는다.
- tool 호출은 `search_policy`, `get_policy_section`만 허용한다. 변경·Business MCP 도구는 연결 전에 거절한다. Resource는 `policy://{version}/{documentId}/{sectionId}`만 허용하며 이를 외부 URL로 요청하지 않는다.
- 호출당 timeout은 10초다. 애플리케이션 자동 재시도·대체 정책은 없다. 공통 활성 시간·호출 수·재시도 예산은 #6, 마스킹·일반 trace 처리는 #7에서 호출 경계에 연결한다. SDK의 인증/discovery 교환과 실제 업무 도구 재시도는 구분한다.
- `input_required_max_rounds=0`, SDK 응답 캐시 비활성화로 암묵적인 입력 요청 반복·오래된 Resource 캐시를 피한다.

## Tool 입력·응답

```text
search_policy
입력: query(1~1000자), category?(5종), policyVersion, topK?(1~4, 기본4), traceId(UUID)
응답: {data:{matches:[{policyId,title,text,version,resourceUri,score}]},traceId}

get_policy_section
입력: policyId, policyVersion, traceId(UUID)
응답: {data:{policyId,title,text,version,resourceUri},traceId}
```

Agent는 매 호출에 작업 traceId를 보내고 응답의 동일 값을 검사한다. `score`는 유한한 숫자여야 한다. 검색 개수는 topK 이하여야 하며 같은 policyId 또는 URI 중복을 허용하지 않는다. 도구 설명에 없는 별도 필드·다른 버전으로 재시도하지 않는다. 제공자가 새로운 응답 메타데이터를 추가할 수 있지만 소비 필드 타입은 검증한다.

이 소비자는 기존 Business Client의 안전 오류·envelope decoder를 재사용한다. 별도 transport·설정·도구 허용 목록을 가지므로 Business 인증이나 변경 도구를 상속하지 않는다. 기존 Business 코드는 수정하지 않았다.

## Resource 표현: #13 제공자 구현과 확인

#13은 **단일 `text/markdown` 콘텐츠의 본문이 조항 전체 `text`와 정확히 동일**하게 구현했다. 소비자는 이 형식과 `text/plain`(미지정 MIME도 허용)을 지원한다. JSON이나 여러 콘텐츠면 추측해서 읽지 않고 `INVALID_TOOL_RESPONSE`로 중단한다. 실제 제공자 코드를 사용한 테스트로 형식을 확인한다.

Resource 응답의 URI가 요청 URI와 같은지 검사한다. `resources/read`에는 tool의 data/traceId envelope가 없으므로 결과의 traceId는 **Client가 로컬 상관관계용으로 붙인 값**이다. 서버가 이를 echo했다고 주장하지 않는다.

문서 ID/조항 ID는 제공자가 돌려준 URI를 사용하며 policyId에서 임의로 조립하지 않는다. 현재 제공자는 `policy://3.0/operating-policy/CP-2.1` 같은 주소를 돌려준다. 요청 버전과 URI 버전을 대조하고 검색·조항 응답의 policyId/URI/category가 같은지 확인한다. 최종 문서·조항 ID 매핑의 권위는 제공자 원본이다.

## 검증된 근거를 State에 넣는 과정

```text
load_case의 policy_version
  → search_policy
  → 검색의 version·URI·중복·타입 검사
  → get_policy_section(policyId)
  → 조항 ID·버전·URI 일치 검사
  → 검색 조각이 조항 전체 text에 포함되는지 검사
  → Resource 읽기
  → URI 및 본문 전체 일치 검사
  → 모든 결과가 통과한 경우에만 State patch 반환
```

`retrieve_policy_evidence(state, query=..., category=..., top_k=..., tools=...)`는 변경 전 State를 직접 수정하지 않는다. 전체 조항·ID·제목·버전·URI를 `policy_evidence`에 저장하고, 일반 `tool_observations`에는 호출 이름·ID·개수만 남긴다. 같은 버전/정책 ID에 원문이 달라지면 덮어쓰지 않고 중단한다. 기존 State 근거의 버전도 다시 검사한다.

제공자의 `category`도 보존한다. 위험 유형 조항은 5개 enum 중 하나, 모든 유형에 적용되는 정상 예외·일반 원칙·제재 기준 등 공통 조항은 null이다. 공통 조항을 위험 유형 위반으로 강제 분류하지 않는다.

검색 `text`가 조각일 수 있으므로 조항 전체와 무조건 같은 길이를 강제하지 않는다. 다만 조각이 전체 원문에 포함되지 않으면 받아들이지 않는다. HTML 변환·마스킹·정규화 차이는 자동 보정하지 않고 계약 차이로 확인한다.

**소비자 제안 제한:** 정책 조각·원문 하나는 최대 24,000자다. 초과 시 자르지 않고 오류로 중단한다. 이는 원문 전체 처리·프롬프트 예산과 다르며 #7 및 실제 정책 크기를 보고 조정한다. 원문·검색 내용은 신뢰할 수 없는 데이터이며 호출 권한이나 시스템 지시로 사용하지 않는다. 저장 근거는 일반 로그/모델 입력에 자동 주입되지 않는다.

## 오류와 다음 행동

| 코드 | 의미 | 소비자 행동 |
|---|---|---|
| INVALID_INPUT | query/topK/category/traceId 등 잘못된 입력 | 연결 전 중단 |
| FORBIDDEN / UNAUTHENTICATED | 허용 밖 도구·인증/권한 실패 | 중단, 비밀값 미노출 |
| NO_POLICY_EVIDENCE | 검색 자체는 성공했으나 matches가 비어 있음 | 판정하지 않음; #9에서 검토·추가 조회 분기 |
| POLICY_MISMATCH | 요청·검색·원문·URI·기존 State의 버전/내용 충돌 | 근거 갱신·변경 진행 중단 |
| INVALID_TOOL_RESPONSE | 잘못된 envelope/trace/타입/Resource 형식·크기 | 중단 |
| INVALID_POLICY_EVIDENCE | 중복·조항 ID·URI·조각/원문 내용 불일치 | 중단 |
| MCP_REQUEST_FAILED | Resource/프로토콜 요청 오류 | 안전 코드로 중단; 서버 원문 오류 노출 없음 |
| TEMPORARY_UNAVAILABLE | timeout·전송 장애 | retryable 표시만; 실제 재시도는 #6 |
| 제공자 ToolError의 안전 코드 | 없는 조항·버전 등 | 코드는 보존하되 원문 오류/토큰은 출력하지 않음 |

조회 실패·근거 없음은 `NO_VIOLATION`이 아니다. 이 PR은 제재를 실행하거나 판정을 만들어내지 않는다.

## 개발·실제 서버 확인

```bash
uv sync --locked --python 3.11
uv run --locked python -m unittest discover -s tests -v
LIVEGUARD_TEST_TCP=1 uv run --locked python -m unittest discover -s tests -v
```

제공자 서버·정확한 버전·환경변수 준비 후 다음 명령으로 실제 서버를 확인한다. 값은 제공자와 맞추고 토큰을 커밋/공유하지 않는다.

```bash
uv run --locked python -m app.check_rag \
  --policy-version '<제공자와 확인한 정확한 버전>' \
  --query '욕설 판단 조건과 정상 예외' --top-k 1
```

이 명령은 fixture를 사용하지 않는다. 성공 시 정책 ID·버전·URI·원문 해시만 출력하며, 원문·토큰·주소는 출력하지 않는다. 결과는 연결·출처 일치 검증이며 검색 품질·모델 판단·제재 실행 증거가 아니다. 빈 결과는 실패로 표시한다. 이 PR의 실제 서버 완료 기준은 이 검증 이후 별도 기록한다.

## 다음 #9와 공유 파일

Graph의 고정 조회 단계에 임의 검색을 삽입하지 않았다. #9의 모델 조사 루프가 필요한 query를 만들고 이 경계를 사용한다. 모델이 보고한 policyId는 검증된 `policy_evidence`와 대조한다. `app/state.py`에는 새 필드 하나만 추가했으며 Graph·resume·예산·프롬프트는 수정하지 않았다. #5의 공유 State 변경과 병합 시 함께 확인한다.

실제 승인/강퇴 구현과 수업의 변경 전 승인 기준 차이는 #10에서 다룬다. 이 PR은 조회 전용이다.

## #13 병합 후 통합 기록 (2026-10-20)

- 인희님의 Slack 승인과 사용자의 명시적 실행 요청에 따라 #13의 정확한 head `2a7d8f0`를 확인하고 머지했다. main merge commit은 `096049c`다. #4는 종료됐다.
- 최신 main을 #12 브랜치에 병합했다. `.env.example`의 동일 토큰 변수 중복만 정리하고 제공자 코드와 Graph/State 변경은 덮어쓰지 않았다.
- `tests/test_rag_consumer_integration.py`는 실제 제공자 `create_app`/도구/Resource를 실행한다. 모델 없는 계약 검증은 제어된 저장소·임베딩으로 5개 유형, 공통 예외, 실제 제공자 오류 코드를 확인한다.
- DB 통합 테스트는 실제 정책 23개를 **별도 임시 버전**으로 pgvector에 넣고, SQL 유사도 검색→실제 MCP 앱→전체 원문 Resource→소비자 State를 확인한 뒤 임시 버전을 삭제한다. 임베딩은 결정적인 테스트 벡터다.
- CI에 임시 pgvector 서비스를 추가해 기존 5개 DB 테스트와 새 소비자 DB 테스트를 실행한다. 실제 TCP는 CI에서 검증하며 로컬 네트워크 제한은 기능 실패와 구분한다.
- 로컬 전체 결과: 61 tests, 실패 없음, DB 의존 6개 skip. 설치는 uv.lock의 해시 고정 requirements를 사용했다. CI 결과는 PR과 작업공간의 진행 기록으로 갱신한다.
- 남은 외부 실행: 사용자/통합 환경의 실제 OpenAI 질의 임베딩→색인→MCP 소비자 실행. 이 기록을 실제 모델 조사·제재·Harness 완료 증거로 사용하지 않는다.
