# AI2-01 Agent ↔ RAG MCP 정책 검색 계약

> 상태: 구현 기준 (2026-10-20, #4). 운영정책 v3.0 색인 기준이다. 도구 이름·입출력이 바뀌면 이 문서와 테스트를 함께 고친다.

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
        "version": "3.0",
        "category": null,
        "resourceUri": "policy://3.0/operating-policy/MP-4.1",
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
| `FLOOD` | CP-4.1 |
| `PHISHING` | CP-5.1, SP-2.1 |
| `PII_EXPOSURE` | SP-3.1 |
| 공통(null) | 그 외 16개 (CP-1.x, CP-2.2, CP-6.1, CP-7.1, MP-x, SP-4.1, MM-x) |

## 7. 색인과 버전

- 원문: `app/rag_mcp/policies/policy-v{version}.md` (노션 운영정책 Export). 색인: `uv run --env-file .env python -m app.rag_mcp.index`
- 임베딩: OpenAI `text-embedding-3-small`, 1536차원. 색인과 질의 모델이 다르면 `POLICY_INDEX_MISMATCH`.
- 정책 개정: 노션 수정 → 버전 올림 → 새 파일 추가 → 재색인. 이전 버전 행은 남아 과거 판단의 근거를 다시 볼 수 있다.

## 8. 확인된 동작 (2026-10-20, 실제 임베딩)

| 질의 (category) | 상위 3개 |
|---|---|
| 강퇴는 언제 승인 요청하나 (-) | MP-4.1, MP-3.2, SP-2.1 |
| 전화번호가 채팅에 올라왔다 (PII_EXPOSURE) | SP-4.1, SP-3.1, CP-1.1 |
| 같은 문장을 반복해서 보냄 (FLOOD) | MP-2.2, CP-4.1, MP-3.1 |

공통 조항이 유형 조항보다 위에 올 수 있으므로, Agent는 `topK` 결과 전체를 근거 후보로 보고 `category`가 일치하는 조항을 우선 인용한다.
