from dataclasses import dataclass
import os
from pathlib import Path

POLICY_DIR = Path(__file__).parent / "policies"
DEFAULT_POLICY_VERSION = "3.0"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# 테이블의 vector 차원과 같아야 한다. 모델을 바꾸면 스키마도 함께 바꾼다.
EMBEDDING_DIMENSIONS = 1536


@dataclass(frozen=True)
class IndexSettings:
    db_url: str
    openai_api_key: str
    embedding_model: str
    policy_version: str

    def __post_init__(self) -> None:
        if not self.db_url:
            raise ValueError("RAG_DB_URL is required")
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required")
        if not self.policy_version:
            raise ValueError("POLICY_VERSION cannot be empty")

    @property
    def policy_file(self) -> Path:
        return POLICY_DIR / f"policy-v{self.policy_version}.md"

    @classmethod
    def from_env(cls) -> "IndexSettings":
        # .env.example의 빈 값(KEY=)도 미설정으로 보고 기본값을 쓴다.
        return cls(
            db_url=os.getenv("RAG_DB_URL", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
            policy_version=os.getenv("POLICY_VERSION") or DEFAULT_POLICY_VERSION,
        )


@dataclass(frozen=True)
class ServerSettings:
    """RAG MCP 서버 설정. 값이 없어도 기동은 하고, /health와 /mcp 인증에서 미설정을 드러낸다."""

    db_url: str
    openai_api_key: str
    embedding_model: str
    agent_token: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("RAG_MCP_HOST cannot be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("RAG_MCP_PORT must be between 1 and 65535")

    @classmethod
    def from_env(cls) -> "ServerSettings":
        port_value = os.getenv("RAG_MCP_PORT", "8003")
        try:
            port = int(port_value)
        except ValueError as error:
            raise ValueError("RAG_MCP_PORT must be an integer") from error
        return cls(
            db_url=os.getenv("RAG_DB_URL", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
            agent_token=os.getenv("RAG_MCP_AGENT_TOKEN", ""),
            host=os.getenv("RAG_MCP_HOST", "127.0.0.1"),
            port=port,
        )

    @property
    def ready(self) -> bool:
        return bool(self.db_url and self.openai_api_key and self.agent_token)
