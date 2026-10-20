# AI2-05 agent 레포 Docker 이미지와 compose 통합 안내

> 상태: 2026-10-20, #22. compose 파일 통합은 Infra(강인서) 담당이다. 아래는 검증된 서비스 정의 예시다.

## 1. 이미지

agent 레포 루트의 `Dockerfile` 하나로 세 서비스를 띄운다. 코드·의존성(`uv.lock`)이 같고 시작 명령만 다르다.

```bash
docker build -t liveguard-agent .
```

| 서비스 | 명령 | 포트 |
|---|---|---|
| agent | (기본) `uvicorn app.main:app --host 0.0.0.0 --port 8001` | 8001 |
| business-mcp | `python -m app.business_mcp` | 8002 |
| rag-mcp | `python -m app.rag_mcp` | 8003 |
| rag-index (1회) | `python -m app.rag_mcp.index` | – |

## 2. compose 서비스 예시

DB는 Backend compose의 `pgvector/pgvector:pg16`을 함께 쓴다. RAG는 `rag` 스키마, checkpointer는 `agent` 스키마를 스스로 만들며 Backend의 `platform`·`guard` 스키마와 겹치지 않는다. 아래 `db`, `backend`, 계정 값은 Backend compose에 맞춰 바꾼다.

```yaml
  rag-index:
    image: liveguard-agent
    command: ["python", "-m", "app.rag_mcp.index"]
    environment:
      RAG_DB_URL: postgresql://<user>:<password>@db:5432/<db>
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      POLICY_VERSION: "3.1"
    depends_on:
      db: {condition: service_healthy}
    restart: "no"

  rag-mcp:
    image: liveguard-agent
    command: ["python", "-m", "app.rag_mcp"]
    environment:
      RAG_MCP_HOST: 0.0.0.0
      RAG_MCP_PORT: "8003"
      RAG_MCP_ALLOWED_HOSTS: rag-mcp:*,localhost:*,127.0.0.1:*
      RAG_DB_URL: postgresql://<user>:<password>@db:5432/<db>
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      RAG_MCP_AGENT_TOKEN: ${RAG_MCP_AGENT_TOKEN}
    depends_on:
      rag-index: {condition: service_completed_successfully}

  business-mcp:
    image: liveguard-agent
    command: ["python", "-m", "app.business_mcp"]
    environment:
      BUSINESS_MCP_HOST: 0.0.0.0
      BUSINESS_MCP_PORT: "8002"
      BUSINESS_BACKEND_BASE_URL: http://backend:8080
      BUSINESS_MCP_AGENT_TOKEN: ${BUSINESS_MCP_AGENT_TOKEN}
      BUSINESS_MCP_SERVICE_TOKEN: ${BUSINESS_MCP_SERVICE_TOKEN}

  agent:
    image: liveguard-agent
    environment:
      BUSINESS_MCP_URL: http://business-mcp:8002/mcp
      BUSINESS_MCP_AGENT_TOKEN: ${BUSINESS_MCP_AGENT_TOKEN}
      RAG_MCP_URL: http://rag-mcp:8003/mcp
      RAG_MCP_AGENT_TOKEN: ${RAG_MCP_AGENT_TOKEN}
      AGENT_CHECKPOINT_DB_URL: postgresql://<user>:<password>@db:5432/<db>
      MODEL_PROVIDER: openai
      MODEL_NAME: ${MODEL_NAME}
      MODEL_API_KEY: ${OPENAI_API_KEY}
    depends_on: [rag-mcp, business-mcp]
```

- agent·MCP 서비스는 외부 공개 포트가 필요 없다(내부 네트워크). 로컬 확인용으로만 `ports`를 연다.
- 비밀값(API 키, 토큰)은 compose `.env`로 주입하고 커밋하지 않는다.
- 정책을 개정하면 `POLICY_VERSION`을 올리고 `rag-index`를 다시 실행한다. Backend의 사건 `policyVersion`도 같은 값이어야 한다.

## 3. 컨테이너 간 호출과 Host 검사

MCP SDK 2.3의 Streamable HTTP는 DNS rebinding 방지를 위해 Host 헤더를 검사하며, 기본값은 `localhost`/`127.0.0.1`만 허용한다. compose에서 Agent는 `Host: rag-mcp:8003`처럼 서비스 이름으로 호출하므로 허용 목록에 서비스 이름을 넣어야 한다. 넣지 않으면 `421 Invalid Host header`로 실패한다.

- RAG MCP: `RAG_MCP_ALLOWED_HOSTS`로 설정한다(이 PR).
- Business MCP: 같은 문제가 있다(2026-10-20 컨테이너 검증에서 421 확인). `streamable_http_app(transport_security=TransportSecuritySettings(allowed_hosts=[...], allowed_origins=[...]))`로 같은 방식의 설정이 필요하다.

## 4. 검증 기록 (2026-10-20, 임시 compose)

| 확인 | 결과 |
|---|---|
| rag-index | `indexed policy v3.1: 29 sections (text-embedding-3-small)` |
| rag-mcp `/health` | `ready`, `indexedPolicyVersions: ["3.1"]`, 토큰 없는 `/mcp` 401 |
| business-mcp `/health` | `ready` |
| agent `/health` | `scaffold` (실행기 연결 #10 전 정상) |
| agent 컨테이너 → rag-mcp (`app.check_rag`) | `verified_against_configured_rag_server` (SP-2.1, CP-5.1) |
| agent 컨테이너 → business-mcp | `421 Invalid Host header` (위 3 참고) |
| DB 스키마 | `rag`(29행), checkpointer `agent` |
