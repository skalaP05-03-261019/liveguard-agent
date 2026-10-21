from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    backend_base_url: str
    backend_service_token: str
    agent_token: str
    host: str
    port: int
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*")

    def __post_init__(self) -> None:
        parsed_url = urlsplit(self.backend_base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("BUSINESS_BACKEND_BASE_URL must be an absolute HTTP URL")
        if not self.host:
            raise ValueError("BUSINESS_MCP_HOST cannot be empty")

    @classmethod
    def from_env(cls) -> "Settings":
        port_value = os.getenv("BUSINESS_MCP_PORT", "8002")
        try:
            port = int(port_value)
        except ValueError as error:
            raise ValueError("BUSINESS_MCP_PORT must be an integer") from error
        if not 1 <= port <= 65535:
            raise ValueError("BUSINESS_MCP_PORT must be between 1 and 65535")

        return cls(
            backend_base_url=os.getenv(
                "BUSINESS_BACKEND_BASE_URL", "http://127.0.0.1:8080"
            ).rstrip("/"),
            backend_service_token=os.getenv("BUSINESS_MCP_SERVICE_TOKEN", ""),
            agent_token=os.getenv("BUSINESS_MCP_AGENT_TOKEN", ""),
            host=os.getenv("BUSINESS_MCP_HOST", "127.0.0.1"),
            port=port,
            allowed_hosts=tuple(host.strip() for host in os.getenv(
                "BUSINESS_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*"
            ).split(",") if host.strip()),
        )

    @property
    def ready(self) -> bool:
        return bool(self.backend_service_token and self.agent_token)
