import uvicorn

from app.rag_mcp.config import ServerSettings


def main() -> None:
    settings = ServerSettings.from_env()
    uvicorn.run(
        "app.rag_mcp.main:app",
        host=settings.host,
        port=settings.port,
        factory=True,
    )


if __name__ == "__main__":
    main()
