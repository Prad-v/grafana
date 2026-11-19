"""Prometheus text exposition of the simulated DCGM metrics."""

from __future__ import annotations

from .engine import Fleet

# (metric name, type, help, Gpu attribute, needs NVLink)
DCGM_METRICS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("DCGM_FI_DEV_SM_CLOCK", "gauge", "SM clock frequency (in MHz).", "sm_clock", False),
    ("DCGM_FI_DEV_MEM_CLOCK", "gauge", "Memory clock frequency (in MHz).", "mem_clock", False),
    ("DCGM_FI_DEV_MEMORY_TEMP", "gauge", "Memory temperature (in C).", "memory_temp", False),
    ("DCGM_FI_DEV_GPU_TEMP", "gauge", "GPU temperature (in C).", "gpu_temp", False),
    ("DCGM_FI_DEV_SLOWDOWN_TEMP", "gauge", "Slowdown temperature (in C).", "slowdown_temp", False),
    ("DCGM_FI_DEV_POWER_USAGE", "gauge", "Power draw (in W).", "power", False),
    ("DCGM_FI_DEV_ENFORCED_POWER_LIMIT", "gauge", "Enforced power limit (in W).", "power_limit", False),
    ("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", "counter", "Total energy consumption since boot (in mJ).", "energy", False),
    ("DCGM_FI_DEV_PCIE_REPLAY_COUNTER", "counter", "Total number of PCIe retries.", "pcie_replay", False),
    ("DCGM_FI_DEV_PCIE_LINK_GEN", "gauge", "PCIe link generation.", "link_gen", False),
    ("DCGM_FI_DEV_PCIE_LINK_WIDTH", "gauge", "PCIe link width.", "link_width", False),
    ("DCGM_FI_DEV_GPU_UTIL", "gauge", "GPU utilization (in %).", "util", False),
    ("DCGM_FI_DEV_MEM_COPY_UTIL", "gauge", "Memory utilization (in %).", "mem_copy_util", False),
    ("DCGM_FI_DEV_ENC_UTIL", "gauge", "Encoder utilization (in %).", "enc_util", False),
    ("DCGM_FI_DEV_DEC_UTIL", "gauge", "Decoder utilization (in %).", "dec_util", False),
    ("DCGM_FI_DEV_XID_ERRORS", "gauge", "Value of the last XID error encountered.", "xid", False),
    ("DCGM_FI_DEV_FB_FREE", "gauge", "Framebuffer memory free (in MiB).", "fb_free", False),
    ("DCGM_FI_DEV_FB_USED", "gauge", "Framebuffer memory used (in MiB).", "fb_used", False),
    ("DCGM_FI_DEV_FB_TOTAL", "gauge", "Framebuffer memory total (in MiB).", "fb_total", False),
    ("DCGM_FI_DEV_CLOCK_THROTTLE_REASONS", "gauge", "Current clock throttle reasons (bitmask).", "throttle_reasons", False),
    ("DCGM_FI_DEV_POWER_VIOLATION", "counter", "Throttling duration due to power constraints (in us).", "power_violation", False),
    ("DCGM_FI_DEV_THERMAL_VIOLATION", "counter", "Throttling duration due to thermal constraints (in us).", "thermal_violation", False),
    ("DCGM_FI_DEV_ECC_SBE_VOL_TOTAL", "counter", "Total number of single-bit volatile ECC errors.", "ecc_sbe_vol", False),
    ("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "counter", "Total number of double-bit volatile ECC errors.", "ecc_dbe_vol", False),
    ("DCGM_FI_DEV_ECC_SBE_AGG_TOTAL", "counter", "Total number of single-bit persistent ECC errors.", "ecc_sbe_agg", False),
    ("DCGM_FI_DEV_ECC_DBE_AGG_TOTAL", "counter", "Total number of double-bit persistent ECC errors.", "ecc_dbe_agg", False),
    ("DCGM_FI_DEV_RETIRED_SBE", "gauge", "Total number of retired pages due to single-bit errors.", "retired_sbe", False),
    ("DCGM_FI_DEV_RETIRED_DBE", "gauge", "Total number of retired pages due to double-bit errors.", "retired_dbe", False),
    ("DCGM_FI_DEV_RETIRED_PENDING", "gauge", "Total number of pages pending retirement.", "retired_pending", False),
    ("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", "gauge", "Number of remapped rows for uncorrectable errors.", "remap_uncorr", False),
    ("DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS", "gauge", "Number of remapped rows for correctable errors.", "remap_corr", False),
    ("DCGM_FI_DEV_ROW_REMAP_FAILURE", "gauge", "Whether remapping of rows has failed.", "remap_failure", False),
    ("DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL", "counter", "Total NVLink bandwidth counter (in bytes).", "nvlink_bw", True),
    ("DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL", "counter", "Total NVLink flow-control CRC errors.", "nvlink_crc_flit", True),
    ("DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL", "counter", "Total NVLink data CRC errors.", "nvlink_crc_data", True),
    ("DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL", "counter", "Total NVLink retries.", "nvlink_replay", True),
    ("DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL", "counter", "Total NVLink recovery errors.", "nvlink_recovery", True),
    ("DCGM_FI_PROF_GR_ENGINE_ACTIVE", "gauge", "Ratio of time the graphics engine is active.", "gr_active", False),
    ("DCGM_FI_PROF_SM_ACTIVE", "gauge", "Ratio of cycles an SM has at least 1 warp assigned.", "sm_active", False),
    ("DCGM_FI_PROF_SM_OCCUPANCY", "gauge", "Ratio of warps resident on an SM.", "sm_occupancy", False),
    ("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "gauge", "Ratio of cycles the tensor (HMMA) pipe is active.", "tensor_active", False),
    ("DCGM_FI_PROF_DRAM_ACTIVE", "gauge", "Ratio of cycles the device memory interface is active.", "dram_active", False),
    ("DCGM_FI_PROF_PIPE_FP64_ACTIVE", "gauge", "Ratio of cycles the FP64 pipe is active.", "fp64_active", False),
    ("DCGM_FI_PROF_PIPE_FP32_ACTIVE", "gauge", "Ratio of cycles the FP32 pipe is active.", "fp32_active", False),
    ("DCGM_FI_PROF_PIPE_FP16_ACTIVE", "gauge", "Ratio of cycles the FP16 pipe is active.", "fp16_active", False),
    ("DCGM_FI_PROF_PCIE_TX_BYTES", "counter", "Total bytes transmitted over PCIe.", "pcie_tx", False),
    ("DCGM_FI_PROF_PCIE_RX_BYTES", "counter", "Total bytes received over PCIe.", "pcie_rx", False),
)


def _fmt(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


def render_dcgm(fleet: Fleet, region: str = "") -> str:
    """All visible GPUs in the DCGM exporter format, optionally for a single region."""
    with fleet.lock:
        gpus = [
            g
            for n in fleet.nodes
            for g in n.gpus
            if g.visible and (not region or g.region == region)
        ]
        out: list[str] = []
        for name, mtype, help_text, attr, nvlink_only in DCGM_METRICS:
            rows = [g for g in gpus if g.model.nvlink_gbs] if nvlink_only else gpus
            if not rows:
                continue
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {mtype}")
            out.extend(f"{name}{{{g.label_str}}} {_fmt(getattr(g, attr))}" for g in rows)
        return "\n".join(out) + "\n"


def render_self(fleet: Fleet, shipper) -> str:
    """The simulator's own health, scraped as a separate job."""
    with fleet.lock:
        total = sum(len(n.gpus) for n in fleet.nodes)
        visible = sum(1 for n in fleet.nodes for g in n.gpus if g.visible)
        faults = len(fleet.faults)
        ticks = fleet.stats["ticks"]
        by_level = dict(fleet.stats["logs"])
    lines = [
        "# HELP gpusim_gpus Simulated GPUs, by whether they currently export metrics.",
        "# TYPE gpusim_gpus gauge",
        f'gpusim_gpus{{state="total"}} {total}',
        f'gpusim_gpus{{state="visible"}} {visible}',
        "# HELP gpusim_active_faults Fault injections currently active.",
        "# TYPE gpusim_active_faults gauge",
        f"gpusim_active_faults {faults}",
        "# HELP gpusim_ticks_total Simulation ticks since start.",
        "# TYPE gpusim_ticks_total counter",
        f"gpusim_ticks_total {ticks}",
        "# HELP gpusim_logs_generated_total Log records generated, by level.",
        "# TYPE gpusim_logs_generated_total counter",
        *(f'gpusim_logs_generated_total{{level="{lvl}"}} {n}' for lvl, n in by_level.items()),
        "# HELP gpusim_logs_shipped_total Log records accepted by VictoriaLogs.",
        "# TYPE gpusim_logs_shipped_total counter",
        f"gpusim_logs_shipped_total {shipper.shipped}",
        "# HELP gpusim_logs_dropped_total Log records dropped because the shipping queue was full.",
        "# TYPE gpusim_logs_dropped_total counter",
        f"gpusim_logs_dropped_total {shipper.dropped}",
        "# HELP gpusim_log_ship_errors_total Failed pushes to VictoriaLogs.",
        "# TYPE gpusim_log_ship_errors_total counter",
        f"gpusim_log_ship_errors_total {shipper.errors}",
        "# HELP gpusim_log_queue_length Log records waiting to be shipped.",
        "# TYPE gpusim_log_queue_length gauge",
        f"gpusim_log_queue_length {shipper.queue_length()}",
    ]
    return "\n".join(lines) + "\n"
