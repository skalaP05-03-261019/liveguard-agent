# AI2-01 Agent ↔ RAG MCP 정책 검색 계약

> 상태: 구현 기준 (2026-10-20, #4, #15). 활성 색인은 운영정책 v3.1(팀 POLICY 개정 반영), v3.0은 과거 판단 근거로 유지한다. 도구 이름·입출력이 바뀌면 이 문서와 테스트를 함께 고친다.

## 1. 목적과 책임 경계

- RAG MCP는 운영정책 원문을 조항 단위로 검색하고 원문을 돌려준다. 업무 데이터를 읽거나 바꾸지 않는다.
- 위반 판단과 제재 계산은 하지 않는다. Agent는 검색 결과의 `policyId`를 판단 근거(`policyIds`)로 인용하고, 제재는 Backend가 계산한다.
- 모든 도구는 조회 전용(`readOnlyHint`)이다. 조사 모델에 그대로 노출해도 된다.

## 2. 연결

| 항목 | 값 |
|---|---|
| Transport | MCP SDK 2.3 Streamable HTTP, FastAPI `/mcp` (Business MCP와 동일) |
| 로컬 주소 | `http://127.0.0.1:8003/mcp` (`RAG_MCP_HOST`, `RAG_MCP_PORT`) |
| 인증 | `Authorization: Bearer ${RAG_MCP_AGENT_TOKEN}`. 토큰 미설정 서버는 503, 불일치는 401 |
| Agent 측 설정 | `RAG_MCP_URL`, `RAG_MCP_AGENT_TOKEN` |
| 상태 확인 | `GET /health` → `{status, readyForTasks, indexedPolicyVersions}`. 비밀값 미노출 |

Agent의 `app/clients/business_mcp.py`와 같은 방식(Bearer 헤더 + `streamable_http_client`)으로 연결하면 된다. 오류 해석(`_decode_result`)도 그대로 쓸 수 있다.

## 3. 공통 표현

- JSON 필드는 camelCase. 성공 결과는 `{"data": {...}, "traceId": "uuid"}`.
- 모든 도구는 optional `traceId`를 받는다. Agent는 작업 envelope의 `traceId`를 항상 넘긴다. 생략하면 서버가 새 UUID를 만든다.
- 오류는 MCP tool 오류(`isError: true`)로 돌려주며 본문은 Business MCP와 같은 envelope이다. SDK가 앞에 `Error executing tool <name>: `를 붙인다.

```json
{"error": {"code": "POLICY_VERSION_NOT_FOUND", "message": "...", "retryable": false}, "traceId": "uuid"}
```

| code | retryable | 의미 / Agent 처리 |
|---|---|---|
| `POLICY_VERSION_NOT_FOUND` | false | 색인되지 않은 policyVersion. 메시지에 사용 가능한 버전 목록 포함 |
| `POLICY_SECTION_NOT_FOUND` | false | 없는 조항 ID. 메시지에 해당 버전의 조항 ID 목록 포함 |
| `POLICY_INDEX_MISMATCH` | false | 색인과 질의의 임베딩 모델이 다름. 재색인 필요 |
| `TEMPORARY_UNAVAILABLE` | true | DB·임베딩 API 일시 장애. Harness 재시도 예산 안에서만 재시도 |

입력 검증 실패(topK 5, 없는 category, 형식이 틀린 policyId 등)는 SDK가 인자 거부 오류로 돌려준다.

## 4. 도구

### 4.1 `search_policy`

위반 정의·예외·제재 조건 조항을 의미 검색한다.

| 입력 | 타입 | 규칙 |
|---|---|---|
| `query` | string | 1~1000자. 판단에 필요한 내용을 자연어로 |
| `policyVersion` | string | 필수. 사건 조회(`get_moderation_case`)의 `policyVersion`을 그대로 쓴다 |
| `category` | enum? | `PROFANITY`, `HARASSMENT`, `FLOOD`, `PHISHING`, `PII_EXPOSURE`. 주면 해당 유형 조항 + 공통 조항만 검색 |
| `topK` | int? | 1~4, 기본 4 |
| `traceId` | uuid? | 작업 traceId |

```json
{
  "data": {
    "matches": [
      {
        "policyId": "MP-4.1",
        "title": "강퇴 심사",
        "text": "- **집계 대상:** ...",
        "version": "3.1",
        "category": null,
        "resourceUri": "policy://3.1/operating-policy/MP-4.1",
        "score": 0.4189
      }
    ]
  },
  "traceId": "uuid"
}
```

- `data`에는 `matches`만 있다(Agent 소비자가 다른 최상위 필드를 거부). 각 match의 `version`이 요청한 policyVersion이다.
- `score`는 코사인 유사도(1에 가까울수록 유사). 절대 기준값으로 쓰지 말고 순위로만 쓴다.
- `category: null`은 모든 유형에 적용되는 공통 조항(판단 원칙, 오탐 예외, 제재표, 강퇴 심사 등)이다.

### 4.2 `get_policy_section`

조항 ID로 원문 하나를 정확히 조회한다. 근거로 인용한 조항을 다시 확인할 때 쓴다.

| 입력 | 타입 | 규칙 |
|---|---|---|
| `policyId` | string | `^(CP\|SP\|MP\|MM)-\d+\.\d+$` (예: `CP-2.1`) |
| `policyVersion` | string | 필수 |
| `traceId` | uuid? | |

결과 `data`는 `search_policy`의 match에서 `score`를 뺀 형태다.

## 5. Resource

| URI 템플릿 | MIME | 내용 |
|---|---|---|
| `policy://{version}/{documentId}/{sectionId}` | `text/markdown` | 단일 콘텐츠. 본문은 `get_policy_section`의 `text`와 **정확히 같은 문자열**(제목 미포함) |

현재 `documentId`는 `operating-policy` 하나다. 운영자 화면의 근거 패널은 판단 결과의 `resourceUri`로 원문을 읽고, 제목은 도구 결과의 `title`을 쓴다. Agent 소비자([AI1-08](AI1-08-rag-read-contract.md))는 URI 일치와 본문 완전 일치를 검사한다.

## 6. 조항과 위험 유형

| 위험 유형 | 조항 |
|---|---|
| `PROFANITY` | CP-2.1 |
| `HARASSMENT` | CP-3.1, CP-3.2 |
| `FLOOD` | CP-4.1(최초 적발), CP-4.2(재적발) |
| `PHISHING` | CP-5.1(링크 전송 금지), CP-5.2(사칭), SP-2.1(공식 링크 정보의 사용 범위) |
| `PII_EXPOSURE` | SP-3.1 |
| 공통(null) | 그 외 20개 (CP-1.x, CP-2.2, CP-6.1, CP-7.1, MP-x, SP-4.1, MM-x) |

v3.1 기준 29개 조항. v3.0(23개)에는 CP-4.2, CP-5.2, MP-1.3, MP-1.4, MP-4.2, MP-4.3이 없다.

## 7. 색인과 버전

- 원문: `app/rag_mcp/policies/policy-v{version}.md` (노션 운영정책 Export). 색인: `uv run --env-file .env python -m app.rag_mcp.index`
- 임베딩: OpenAI `text-embedding-3-small`, 1536차원. 색인과 질의 모델이 다르면 `POLICY_INDEX_MISMATCH`.
- 정책 개정: 노션 수정 → 버전 올림 → 새 파일 추가 → 재색인. 이전 버전 행은 남아 과거 판단의 근거를 다시 볼 수 있다.

- 활성 버전 값은 Backend·Agent·RAG가 같아야 한다(팀 TBD-08). 현재 RAG 기본 색인은 `3.1`이며, `get_moderation_case`의 `policyVersion`이 `3.1`이어야 v3.1 조항을 검색한다.

## 8. 확인된 동작 (2026-10-20, v3.1 실제 임베딩)

| 질의 (category) | 상위 3개 |
|---|---|
| 공식 사이트 링크를 채팅에 보내도 되나 (PHISHING) | SP-2.1, CP-5.1, MP-1.2 |
| 문맥 의심 채팅을 AI가 정상이라고 판단하면 바로 공개하나 (-) | MP-1.3, CP-1.2, CP-3.2 |
| 강퇴를 거절하면 심사에 쓴 사건은 어떻게 되나 (-) | MP-4.1, MP-4.2, MP-4.3 |
| 도배 안내 후 같은 문장을 다시 보냄 (FLOOD) | CP-4.1, CP-4.2, MM-1.1 |
| 인용하면서 욕설을 쓴 채팅 (PROFANITY) | CP-2.1, CP-2.2, CP-1.2 |
| 괴롭힘 세 번째 위반 채팅 제한 시간 (-) | MP-2.2, MM-2.1, MP-2.1 |

공통 조항이 유형 조항보다 위에 올 수 있으므로, Agent는 `topK` 결과 전체를 근거 후보로 보고 `category`가 일치하는 조항을 우선 인용한다.

## 9. 검색 정확도 평가 (#28, 2026-10-21)

`uv run --env-file .env python -m app.rag_mcp.evaluate --out evidence/rag-retrieval-eval-v3.1.json`

- 평가셋: `app/rag_mcp/evalsets/policy-v3.1.json`. 실제 상황 질문 24개와 정답으로 인정하는 조항 ID. 정답 조항이 원문에 있는지는 `tests/test_rag_eval.py`가 확인한다.
- 경로: `search_policy`와 같은 질의 임베딩 → `repository.search`. 유형 미지정(unfiltered)과 Agent가 넘길 법한 유형 지정(category)을 모두 잰다.

| 방식 | 1위 적중 | 4위 안 적중 (기본 topK) | MRR |
|---|---|---|---|
| 유형 미지정 | 58% (14/24) | 92% (22/24) | 0.733 |
| 유형 지정 | 58% (14/24) | 96% (23/24) | 0.747 |

해석:
- Agent는 상위 4개를 모두 근거 후보로 받고 원문을 다시 읽으므로, 4위 안 적중이 실제 영향에 가깝다. 정답이 4개 안에 거의 항상 들어온다.
- 1위 적중이 낮은 이유는 순서다. 비슷한 공통 조항(MP-2.2 동시 위반, SP-4.1 원문 보호 등)이 유형 조항보다 위에 오는 경우가 많다. 위 8절의 "category가 일치하는 조항을 우선 인용"이 이것을 보완한다.
- 놓친 질문:
  - Q03 "닉네임을 부르며 반복 조롱": 유형 미지정 시 괴롭힘 조항(CP-3.2)이 4위 밖이었고, 유형 지정 시 3위였다.
  - Q16 "욕설·괴롭힘 동시 해당 시 적용": 두 방식 모두 MP-2.2가 4위 밖이었다.
  - 짧은 조항일수록 질문 표현과 겹치는 말이 적어 밀린다.
- 개선 후보(미적용): 임베딩 입력에 유형 이름과 대표 표현 추가, 키워드 검색과의 혼합, 상위 결과 재순위. 평가셋에 맞춰 조정하면 수치만 좋아질 수 있으므로, 적용하려면 별도 질문 세트로 다시 검증해야 한다.
- 한계: 질문은 내가 정책을 보고 작성했다. 실제 Agent가 만드는 질의 표현과 다를 수 있다.
