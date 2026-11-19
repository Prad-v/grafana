"""Static description of the simulated multi-region GPU fleet.

The label names produced here (region, availability_zone, datacenter, cluster,
rack, node, gpu, UUID, modelName) are shared by metrics and logs, so a log line
can be correlated with the series of the GPU it talks about.
"""

from __future__ import annotations

import random
import uuid
import zlib
from dataclasses import dataclass, field, fields


@dataclass(frozen=True)
class GpuModel:
    key: str
    model_name: str
    fb_total_mib: int
    tdp_w: float
    idle_w: float
    max_sm_clock: int
    mem_clock: int
    slowdown_temp: int
    # Aggregate NVLink bandwidth in GB/s; 0 means the board has no NVLink.
    nvlink_gbs: int
    pcie_gen: int
    driver: str


GPU_MODELS: dict[str, GpuModel] = {
    m.key: m
    for m in (
        GpuModel("H100", "NVIDIA H100 80GB HBM3", 81559, 700, 75, 1980, 2619, 85, 900, 5, "550.90.07"),
        GpuModel("H200", "NVIDIA H200", 143771, 700, 80, 1980, 3201, 85, 900, 5, "550.90.07"),
        GpuModel("A100", "NVIDIA A100-SXM4-80GB", 81920, 400, 55, 1410, 1593, 85, 600, 4, "535.161.08"),
        GpuModel("L40S", "NVIDIA L40S", 46068, 350, 40, 2520, 9001, 90, 0, 4, "550.90.07"),
        GpuModel("B200", "NVIDIA B200", 183359, 1000, 120, 1965, 3996, 87, 1800, 5, "570.133.20"),
    )
}


@dataclass(frozen=True)
class RegionSpec:
    name: str
    city: str
    utc_offset: int
    gpu_model: str
    datacenters: tuple[str, str]
    ambient_c: float


REGION_SPECS: tuple[RegionSpec, ...] = (
    RegionSpec("us-east-1", "N. Virginia", -5, "H100", ("iad1", "iad2"), 24.0),
    RegionSpec("us-west-2", "Oregon", -8, "H200", ("pdx1", "pdx2"), 21.0),
    RegionSpec("eu-west-1", "Ireland", 0, "A100", ("dub1", "dub2"), 20.0),
    RegionSpec("eu-central-1", "Frankfurt", 1, "H100", ("fra1", "fra2"), 22.0),
    RegionSpec("ap-south-1", "Mumbai", 5, "L40S", ("bom1", "bom2"), 29.0),
    RegionSpec("ap-northeast-1", "Tokyo", 9, "B200", ("nrt1", "nrt2"), 25.0),
)
REGIONS_BY_NAME = {r.name: r for r in REGION_SPECS}

NODES_PER_RACK = 4
# PCI bus ids of an HGX baseboard; boards with more GPUs continue after the last.
_PCI_BUSES = (0x18, 0x2A, 0x3A, 0x5D, 0x9A, 0xAB, 0xBA, 0xDB)

WORKLOADS = {
    "training": {"app": "training-job", "namespace": "ml-training", "container": "trainer", "pod": "llm-train"},
    "inference": {"app": "inference-server", "namespace": "inference", "container": "vllm", "pod": "llm-serve"},
    "batch": {"app": "batch-job", "namespace": "batch-jobs", "container": "worker", "pod": "render"},
    "idle": {"app": "", "namespace": "", "container": "", "pod": ""},
}
_WORKLOAD_CHOICES = ("training", "inference", "batch", "idle")
_WORKLOAD_WEIGHTS = (45, 35, 15, 5)


@dataclass
class Gpu:
    """One GPU. Static identity first, then the mutable simulation state."""

    key: str
    index: int
    uuid: str
    pci_bus_id: str
    model: GpuModel
    region: str
    availability_zone: str
    datacenter: str
    cluster: str
    rack: str
    node: str
    workload: str
    namespace: str
    pod: str
    container: str
    labels: dict[str, str] = field(default_factory=dict)
    label_str: str = ""

    # --- dynamic state, advanced by the engine every tick ---
    visible: bool = True
    fx: dict = field(default_factory=dict)
    util: float = 0.0
    mem_copy_util: float = 0.0
    enc_util: float = 0.0
    dec_util: float = 0.0
    fb_used: float = 0.0
    fb_free: float = 0.0
    gpu_temp: float = 30.0
    memory_temp: float = 38.0
    power: float = 0.0
    power_limit: float = 0.0
    energy: float = 0.0
    sm_clock: float = 0.0
    mem_clock: float = 0.0
    throttle_reasons: int = 0
    gr_active: float = 0.0
    sm_active: float = 0.0
    sm_occupancy: float = 0.0
    tensor_active: float = 0.0
    dram_active: float = 0.0
    fp64_active: float = 0.0
    fp32_active: float = 0.0
    fp16_active: float = 0.0
    pcie_tx: float = 0.0
    pcie_rx: float = 0.0
    pcie_replay: float = 0.0
    nvlink_bw: float = 0.0
    nvlink_crc_flit: float = 0.0
    nvlink_crc_data: float = 0.0
    nvlink_replay: float = 0.0
    nvlink_recovery: float = 0.0
    xid: float = 0.0
    xid_until: float = 0.0
    power_violation: float = 0.0
    thermal_violation: float = 0.0
    ecc_sbe_vol: float = 0.0
    ecc_dbe_vol: float = 0.0
    ecc_sbe_agg: float = 0.0
    ecc_dbe_agg: float = 0.0
    retired_sbe: float = 0.0
    retired_dbe: float = 0.0
    retired_pending: float = 0.0
    remap_uncorr: float = 0.0
    remap_corr: float = 0.0
    remap_failure: float = 0.0
    lost_logged: bool = False
    last_thermal_log: float = 0.0
    last_power_log: float = 0.0
    last_oom_log: float = 0.0
    link_gen: float = 0.0
    link_width: float = 16.0

    @property
    def slowdown_temp(self) -> int:
        return self.model.slowdown_temp

    @property
    def fb_total(self) -> float:
        return float(self.model.fb_total_mib)


def dynamic_field_names() -> tuple[str, ...]:
    """Names of the mutable simulation fields, in declaration order."""
    names = [f.name for f in fields(Gpu)]
    return tuple(names[names.index("visible") :])


@dataclass
class Node:
    name: str
    region: str
    availability_zone: str
    datacenter: str
    cluster: str
    rack: str
    workload: str
    pod: str
    phase: float
    gpus: list[Gpu]
    down: bool = False
    step: int = 0

    def log_labels(self) -> dict[str, str]:
        return {
            "region": self.region,
            "availability_zone": self.availability_zone,
            "datacenter": self.datacenter,
            "cluster": self.cluster,
            "rack": self.rack,
            "node": self.name,
        }


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _gpu_labels(gpu: Gpu) -> dict[str, str]:
    labels = {
        "gpu": str(gpu.index),
        "UUID": gpu.uuid,
        "pci_bus_id": gpu.pci_bus_id,
        "device": f"nvidia{gpu.index}",
        "modelName": gpu.model.model_name,
        "Hostname": gpu.node,
        "DCGM_FI_DRIVER_VERSION": gpu.model.driver,
        "region": gpu.region,
        "availability_zone": gpu.availability_zone,
        "datacenter": gpu.datacenter,
        "cluster": gpu.cluster,
        "rack": gpu.rack,
        "node": gpu.node,
        # With honor_labels the scraped instance wins, as it would for a real
        # dcgm-exporter daemonset pod that is scraped per node.
        "instance": f"{gpu.node}:9400",
    }
    if gpu.pod:
        labels["namespace"] = gpu.namespace
        labels["pod"] = gpu.pod
        labels["container"] = gpu.container
    return labels


def build_nodes(nodes_per_dc: int, gpus_per_node: int) -> list[Node]:
    """Build the whole fleet. Identical inputs always give an identical fleet."""
    nodes: list[Node] = []
    for region in REGION_SPECS:
        model = GPU_MODELS[region.gpu_model]
        for dc_index, dc in enumerate(region.datacenters):
            az = f"{region.name}{'ab'[dc_index]}"
            cluster = f"gpu-{dc}"
            for n in range(nodes_per_dc):
                rack = f"rack-{n // NODES_PER_RACK + 1:02d}"
                name = f"gpu-{dc}-r{n // NODES_PER_RACK + 1:02d}-n{n % NODES_PER_RACK + 1:02d}"
                rng = random.Random(zlib.crc32(name.encode()))
                workload = rng.choices(_WORKLOAD_CHOICES, _WORKLOAD_WEIGHTS)[0]
                spec = WORKLOADS[workload]
                pod = f"{spec['pod']}-{rng.getrandbits(20):05x}" if spec["pod"] else ""
                node = Node(
                    name=name,
                    region=region.name,
                    availability_zone=az,
                    datacenter=dc,
                    cluster=cluster,
                    rack=rack,
                    workload=workload,
                    pod=pod,
                    phase=rng.uniform(0, 600),
                    gpus=[],
                )
                for i in range(gpus_per_node):
                    bus = _PCI_BUSES[i] if i < len(_PCI_BUSES) else _PCI_BUSES[-1] + 0x10 * (i - len(_PCI_BUSES) + 1)
                    gpu_pod = pod if workload != "batch" else f"{pod}-{i}"
                    gpu = Gpu(
                        key=f"{name}/{i}",
                        index=i,
                        uuid="GPU-" + str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{name}/{i}")),
                        pci_bus_id=f"00000000:{bus & 0xFF:02X}:00.0",
                        model=model,
                        region=region.name,
                        availability_zone=az,
                        datacenter=dc,
                        cluster=cluster,
                        rack=rack,
                        node=name,
                        workload=workload,
                        namespace=spec["namespace"],
                        pod=gpu_pod,
                        container=spec["container"],
                    )
                    gpu.labels = _gpu_labels(gpu)
                    gpu.label_str = ",".join(f'{k}="{_escape(v)}"' for k, v in gpu.labels.items())
                    node.gpus.append(gpu)
                nodes.append(node)
    return nodes
