"""The simulated GPU fleet: workload model, physics, fault injection and logs."""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from . import logs as logmod
from .topology import (
    NODES_PER_RACK,
    REGION_SPECS,
    REGIONS_BY_NAME,
    WORKLOADS,
    Gpu,
    Node,
    build_nodes,
    dynamic_field_names,
)

MAX_GPUS = 4096
MAX_BURST = 5000
MAX_DURATION_S = 24 * 3600

# kind -> (label, description, parameters the UI should offer)
FAULT_KINDS: dict[str, dict] = {
    "thermal": {
        "label": "Cooling failure",
        "description": "Raises ambient temperature so GPUs heat up and throttle.",
        "params": [],
    },
    "power_cap": {
        "label": "Power cap",
        "description": "Limits board power, causing power violations and lower clocks.",
        "params": [],
    },
    "xid": {
        "label": "Xid errors",
        "description": "Emits NVIDIA Xid errors in the kernel log and sets DCGM_FI_DEV_XID_ERRORS.",
        "params": [{"name": "xid", "label": "Xid code (blank = random)", "type": "select", "options": list(logmod.XID_CODES)}],
    },
    "ecc": {
        "label": "ECC errors",
        "description": "Single-bit errors, and double-bit errors with row remapping at high intensity.",
        "params": [],
    },
    "nvlink": {
        "label": "NVLink errors",
        "description": "CRC, replay and recovery errors and lower NVLink bandwidth.",
        "params": [],
    },
    "pcie": {
        "label": "PCIe degradation",
        "description": "PCIe replays and a link that retrains at a lower generation or width.",
        "params": [],
    },
    "mem_leak": {
        "label": "Memory leak",
        "description": "Framebuffer fills up until the workload logs CUDA out-of-memory errors.",
        "params": [],
    },
    "load": {
        "label": "Force utilization",
        "description": "Pins GPU utilization to a fixed value.",
        "params": [{"name": "value", "label": "Utilization %", "type": "number", "min": 0, "max": 100, "default": 100}],
    },
    "gpu_lost": {
        "label": "GPU fell off the bus",
        "description": "Xid 79: the GPU disappears from metrics and the device plugin marks it unhealthy.",
        "params": [],
    },
    "node_down": {
        "label": "Node down",
        "description": "The node stops exporting metrics and logs.",
        "params": [],
    },
}

SCOPE_KEYS = ("region", "datacenter", "node", "gpu")

PRESETS = {
    "healthy": "Clear all faults and restore default settings.",
    "heatwave": "Cooling failure in eu-west-1 / dub1 for 30 minutes.",
    "training-surge": "Global load at 120% and us-east-1 pinned near 99% for 20 minutes.",
    "xid-storm": "Xid and ECC errors on one ap-northeast-1 node for 15 minutes.",
    "region-outage": "us-west-2 goes dark: no metrics and no logs.",
    "gpu-lost": "One GPU in eu-central-1 falls off the bus.",
    "chaos": "A random mix of faults across regions for 15 minutes.",
}

# Per-workload behaviour. tensor/occ/dram/fp32/fp64 are fractions of SM activity,
# mem is the fraction of framebuffer in use, nv the NVLink load, pcie bytes/s at full load.
PROFILES = {
    "training": dict(tensor=0.62, occ=0.5, dram=0.55, fp32=0.08, fp64=0.0, mem=0.88, nv=0.35, pcie=1.5e9),
    "inference": dict(tensor=0.38, occ=0.4, dram=0.7, fp32=0.05, fp64=0.0, mem=0.74, nv=0.08, pcie=0.8e9),
    "batch": dict(tensor=0.0, occ=0.35, dram=0.4, fp32=0.55, fp64=0.04, mem=0.55, nv=0.0, pcie=2.5e9),
    "idle": dict(tensor=0.0, occ=0.05, dram=0.02, fp32=0.0, fp64=0.0, mem=0.01, nv=0.0, pcie=1e6),
}

# DCGM_FI_DEV_CLOCK_THROTTLE_REASONS bits
IDLE_UTIL = 0.05
THROTTLE_IDLE = 0x1
THROTTLE_SW_POWER_CAP = 0x4
THROTTLE_SW_THERMAL = 0x20
THROTTLE_HW_THERMAL = 0x40

_GPU_DYNAMIC = dynamic_field_names()


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _number(name: str, value, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return float(value)


def _boolean(name: str, value) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


@dataclass
class Settings:
    paused: bool = False
    intensity: float = 1.0
    noise: float = 0.5
    logs_enabled: bool = True
    log_rate: float = 0.2
    warn_ratio: float = 0.05
    error_ratio: float = 0.01
    sources: dict = field(default_factory=lambda: {k: True for k in logmod.SOURCES})

    def update(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        unknown = set(data) - set(self.as_dict())
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
        # Validate everything before applying anything.
        new: dict = {}
        for key in ("paused", "logs_enabled"):
            if key in data:
                new[key] = _boolean(key, data[key])
        if "intensity" in data:
            new["intensity"] = _number("intensity", data["intensity"], 0, 1.5)
        if "noise" in data:
            new["noise"] = _number("noise", data["noise"], 0, 1)
        if "log_rate" in data:
            new["log_rate"] = _number("log_rate", data["log_rate"], 0, 20)
        for key in ("warn_ratio", "error_ratio"):
            if key in data:
                new[key] = _number(key, data[key], 0, 1)
        if "sources" in data:
            src = data["sources"]
            if not isinstance(src, dict) or set(src) - set(logmod.SOURCES):
                raise ValueError(f"sources must be an object with keys from {', '.join(logmod.SOURCES)}")
            new["sources"] = {**self.sources, **{k: _boolean(f"sources.{k}", v) for k, v in src.items()}}
        if new.get("warn_ratio", self.warn_ratio) + new.get("error_ratio", self.error_ratio) > 1:
            raise ValueError("warn_ratio + error_ratio must not exceed 1")
        for key, value in new.items():
            setattr(self, key, value)

    def as_dict(self) -> dict:
        return {
            "paused": self.paused,
            "intensity": self.intensity,
            "noise": self.noise,
            "logs_enabled": self.logs_enabled,
            "log_rate": self.log_rate,
            "warn_ratio": self.warn_ratio,
            "error_ratio": self.error_ratio,
            "sources": dict(self.sources),
        }


@dataclass
class RegionState:
    enabled: bool = True
    load: float = 1.0
    ambient_offset: float = 0.0

    def update(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        unknown = set(data) - {"enabled", "load", "ambient_offset"}
        if unknown:
            raise ValueError(f"unknown region fields: {', '.join(sorted(unknown))}")
        new = {}
        if "enabled" in data:
            new["enabled"] = _boolean("enabled", data["enabled"])
        if "load" in data:
            new["load"] = _number("load", data["load"], 0, 2)
        if "ambient_offset" in data:
            new["ambient_offset"] = _number("ambient_offset", data["ambient_offset"], -10, 40)
        for key, value in new.items():
            setattr(self, key, value)


@dataclass
class Fault:
    id: str
    kind: str
    scope: dict
    start: float
    end: float | None
    intensity: float
    params: dict


def _matches(gpu: Gpu, scope: dict) -> bool:
    for key, value in scope.items():
        if key == "gpu":
            if gpu.index != value:
                return False
        elif getattr(gpu, key) != value:
            return False
    return True


class Fleet:
    def __init__(
        self,
        nodes_per_dc: int = 4,
        gpus_per_node: int = 8,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = 5.0,
        log_sink: Callable[[dict], None] | None = None,
    ):
        self.lock = threading.RLock()
        self.rng = rng or random.Random()
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.log_sink = log_sink or (lambda record: None)
        self.settings = Settings()
        self.regions = {r.name: RegionState() for r in REGION_SPECS}
        self.faults: dict[str, Fault] = {}
        self._fault_seq = 0
        self.tail: deque = deque(maxlen=1000)
        self.stats = {"ticks": 0, "logs": {lvl: 0 for lvl in logmod.LEVELS}, "last_tick": 0.0}
        self.nodes_per_dc = 0
        self.gpus_per_node = 0
        self.nodes: list[Node] = []
        self._build(nodes_per_dc, gpus_per_node)

    # ------------------------------------------------------------------ topology

    def _build(self, nodes_per_dc: int, gpus_per_node: int) -> None:
        old = {g.key: g for n in self.nodes for g in n.gpus}
        old_nodes = {n.name: n for n in self.nodes}
        nodes = build_nodes(nodes_per_dc, gpus_per_node)
        fresh: list[Node] = []
        for node in nodes:
            previous = old_nodes.get(node.name)
            if previous:
                node.down, node.step = previous.down, previous.step
            node_is_new = False
            for gpu in node.gpus:
                prev_gpu = old.get(gpu.key)
                if prev_gpu:
                    for name in _GPU_DYNAMIC:
                        setattr(gpu, name, getattr(prev_gpu, name))
                else:
                    self._init_gpu(gpu, node)
                    node_is_new = True
            if node_is_new:
                fresh.append(node)
        self.nodes = nodes
        self.nodes_per_dc, self.gpus_per_node = nodes_per_dc, gpus_per_node
        self._drop_dead_faults()
        self._warm_up(fresh)

    def _init_gpu(self, gpu: Gpu, node: Node) -> None:
        spec = REGIONS_BY_NAME[gpu.region]
        gpu.gpu_temp = spec.ambient_c + 10
        gpu.memory_temp = gpu.gpu_temp + 6
        gpu.power = gpu.model.idle_w
        gpu.power_limit = gpu.model.tdp_w
        gpu.mem_clock = gpu.model.mem_clock
        gpu.link_gen = gpu.model.pcie_gen
        gpu.link_width = 16
        # Pretend the board has been up for a while, like a real counter would be.
        gpu.energy = self.rng.uniform(5e9, 2e11)
        gpu.pcie_tx = self.rng.uniform(1e12, 1e14)
        gpu.pcie_rx = self.rng.uniform(1e12, 1e14)
        gpu.nvlink_bw = self.rng.uniform(1e13, 1e15) if gpu.model.nvlink_gbs else 0.0
        gpu.fb_used = gpu.fb_total * 0.01
        gpu.fb_free = gpu.fb_total - gpu.fb_used

    def _warm_up(self, nodes: list[Node]) -> None:
        """Let new GPUs settle to their steady state without emitting logs."""
        now = self.clock()
        for node in nodes:
            spec = REGIONS_BY_NAME[node.region]
            for step in range(30):
                for gpu in node.gpus:
                    self._step_gpu(gpu, node, spec, self.regions[node.region], now + step * 5, 5.0, emit=False)

    def resize(self, nodes_per_dc: int, gpus_per_node: int) -> None:
        if isinstance(nodes_per_dc, bool) or isinstance(gpus_per_node, bool):
            raise ValueError("nodes_per_dc and gpus_per_node must be integers")
        if not isinstance(nodes_per_dc, int) or not 1 <= nodes_per_dc <= 64:
            raise ValueError("nodes_per_dc must be an integer between 1 and 64")
        if not isinstance(gpus_per_node, int) or not 1 <= gpus_per_node <= 16:
            raise ValueError("gpus_per_node must be an integer between 1 and 16")
        total = len(REGION_SPECS) * 2 * nodes_per_dc * gpus_per_node
        if total > MAX_GPUS:
            raise ValueError(f"fleet would have {total} GPUs, the limit is {MAX_GPUS}")
        with self.lock:
            self._build(nodes_per_dc, gpus_per_node)

    def topology(self) -> list[dict]:
        with self.lock:
            tree = []
            for spec in REGION_SPECS:
                dcs = []
                for dc in spec.datacenters:
                    names = [n.name for n in self.nodes if n.datacenter == dc]
                    dcs.append({"name": dc, "nodes": names})
                tree.append(
                    {
                        "name": spec.name,
                        "city": spec.city,
                        "gpu_model": spec.gpu_model,
                        "datacenters": dcs,
                    }
                )
            return tree

    def reset(self) -> None:
        """Back to a healthy, freshly booted fleet with default settings."""
        with self.lock:
            self.settings = Settings()
            self.regions = {r.name: RegionState() for r in REGION_SPECS}
            self.faults.clear()
            self.nodes = []
            self._build(self.nodes_per_dc, self.gpus_per_node)

    # -------------------------------------------------------------------- faults

    def add_fault(
        self,
        kind: str,
        scope: dict | None = None,
        duration_s: float = 0,
        intensity: float = 0.7,
        params: dict | None = None,
        now: float | None = None,
    ) -> Fault:
        if kind not in FAULT_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(FAULT_KINDS)}")
        scope = scope or {}
        if not isinstance(scope, dict) or set(scope) - set(SCOPE_KEYS):
            raise ValueError(f"scope keys must be from: {', '.join(SCOPE_KEYS)}")
        scope = {k: v for k, v in scope.items() if v not in (None, "")}
        if "gpu" in scope:
            try:
                scope["gpu"] = int(scope["gpu"])
            except (TypeError, ValueError):
                raise ValueError("scope.gpu must be an integer") from None
            if "node" not in scope:
                raise ValueError("scope.gpu needs scope.node")
        intensity = _number("intensity", intensity, 0, 1)
        duration_s = _number("duration_s", duration_s, 0, MAX_DURATION_S)
        params = self._check_params(kind, params or {})
        with self.lock:
            if not any(_matches(g, scope) for n in self.nodes for g in n.gpus):
                raise ValueError("scope matches no GPU")
            now = self.clock() if now is None else now
            self._fault_seq += 1
            fault = Fault(
                id=f"f{self._fault_seq}",
                kind=kind,
                scope=scope,
                start=now,
                end=now + duration_s if duration_s else None,
                intensity=intensity,
                params=params,
            )
            self.faults[fault.id] = fault
            return fault

    @staticmethod
    def _check_params(kind: str, params: dict) -> dict:
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        out: dict = {}
        if kind == "xid" and params.get("xid") not in (None, ""):
            try:
                code = int(params["xid"])
            except (TypeError, ValueError):
                raise ValueError("params.xid must be an integer") from None
            if code not in logmod.XID:
                raise ValueError(f"params.xid must be one of: {', '.join(map(str, logmod.XID_CODES))}")
            out["xid"] = code
        if kind == "load":
            out["value"] = _number("params.value", params.get("value", 100), 0, 100)
        return out

    def remove_fault(self, fault_id: str) -> bool:
        with self.lock:
            return self.faults.pop(fault_id, None) is not None

    def clear_faults(self) -> int:
        with self.lock:
            n = len(self.faults)
            self.faults.clear()
            return n

    def _drop_dead_faults(self) -> None:
        for fid, fault in list(self.faults.items()):
            if not any(_matches(g, fault.scope) for n in self.nodes for g in n.gpus):
                del self.faults[fid]

    def _expire_faults(self, now: float) -> None:
        for fid, fault in list(self.faults.items()):
            if fault.end is not None and fault.end <= now:
                del self.faults[fid]

    def _apply_faults(self) -> None:
        for node in self.nodes:
            for gpu in node.gpus:
                gpu.fx = {}
        for fault in self.faults.values():
            for node in self.nodes:
                for gpu in node.gpus:
                    if _matches(gpu, fault.scope):
                        current = gpu.fx.get(fault.kind)
                        if current is None or fault.intensity > current.intensity:
                            gpu.fx[fault.kind] = fault
        for node in self.nodes:
            node.down = all("node_down" in g.fx for g in node.gpus)
            enabled = self.regions[node.region].enabled
            for gpu in node.gpus:
                gpu.visible = enabled and "node_down" not in gpu.fx and "gpu_lost" not in gpu.fx

    # ---------------------------------------------------------------------- tick

    def tick(self, now: float | None = None, dt: float | None = None) -> None:
        with self.lock:
            now = self.clock() if now is None else now
            dt = self.tick_seconds if dt is None else dt
            self._expire_faults(now)
            if self.settings.paused:
                return
            self._apply_faults()
            for node in self.nodes:
                region = self.regions[node.region]
                spec = REGIONS_BY_NAME[node.region]
                if not region.enabled or node.down:
                    continue
                for gpu in node.gpus:
                    self._lost_events(gpu, node, now)
                    if "gpu_lost" in gpu.fx:
                        continue
                    self._step_gpu(gpu, node, spec, region, now, dt, emit=True)
                if self.settings.logs_enabled:
                    self._background_logs(node, now, dt)
            self.stats["ticks"] += 1
            self.stats["last_tick"] = now

    def _workload_target(self, node: Node, spec, now: float) -> float:
        kind = node.workload
        hour = (now / 3600 + spec.utc_offset) % 24
        diurnal = 0.8 + 0.2 * math.cos(2 * math.pi * (hour - 15) / 24)
        if kind == "training":
            return 35.0 if (now + node.phase) % 600 < 20 else 96.0
        if kind == "inference":
            return 25 + 55 * diurnal + 8 * math.sin(now / 37 + node.phase)
        if kind == "batch":
            return 88.0 if (now + node.phase) % 720 < 480 else 6.0
        return 2.0

    def _step_gpu(self, gpu: Gpu, node: Node, spec, region: RegionState, now: float, dt: float, emit: bool) -> None:
        fx, model, s, rng = gpu.fx, gpu.model, self.settings, self.rng
        prof = PROFILES[node.workload]

        # Utilization follows the workload target, then the activity metrics derive from it.
        target = self._workload_target(node, spec, now) * region.load * s.intensity
        if "load" in fx:
            target = fx["load"].params.get("value", 100.0)
        target = _clamp(target, 0, 100)
        # A quiet GPU reads a steady few percent; only a busy one jitters noticeably.
        jitter = 2.5 * s.noise * min(1.0, target / 30)
        gpu.util = _clamp(gpu.util + (target - gpu.util) * 0.6 + rng.gauss(0, jitter), 0, 100)
        u = gpu.util / 100
        gpu.gr_active = u
        gpu.sm_active = u * 0.93
        gpu.sm_occupancy = gpu.sm_active * prof["occ"]
        gpu.tensor_active = gpu.sm_active * prof["tensor"]
        gpu.fp16_active = gpu.tensor_active * 0.25
        gpu.fp32_active = gpu.sm_active * prof["fp32"]
        gpu.fp64_active = gpu.sm_active * prof["fp64"]
        gpu.dram_active = _clamp(u * prof["dram"] + rng.gauss(0, 0.01 * s.noise), 0, 1)
        gpu.mem_copy_util = _clamp(gpu.dram_active * 80, 0, 100)
        gpu.enc_util = u * 20 if node.workload == "batch" else 0.0
        gpu.dec_util = u * 8 if node.workload == "batch" else 0.0

        # Framebuffer
        frac = prof["mem"] if (u >= IDLE_UTIL or node.workload == "inference") else 0.01
        used = gpu.fb_total * frac * (1 + rng.uniform(-0.004, 0.004))
        if "mem_leak" in fx:
            leak = fx["mem_leak"]
            ramp = max(60.0, (leak.end - leak.start) if leak.end else 600.0)
            fill = _clamp((now - leak.start) / ramp, 0, 1) * (0.5 + 0.5 * leak.intensity)
            used += (gpu.fb_total - used) * fill
        gpu.fb_used = _clamp(used, 0, gpu.fb_total)
        gpu.fb_free = gpu.fb_total - gpu.fb_used

        # Throttling is decided from the previous temperature, which is what gives
        # thermal and power feedback loops their realistic lag.
        reasons = 0
        scale = 1.0
        over = gpu.gpu_temp - model.slowdown_temp
        if over > 0:
            scale = max(0.45, 1 - 0.05 * over)
            reasons |= THROTTLE_HW_THERMAL if over > 3 else THROTTLE_SW_THERMAL
            gpu.thermal_violation += dt * 1e6 * min(1.0, 0.3 + over / 10)
        raw = model.idle_w + (model.tdp_w - model.idle_w) * (0.9 * gpu.sm_active**1.15 + 0.1 * gpu.dram_active)
        power = model.idle_w + (raw - model.idle_w) * scale**2
        limit = float(model.tdp_w)
        if "power_cap" in fx:
            limit = model.tdp_w * (1 - 0.5 * fx["power_cap"].intensity)
        gpu.power_limit = limit
        if power > limit:
            ratio = (limit - model.idle_w) / max(1.0, power - model.idle_w)
            scale = min(scale, max(0.4, ratio**0.5))
            power = limit
            reasons |= THROTTLE_SW_POWER_CAP
            gpu.power_violation += dt * 1e6 * 0.8
        power *= 1 + rng.gauss(0, 0.004 + 0.012 * s.noise)
        gpu.power = max(model.idle_w * 0.9, power)
        gpu.energy += gpu.power * dt * 1000

        idle = u < IDLE_UTIL
        if idle:
            reasons |= THROTTLE_IDLE
        gpu.throttle_reasons = reasons
        gpu.sm_clock = 345.0 if idle else model.max_sm_clock * scale * (1 - 0.01 * rng.random())
        gpu.mem_clock = 405.0 if idle else float(model.mem_clock)

        ambient = spec.ambient_c + region.ambient_offset
        if "thermal" in fx:
            ambient += 10 + 25 * fx["thermal"].intensity
        power_frac = gpu.power / model.tdp_w
        temp_target = ambient + 10 + power_frac * 38
        gpu.gpu_temp += (temp_target - gpu.gpu_temp) * min(1.0, 0.3 * dt / 5) + rng.gauss(0, 0.15 * (0.5 + s.noise))
        gpu.gpu_temp = _clamp(gpu.gpu_temp, ambient, model.slowdown_temp + 14)
        gpu.memory_temp = gpu.gpu_temp + 6 + 6 * power_frac

        # Interconnect traffic
        gpu.pcie_tx += u * prof["pcie"] * dt * rng.uniform(0.8, 1.2)
        gpu.pcie_rx += u * prof["pcie"] * dt * rng.uniform(0.8, 1.2)
        if model.nvlink_gbs:
            rate = model.nvlink_gbs * 1e9 * u * prof["nv"]
            if "nvlink" in fx:
                rate *= 1 - 0.8 * fx["nvlink"].intensity
            gpu.nvlink_bw += rate * dt

        gpu.link_gen, gpu.link_width = float(model.pcie_gen), 16.0
        if "pcie" in fx:
            pcie_i = fx["pcie"].intensity
            if pcie_i > 0.3:
                gpu.link_gen = float(model.pcie_gen - 1)
            if pcie_i > 0.6:
                gpu.link_width = 8.0
            if pcie_i > 0.9:
                gpu.link_width = 4.0

        if now >= gpu.xid_until:
            gpu.xid = 0.0

        if emit:
            self._fault_events(gpu, node, now, dt)
            self._state_logs(gpu, node, now)

    # ---------------------------------------------------------------------- logs

    def _emit(self, node: Node, gpu: Gpu | None, app: str, level: str, msg: str, ts: float, **extra) -> None:
        rec = {"_time": logmod.iso(ts), "_msg": msg, "level": level, "app": app, **node.log_labels()}
        if gpu is not None:
            rec.update(gpu=str(gpu.index), UUID=gpu.uuid, modelName=gpu.model.model_name)
            if gpu.pod:
                rec.update(namespace=gpu.namespace, pod=gpu.pod, container=gpu.container)
        rec.update(extra)
        self.stats["logs"][level] += 1
        self.tail.append(rec)
        self.log_sink(rec)

    def _source_enabled(self, app: str) -> bool:
        for key, apps in logmod.SOURCES.items():
            if app in apps:
                return self.settings.sources.get(key, True)
        return True

    def _emit_gated(self, node, gpu, app, level, msg, ts, **extra) -> None:
        if self.settings.logs_enabled and self._source_enabled(app):
            self._emit(node, gpu, app, level, msg, ts, **extra)

    def _xid_event(self, gpu: Gpu, node: Node, code: int, now: float) -> None:
        msg, level = logmod.xid_message(gpu, code, self.rng)
        gpu.xid, gpu.xid_until = float(code), now + 300
        self._emit_gated(node, gpu, "kernel", level, msg, now, xid=str(code))
        if code == 63:
            gpu.remap_corr += 1
        elif code == 64:
            gpu.remap_failure = 1.0
        elif code == 74:
            gpu.nvlink_recovery += 1
        if self.rng.random() < 0.5:
            self._emit_gated(
                node, gpu, "nvidia-device-plugin", "warn",
                f"device {gpu.uuid} reported Xid {code}, health check degraded", now, xid=str(code),
            )
        if node.workload != "idle" and self.rng.random() < 0.5:
            app = WORKLOADS[node.workload]["app"]
            self._emit_gated(
                node, gpu, app, "error", "CUDA error: unspecified launch failure" if code != 48
                else "RuntimeError: CUDA error: uncorrectable ECC error encountered", now, xid=str(code),
            )

    def _lost_events(self, gpu: Gpu, node: Node, now: float) -> None:
        lost = "gpu_lost" in gpu.fx
        if lost and not gpu.lost_logged:
            gpu.lost_logged = True
            self._xid_event(gpu, node, 79, now)
            self._emit_gated(
                node, gpu, "nvidia-device-plugin", "error",
                f"device {gpu.uuid} marked unhealthy, removing from allocatable resources", now, xid="79",
            )
        elif not lost and gpu.lost_logged:
            gpu.lost_logged = False
            self._emit_gated(
                node, gpu, "nvidia-device-plugin", "info", f"device {gpu.uuid} is healthy again, re-registered", now
            )

    def _chance(self, per_second: float, dt: float) -> bool:
        return self.rng.random() < min(1.0, per_second * dt)

    def _fault_events(self, gpu: Gpu, node: Node, now: float, dt: float) -> None:
        fx, rng = gpu.fx, self.rng
        if "xid" in fx:
            fault = fx["xid"]
            if self._chance(fault.intensity * 0.2, dt):
                self._xid_event(gpu, node, fault.params.get("xid") or rng.choice(logmod.XID_CODES), now)
        if "ecc" in fx:
            intensity = fx["ecc"].intensity
            if rng.random() < 0.6:
                n = 1 + int(intensity * 20 * rng.random())
                gpu.ecc_sbe_vol += n
                gpu.ecc_sbe_agg += n
                self._emit_gated(
                    node, gpu, "dcgm-exporter", "warn",
                    f"ECC single-bit errors on GPU {gpu.index}: +{n} (volatile total {int(gpu.ecc_sbe_vol)})", now,
                )
            if intensity > 0.5 and self._chance(0.016 * intensity, dt):
                gpu.ecc_dbe_vol += 1
                gpu.ecc_dbe_agg += 1
                gpu.remap_uncorr += 1
                self._xid_event(gpu, node, 48, now)
        if "nvlink" in fx and gpu.model.nvlink_gbs:
            intensity = fx["nvlink"].intensity
            if rng.random() < 0.7:
                gpu.nvlink_crc_flit += rng.randint(1, 1 + int(500 * intensity))
                gpu.nvlink_crc_data += rng.randint(0, int(50 * intensity))
                gpu.nvlink_replay += rng.randint(0, int(100 * intensity))
            if self._chance(0.1 * intensity, dt):
                self._xid_event(gpu, node, 74, now)
                self._emit_gated(
                    node, gpu, "nvidia-fabricmanager", "error",
                    f"NVLink {rng.randint(0, 17)} on GPU {gpu.index} exceeded the CRC error threshold, bandwidth degraded",
                    now,
                )
        if "pcie" in fx:
            intensity = fx["pcie"].intensity
            gpu.pcie_replay += rng.randint(1, 1 + int(80 * intensity))
            if self._chance(0.15 * intensity, dt):
                bdf = gpu.pci_bus_id[5:].lower()
                self._emit_gated(
                    node, gpu, "kernel", "warn",
                    f"nvidia {bdf}: PCIe Bus Error: severity=Corrected, type=Physical Layer, (Receiver ID)", now,
                )

    def _state_logs(self, gpu: Gpu, node: Node, now: float) -> None:
        """Logs that follow from the GPU state, whether or not a fault caused it."""
        if gpu.gpu_temp > gpu.slowdown_temp and now - gpu.last_thermal_log > 30:
            gpu.last_thermal_log = now
            critical = gpu.gpu_temp > gpu.slowdown_temp + 8
            self._emit_gated(
                node, gpu, "dcgm-exporter", "error" if critical else "warn",
                f"GPU {gpu.index} temperature {gpu.gpu_temp:.0f}C exceeds slowdown threshold {gpu.slowdown_temp}C, "
                f"SM clock throttled to {gpu.sm_clock:.0f} MHz", now,
            )
        if gpu.throttle_reasons & THROTTLE_SW_POWER_CAP and now - gpu.last_power_log > 60:
            gpu.last_power_log = now
            self._emit_gated(
                node, gpu, "dcgm-exporter", "warn",
                f"GPU {gpu.index} power capped at {gpu.power:.0f} W (limit {gpu.power_limit:.0f} W), "
                f"SM clock {gpu.sm_clock:.0f} MHz", now,
            )
        if gpu.fb_used / gpu.fb_total > 0.97 and node.workload != "idle" and now - gpu.last_oom_log > 30:
            gpu.last_oom_log = now
            free_gib = gpu.fb_free / 1024
            self._emit_gated(
                node, gpu, WORKLOADS[node.workload]["app"], "error",
                f"torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB. GPU {gpu.index} has a "
                f"total capacity of {gpu.fb_total / 1024:.2f} GiB of which {free_gib:.2f} GiB is free.", now,
            )

    def _pick_level(self) -> str:
        roll = self.rng.random()
        if roll < self.settings.error_ratio:
            return "error"
        if roll < self.settings.error_ratio + self.settings.warn_ratio:
            return "warn"
        return "info"

    def _background_logs(self, node: Node, now: float, dt: float) -> None:
        rng = self.rng
        expected = self.settings.log_rate * dt
        count = int(expected) + (1 if rng.random() < expected - int(expected) else 0)
        if not count:
            return
        up = [g for g in node.gpus if g.visible]
        sources = {k: v for k, v in logmod.apps_for(node).items() if self.settings.sources.get(k, True)}
        if not up or not sources:
            return
        keys = list(sources)
        weights = [logmod.SOURCE_WEIGHTS[k] for k in keys]
        for _ in range(count):
            app = rng.choice(sources[rng.choices(keys, weights)[0]])
            gpu = rng.choice(up)
            level = self._pick_level()
            msg = logmod.background_message(app, level, node, gpu, rng)
            self._emit(node, gpu, app, level, msg, now - rng.uniform(0, dt))

    def burst(
        self,
        count: int,
        level: str = "error",
        app: str = "",
        scope: dict | None = None,
        message: str = "",
        now: float | None = None,
    ) -> int:
        """Emit `count` log lines at once, e.g. to test an alert or a log panel."""
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_BURST:
            raise ValueError(f"count must be an integer between 1 and {MAX_BURST}")
        if level not in logmod.LEVELS:
            raise ValueError(f"level must be one of: {', '.join(logmod.LEVELS)}")
        known_apps = {a for apps in logmod.SOURCES.values() for a in apps}
        if app and app not in known_apps:
            raise ValueError(f"app must be one of: {', '.join(sorted(known_apps))}")
        if not isinstance(message, str) or len(message) > 500:
            raise ValueError("message must be a string of at most 500 characters")
        scope = {k: v for k, v in (scope or {}).items() if k in SCOPE_KEYS and v not in (None, "")}
        if "gpu" in scope:
            scope["gpu"] = int(scope["gpu"])
        with self.lock:
            now = self.clock() if now is None else now
            gpus = [(n, g) for n in self.nodes for g in n.gpus if _matches(g, scope)]
            if not gpus:
                raise ValueError("scope matches no GPU")
            for _ in range(count):
                node, gpu = self.rng.choice(gpus)
                chosen = app or self.rng.choice(
                    [a for apps in logmod.apps_for(node).values() for a in apps]
                )
                text = message or logmod.background_message(chosen, level, node, gpu, self.rng)
                self._emit(node, gpu, chosen, level, text, now)
            return count

    def recent_logs(self, limit: int = 100, region: str = "", level: str = "") -> list[dict]:
        with self.lock:
            out = []
            for rec in reversed(self.tail):
                if region and rec["region"] != region:
                    continue
                if level and rec["level"] != level:
                    continue
                out.append(rec)
                if len(out) >= limit:
                    break
            return out

    # ------------------------------------------------------------------- summary

    def summary(self) -> dict:
        with self.lock:
            now = self.clock()
            regions = []
            for spec in REGION_SPECS:
                state = self.regions[spec.name]
                nodes = [n for n in self.nodes if n.region == spec.name]
                gpus = [g for n in nodes for g in n.gpus]
                up = [g for g in gpus if g.visible]
                regions.append(
                    {
                        "name": spec.name,
                        "city": spec.city,
                        "gpu_model": spec.gpu_model,
                        "enabled": state.enabled,
                        "load": state.load,
                        "ambient_offset": state.ambient_offset,
                        "nodes": len(nodes),
                        "nodes_down": sum(1 for n in nodes if n.down),
                        "gpus": len(gpus),
                        "gpus_up": len(up),
                        "gpus_faulted": sum(1 for g in gpus if g.fx),
                        "avg_util": sum(g.util for g in up) / len(up) if up else 0.0,
                        "avg_temp": sum(g.gpu_temp for g in up) / len(up) if up else 0.0,
                        "max_temp": max((g.gpu_temp for g in up), default=0.0),
                        "power_kw": sum(g.power for g in up) / 1000,
                    }
                )
            faults = [
                {
                    "id": f.id,
                    "kind": f.kind,
                    "label": FAULT_KINDS[f.kind]["label"],
                    "scope": f.scope,
                    "intensity": f.intensity,
                    "params": f.params,
                    "remaining_s": None if f.end is None else max(0, round(f.end - now)),
                }
                for f in self.faults.values()
            ]
            return {
                "settings": self.settings.as_dict(),
                "topology": {
                    "nodes_per_dc": self.nodes_per_dc,
                    "gpus_per_node": self.gpus_per_node,
                    "total_gpus": sum(r["gpus"] for r in regions),
                    "nodes_per_rack": NODES_PER_RACK,
                },
                "regions": regions,
                "faults": faults,
                "stats": {"ticks": self.stats["ticks"], "logs": dict(self.stats["logs"])},
            }

    # ------------------------------------------------------------------- presets

    def apply_preset(self, name: str) -> str:
        if name not in PRESETS:
            raise ValueError(f"preset must be one of: {', '.join(PRESETS)}")
        with self.lock:
            if name == "healthy":
                self.settings = Settings()
                self.regions = {r.name: RegionState() for r in REGION_SPECS}
                self.faults.clear()
            elif name == "heatwave":
                self.add_fault("thermal", {"region": "eu-west-1", "datacenter": "dub1"}, 1800, 0.8)
            elif name == "training-surge":
                self.settings.intensity = 1.2
                self.add_fault("load", {"region": "us-east-1"}, 1200, 1.0, {"value": 99})
            elif name == "xid-storm":
                node = next(n for n in self.nodes if n.region == "ap-northeast-1")
                scope = {"region": node.region, "node": node.name}
                self.add_fault("xid", scope, 900, 0.9)
                self.add_fault("ecc", scope, 900, 0.8)
            elif name == "region-outage":
                self.regions["us-west-2"].enabled = False
            elif name == "gpu-lost":
                node = next(n for n in self.nodes if n.region == "eu-central-1")
                self.add_fault("gpu_lost", {"region": node.region, "node": node.name, "gpu": 3})
            elif name == "chaos":
                for _ in range(5):
                    spec = self.rng.choice(REGION_SPECS)
                    kind = self.rng.choice([k for k in FAULT_KINDS if k not in ("load", "node_down")])
                    scope = {"region": spec.name, "datacenter": self.rng.choice(spec.datacenters)}
                    if kind in ("xid", "ecc", "gpu_lost", "nvlink", "pcie"):
                        node = self.rng.choice([n for n in self.nodes if n.datacenter == scope["datacenter"]])
                        scope["node"] = node.name
                    self.add_fault(kind, scope, 900, round(self.rng.uniform(0.4, 1.0), 2))
            return PRESETS[name]
