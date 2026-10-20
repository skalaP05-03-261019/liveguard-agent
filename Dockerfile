# LIVE:GUARD agent 레포 공용 이미지.
# Agent 서버, Business MCP, RAG MCP는 같은 코드·의존성을 쓰고 시작 명령만 다르다.
#   Agent 서버:   (기본) uvicorn app.main:app --host 0.0.0.0 --port 8001
#   Business MCP: python -m app.business_mcp   (BUSINESS_MCP_HOST=0.0.0.0)
#   RAG MCP:      python -m app.rag_mcp        (RAG_MCP_HOST=0.0.0.0)
#   RAG 첫 색인:  python -m app.rag_mcp.index  (1회 실행)
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 의존성 레이어를 먼저 만들어 코드만 바뀔 때 재설치를 피한다.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY app ./app
COPY prompts ./prompts

RUN useradd --create-home --uid 10001 liveguard && chown -R liveguard /app
USER liveguard

EXPOSE 8001 8002 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
