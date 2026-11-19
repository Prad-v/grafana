import logging

import uvicorn

from .config import Settings
from .server import create_app


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # --proxy-headers is off on purpose: the only caller is the Grafana plugin proxy on the same network.
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_level=settings.log_level)


if __name__ == "__main__":
    main()
