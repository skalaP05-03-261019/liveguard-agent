from dataclasses import dataclass, field
import math
import os
from urllib.parse import urlsplit

DEFAULT_ALLOWED_HOSTS = ("business-mcp:*", "localhost:*", "127.0.0.1:*")


@dataclass(frozen=True)
class Settings:
    backend_base_url: str
    backend_service_token: str = field(repr=False)
    agent_token: str = field(repr=False)
    host: str
    port: int
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    max_concurrent_requests: int = 16
    backend_connections: int = 16
    backend_timeout: float = 10.0
    shutdown_timeout: float = 20.0

    def __post_init__(self) -> None:
        parsed_url = urlsplit(self.backend_base_url)
        if (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc
                or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment):
            raise ValueError("BUSINESS_BACKEND_BASE_URL must be an absolute HTTP URL")
        if (min(self.max_concurrent_requests, self.backend_connections) < 1
                or any(not math.isfinite(v) or v <= 0
                       for v in (self.backend_timeout, self.shutdown_timeout))):
            raise ValueError("Business MCP limits must be positive and finite")
        if not self.host:
            raise ValueError("BUSINESS_MCP_HOST cannot be empty")
        if not self.allowed_hosts or any(not item.strip() or item == "*" for item in self.allowed_hosts):
            raise ValueError("BUSINESS_MCP_ALLOWED_HOSTS requires explicit hosts, not an empty list or '*'")

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
                "BUSINESS_MCP_ALLOWED_HOSTS", ",".join(DEFAULT_ALLOWED_HOSTS)
            ).split(",") if host.strip()),
            max_concurrent_requests=int(os.getenv("BUSINESS_MCP_MAX_REQUESTS", "16")),
            backend_connections=int(os.getenv("BUSINESS_MCP_BACKEND_CONNECTIONS", "16")),
            backend_timeout=float(os.getenv("BUSINESS_MCP_BACKEND_TIMEOUT", "10")),
            shutdown_timeout=float(os.getenv("BUSINESS_MCP_SHUTDOWN_TIMEOUT", "20")),
        )

    @property
    def ready(self) -> bool:
        return bool(self.backend_service_token.strip() and self.agent_token.strip())
