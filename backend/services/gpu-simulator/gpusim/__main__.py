"""Entrypoint: `python -m gpusim`. Configuration comes from the environment."""

from __future__ import annotations

import logging
import os
import random
import signal
import threading
import time

from .engine import Fleet
from .logs import LogShipper
from .server import make_server


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def main() -> None:
    logging.basicConfig(level=os.environ.get("SIM_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("gpusim")

    tick = _env_float("SIM_TICK_SECONDS", 5)
    seed = os.environ.get("SIM_SEED")
    shipper = LogShipper(os.environ.get("VICTORIALOGS_URL", ""))
    fleet = Fleet(
        nodes_per_dc=int(os.environ.get("SIM_NODES_PER_DC", 4)),
        gpus_per_node=int(os.environ.get("SIM_GPUS_PER_NODE", 8)),
        rng=random.Random(int(seed)) if seed else None,
        tick_seconds=tick,
        log_sink=shipper.push,
    )
    fleet.settings.log_rate = _env_float("SIM_LOG_RATE", 0.2)
    shipper.start()

    stop = threading.Event()

    def loop() -> None:
        # Sleep to the next boundary instead of a fixed delay so ticks do not drift.
        next_at = time.monotonic()
        while not stop.is_set():
            fleet.tick()
            next_at += tick
            stop.wait(max(0.0, next_at - time.monotonic()))

    threading.Thread(target=loop, name="engine", daemon=True).start()

    links = {
        "Grafana": os.environ.get("SIM_LINK_GRAFANA", "http://localhost:3000"),
        "VictoriaMetrics": os.environ.get("SIM_LINK_VICTORIAMETRICS", "http://localhost:8428/vmui"),
        "VictoriaLogs": os.environ.get("SIM_LINK_VICTORIALOGS", "http://localhost:9428/select/vmui"),
        "Metrics": "/metrics",
    }
    server = make_server(fleet, shipper, "0.0.0.0", int(os.environ.get("SIM_PORT", 8090)), links)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    total = sum(len(n.gpus) for n in fleet.nodes)
    log.info("simulating %d GPUs on %d nodes, UI on :%s", total, len(fleet.nodes), server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        shipper.stop()
        shipper.flush()
        server.server_close()


if __name__ == "__main__":
    main()
