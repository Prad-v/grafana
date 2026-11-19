"""Log message catalogue and the shipper that pushes records to VictoriaLogs."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

from .topology import WORKLOADS

log = logging.getLogger("gpusim.logs")

# Stream fields keep the number of VictoriaLogs streams bounded; everything else
# (gpu, UUID, pod, ...) is a regular, searchable field.
STREAM_FIELDS = "region,datacenter,cluster,node,app"

# Source key (what the UI toggles) -> app label values it produces.
SOURCES = {
    "kernel": ("kernel",),
    "dcgm-exporter": ("dcgm-exporter",),
    "nvidia-fabricmanager": ("nvidia-fabricmanager",),
    "nvidia-device-plugin": ("nvidia-device-plugin",),
    "workload": ("training-job", "inference-server", "batch-job"),
}
SOURCE_WEIGHTS = {
    "workload": 5,
    "dcgm-exporter": 2,
    "kernel": 1,
    "nvidia-fabricmanager": 1,
    "nvidia-device-plugin": 1,
}
LEVELS = ("info", "warn", "error")

# NVIDIA Xid codes: (description, level)
XID = {
    13: ("Graphics Engine Exception", "error"),
    31: ("GPU memory page fault", "error"),
    43: ("GPU stopped processing", "error"),
    45: ("Preemptive cleanup, due to previous errors", "warn"),
    48: ("Double Bit ECC Error", "error"),
    63: ("ECC page retirement or row remapping recording event", "warn"),
    64: ("ECC page retirement or row remapper recording failure", "error"),
    74: ("NVLink Error", "error"),
    79: ("GPU has fallen off the bus", "error"),
    94: ("Contained ECC error", "warn"),
    95: ("Uncontained ECC error", "error"),
    119: ("GSP RPC timeout", "error"),
}
XID_CODES = tuple(XID)
_PROCS = ("python3", "pt_main_thread", "vllm", "nccl-tests", "blender")


def xid_message(gpu, code: int, rng) -> tuple[str, str]:
    desc, level = XID[code]
    pid = rng.randint(1000, 99999)
    proc = rng.choice(_PROCS)
    return f"NVRM: Xid (PCI:{gpu.pci_bus_id[4:].lower()}): {code}, pid={pid}, name={proc}, {desc}", level


def background_message(app: str, level: str, node, gpu, rng) -> str:
    """One routine line for `app` at `level`, as produced by healthy-ish hardware."""
    i = gpu.index
    if app == "kernel":
        if level == "error":
            return xid_message(gpu, 13, rng)[0] + f", ESR 0x{rng.getrandbits(24):06x}"
        if level == "warn":
            return (
                f"pcieport {gpu.pci_bus_id[5:].lower()}: AER: Corrected error received: "
                f"{gpu.pci_bus_id[5:].lower()}"
            )
        return rng.choice(
            (
                f"NVRM: GPU {gpu.pci_bus_id[4:].lower()}: persistence mode enabled",
                f"nvidia-uvm: Loaded the UVM driver, major device number {rng.choice((234, 235, 511))}",
                f"nvidia {gpu.pci_bus_id[5:].lower()}: GSP firmware heartbeat ok",
            )
        )
    if app == "dcgm-exporter":
        if level == "error":
            return f"failed to read field DCGM_FI_DEV_GPU_TEMP for GPU {i}: DCGM_ST_NO_DATA"
        if level == "warn":
            return f"scrape took {rng.uniform(1.1, 3.4):.1f}s, above the 1s warning threshold"
        return rng.choice(
            (
                f"collected {len(node.gpus) * 48} samples from {len(node.gpus)} GPUs in {rng.randint(18, 90)}ms",
                f"health watch passed for GPU {i}",
            )
        )
    if app == "nvidia-fabricmanager":
        if level == "error":
            return f"NVSwitch {rng.randint(0, 3)} reported a fatal error, link {rng.randint(0, 17)} is down"
        if level == "warn":
            return f"NVLink training retry on GPU {i} link {rng.randint(0, 17)}"
        return f"NVSwitch {rng.randint(0, 3)} heartbeat ok, links active 18/18"
    if app == "nvidia-device-plugin":
        if level == "error":
            return "failed to update device health: context deadline exceeded"
        if level == "warn":
            return "kubelet socket re-registration took longer than expected"
        return rng.choice(
            (
                f"health check passed for {len(node.gpus)} devices",
                f"Allocate request for device {gpu.uuid} pod {gpu.pod or 'none'}",
            )
        )
    if app == "training-job":
        if level == "error":
            return rng.choice(
                (
                    "NCCL WARN NET/IB : Got completion with error 12, opcode 0, len 0, vendor err 129",
                    "RuntimeError: CUDA error: an illegal memory access was encountered",
                )
            )
        if level == "warn":
            return rng.choice(
                (
                    "gradient overflow detected, skipping optimizer step",
                    f"rank {rng.randint(0, 63)} is {rng.uniform(0.5, 4):.1f}s behind the slowest rank",
                )
            )
        node.step += 1
        return (
            f"step={node.step} loss={max(0.2, 3.2 - node.step * 0.0004 + rng.uniform(-0.05, 0.05)):.4f} "
            f"tokens_per_sec={rng.randint(9000, 14000) * len(node.gpus)} grad_norm={rng.uniform(0.2, 1.4):.3f}"
        )
    if app == "inference-server":
        if level == "error":
            return rng.choice(
                ("upstream timeout after 30000ms, status=504", "KV cache allocation failed, evicting sequences")
            )
        if level == "warn":
            return f"request queue depth {rng.randint(65, 220)} exceeds threshold 64"
        return (
            f"POST /v1/chat/completions 200 ttft_ms={rng.randint(40, 380)} "
            f"output_tokens={rng.randint(20, 900)} batch={rng.randint(1, 64)}"
        )
    if app == "batch-job":
        if level == "error":
            return "worker exited with code 137 (OOMKilled)"
        if level == "warn":
            return f"frame retried after transient CUDA failure on GPU {i}"
        return f"frame {rng.randint(1, 4000)}/4000 rendered in {rng.uniform(2, 14):.1f}s"
    return f"{app}: {level}"


def apps_for(node) -> dict[str, tuple[str, ...]]:
    """Source key -> app labels that exist on this node."""
    sources = {k: v for k, v in SOURCES.items() if k != "workload"}
    if node.workload != "idle":
        sources["workload"] = (WORKLOADS[node.workload]["app"],)
    if not node.gpus[0].model.nvlink_gbs:
        sources.pop("nvidia-fabricmanager")
    return sources


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class LogShipper:
    """Batches records and POSTs them to VictoriaLogs as JSON lines.

    Delivery is best effort: when VictoriaLogs is unreachable the oldest records
    are dropped once the queue is full, so the simulator never blocks or grows.
    """

    def __init__(self, url: str, max_queue: int = 50000, batch_size: int = 2000, interval: float = 1.0):
        self.url = url.rstrip("/") if url else ""
        self.queue: deque = deque(maxlen=max_queue)
        self.batch_size = batch_size
        self.interval = interval
        self.shipped = 0
        self.errors = 0
        self.dropped = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="log-shipper", daemon=True)

    def start(self) -> None:
        if self.url:
            self._thread.start()
        else:
            log.warning("VICTORIALOGS_URL is empty, logs are only kept in the in-memory tail")

    def stop(self) -> None:
        self._stop.set()

    def push(self, record: dict) -> None:
        if not self.url:
            return
        with self._lock:
            if len(self.queue) == self.queue.maxlen:
                self.dropped += 1
            self.queue.append(record)

    def queue_length(self) -> int:
        return len(self.queue)

    def _take(self) -> list[dict]:
        with self._lock:
            n = min(len(self.queue), self.batch_size)
            return [self.queue.popleft() for _ in range(n)]

    def _requeue(self, batch: list[dict]) -> None:
        with self._lock:
            for record in reversed(batch):
                if len(self.queue) == self.queue.maxlen:
                    self.dropped += 1
                    break
                self.queue.appendleft(record)

    def flush(self) -> bool:
        batch = self._take()
        if not batch:
            return True
        body = "\n".join(json.dumps(r, separators=(",", ":")) for r in batch).encode()
        req = urllib.request.Request(
            f"{self.url}/insert/jsonline?_stream_fields={STREAM_FIELDS}&_msg_field=_msg&_time_field=_time",
            data=body,
            headers={"Content-Type": "application/stream+json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            self.shipped += len(batch)
            self.last_error = ""
            return True
        except (urllib.error.URLError, OSError) as exc:
            self.errors += 1
            self.last_error = str(exc)
            self._requeue(batch)
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            ok = self.flush()
            # Drain a backlog quickly, but back off while the sink is down.
            if ok and self.queue_length() >= self.batch_size:
                continue
            self._stop.wait(self.interval if ok else min(10.0, self.interval * 5))
