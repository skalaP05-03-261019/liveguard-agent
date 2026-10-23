import uvicorn

from app.business_mcp.config import Settings
from app.business_mcp.main import create_app


class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig, frame):
        self.config.app.state.draining = True
        super().handle_exit(sig, frame)


def main() -> None:
    settings = Settings.from_env()
    server = DrainingServer(uvicorn.Config(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=False,
        timeout_graceful_shutdown=int(settings.shutdown_timeout),
    ))
    server.run()


if __name__ == "__main__":
    main()
