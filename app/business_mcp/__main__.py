import uvicorn

from app.business_mcp.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "app.business_mcp.main:app",
        host=settings.host,
        port=settings.port,
        factory=False,
    )


if __name__ == "__main__":
    main()
