---
title: Run the GPU observability stack with Docker Compose
menuTitle: GPU observability stack
description: Run VictoriaMetrics, VictoriaLogs, a mock multi-region NVIDIA GPU fleet, Keycloak, Grafana, and mcp-grafana locally with make and Docker Compose.
weight: 110
---

# Run the GPU observability stack with Docker Compose

This guide shows how to run a complete local observability demo with one `make` command. A mock service simulates a multi-region NVIDIA GPU fleet. It exports DCGM-style metrics to VictoriaMetrics and pushes logs to VictoriaLogs with the same labels. Grafana shows both, Keycloak provides single sign-on, and mcp-grafana lets AI agents query Grafana. A web UI lets you control the simulation and inject faults.

Everything runs in containers. The simulator lives in `backend/services/gpu-simulator/`.

Before you begin, ensure you have the following:

- Docker with the Compose plugin
- `make`
- Outbound access to `grafana.com` on the first start, so Grafana can install the VictoriaMetrics and VictoriaLogs data source plugins
- For `make stack-up`, the Grafana image built once with `make compose-build`. Refer to [Build and deploy with Docker Compose](../docker-compose/).

You don't need Go, Node.js, or Python on the host.

## Quick start

Start everything: the observability services, Keycloak, Grafana, mcp-grafana, and the [chat assistant](../chat-assistant/).

```sh
make stack-up
```

If the Grafana image doesn't exist yet, use `make stack-deploy` instead. It builds the image first, which takes a long time.

To run only VictoriaMetrics, VictoriaLogs, and the simulator, without Grafana or Keycloak, use:

```sh
make obs-up
```

When the command finishes, open these addresses:

| Service | Address | Sign in |
| --- | --- | --- |
| Simulator UI | <http://localhost:8090> | None |
| Grafana | <http://localhost:3000> | `admin` / `admin`, or **Sign in with Keycloak** |
| VictoriaMetrics UI | <http://localhost:8428/vmui> | None |
| VictoriaLogs UI | <http://localhost:9428/select/vmui> | None |
| Keycloak | <http://localhost:8080> | Values from `.env` |
| mcp-grafana | `http://localhost:8000/mcp` | None |
| Chat assistant | <http://localhost:3000/a/kekaflow-chat-app> | Grafana login |

In Grafana, open **Dashboards** > **GPU fleet** > **GPU fleet overview**.

## Architecture

The simulator is the only source of data. VictoriaMetrics scrapes it, and the simulator pushes logs.

```text
                      scrape /metrics every 10s
 VictoriaMetrics  <-----------------------------  gpu-simulator  (UI, API, /metrics)
        ^                                              |
        | PromQL                                       | POST /insert/jsonline
        |                                              v
     Grafana  ------------ LogsQL --------------->  VictoriaLogs
        ^   ^
        |   +-- OAuth login --- Keycloak
        |
   mcp-grafana  <---- MCP (streamable HTTP) ---- AI agents
```

The services are split over three Compose files so you can start the parts you need.

- `docker-compose.observability.yml` defines VictoriaMetrics, VictoriaLogs, and the simulator. It creates the `kekaflow-observability` network.
- `docker-compose.observability-grafana.yml` is an override for Grafana. It provisions the data sources and dashboard, installs the official VictoriaMetrics and VictoriaLogs plugins, and adds mcp-grafana.
- `docker-compose.keycloak.yml` and `docker-compose.sso.yml` provide Keycloak SSO. Refer to [Configure team sync for OAuth in Grafana OSS](../../setup-grafana/configure-access/configure-team-sync-oss/).

## Make targets

Each target wraps a `docker compose` command. Run `make help` to list them.

| Target | Description |
| --- | --- |
| `obs-env` | Append the observability settings from `.env.observability.example` to `.env` if they're missing. |
| `obs-build` | Build the simulator image. |
| `obs-up` | Start VictoriaMetrics, VictoriaLogs, and the simulator. |
| `obs-test` | Check that metrics and logs flow end to end. Needs `obs-up`. |
| `obs-logs` | Follow the logs of the three services. |
| `obs-down` | Stop the services. Data is kept. |
| `obs-clean` | Stop the services and delete their data and the simulator image. |
| `sim-test` | Run the simulator unit tests in a container. |
| `stack-up` | Start everything, reusing the built Grafana image. |
| `stack-deploy` | Build the Grafana image, then run `stack-up`. |
| `stack-test` | Check the full stack, including Grafana and mcp-grafana. Needs `stack-up`. |
| `stack-ps` | Show the status of every container. |
| `stack-logs` | Follow the logs of the whole stack. |
| `stack-down` | Stop everything. All data volumes are kept. |

`stack-up` runs these steps in order:

1. Starts VictoriaMetrics, VictoriaLogs, and the simulator.
1. Starts Keycloak.
1. Starts Grafana with the SSO and observability overrides.
1. Creates the tenant organizations and restarts Grafana, because organization names resolve at startup.
1. Starts mcp-grafana after Grafana is healthy.

## The simulated fleet

The fleet is deterministic: the same settings always produce the same nodes, UUIDs, and workloads. It has six regions with two datacenters each.

| Region | City | GPU model | Datacenters |
| --- | --- | --- | --- |
| `us-east-1` | N. Virginia | NVIDIA H100 | `iad1`, `iad2` |
| `us-west-2` | Oregon | NVIDIA H200 | `pdx1`, `pdx2` |
| `eu-west-1` | Ireland | NVIDIA A100 | `dub1`, `dub2` |
| `eu-central-1` | Frankfurt | NVIDIA H100 | `fra1`, `fra2` |
| `ap-south-1` | Mumbai | NVIDIA L40S | `bom1`, `bom2` |
| `ap-northeast-1` | Tokyo | NVIDIA B200 | `nrt1`, `nrt2` |

By default each datacenter has 4 nodes with 8 GPUs each, which gives 384 GPUs. Four nodes share a rack. Each node runs one workload: `training`, `inference`, `batch`, or `idle`. The workload decides the utilization pattern and the `namespace`, `pod`, and `container` labels.

- **Training:** Runs near 96% utilization with a short dip every 10 minutes for a checkpoint.
- **Inference:** Follows a daily curve in the region's local time.
- **Batch:** Runs 8 minutes on and 4 minutes off.
- **Idle:** Stays at a few percent.

Temperature, power, clocks, and throttling follow from utilization and ambient temperature. A GPU that gets too hot lowers its clocks, which lowers its power and, over time, its temperature.

### Labels

Metrics and logs share these labels, so you can move from a series to the logs of the same GPU.

| Label | Example | On metrics | On logs |
| --- | --- | --- | --- |
| `region` | `us-east-1` | Yes | Yes |
| `availability_zone` | `us-east-1a` | Yes | Yes |
| `datacenter` | `iad1` | Yes | Yes |
| `cluster` | `gpu-iad1` | Yes | Yes |
| `rack` | `rack-01` | Yes | Yes |
| `node` | `gpu-iad1-r01-n01` | Yes | Yes |
| `gpu` | `3` | Yes | Yes |
| `UUID` | `GPU-f39d4f7a-...` | Yes | Yes |
| `modelName` | `NVIDIA H100 80GB HBM3` | Yes | Yes |
| `namespace`, `pod`, `container` | `ml-training`, `llm-train-190c5`, `trainer` | When the GPU runs a workload | When the GPU runs a workload |
| `Hostname`, `device`, `pci_bus_id`, `DCGM_FI_DRIVER_VERSION`, `instance` | `nvidia3`, `00000000:5D:00.0` | Yes | No |
| `app`, `level` | `kernel`, `error` | No | Yes |
| `xid` | `79` | No | On Xid events |

### Metrics

The simulator exports the metrics of NVIDIA's [DCGM exporter](https://github.com/NVIDIA/dcgm-exporter) at `/metrics`, with the same names and types. The set includes:

- **Utilization:** `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_MEM_COPY_UTIL`, `DCGM_FI_DEV_ENC_UTIL`, `DCGM_FI_DEV_DEC_UTIL`, and the profiling fields `DCGM_FI_PROF_GR_ENGINE_ACTIVE`, `SM_ACTIVE`, `SM_OCCUPANCY`, `PIPE_TENSOR_ACTIVE`, `DRAM_ACTIVE`, and `PIPE_FP64`, `FP32`, and `FP16_ACTIVE`.
- **Memory:** `DCGM_FI_DEV_FB_USED`, `FB_FREE`, and `FB_TOTAL`.
- **Thermal and power:** `DCGM_FI_DEV_GPU_TEMP`, `MEMORY_TEMP`, `SLOWDOWN_TEMP`, `POWER_USAGE`, `ENFORCED_POWER_LIMIT`, `TOTAL_ENERGY_CONSUMPTION`, `SM_CLOCK`, `MEM_CLOCK`, `CLOCK_THROTTLE_REASONS`, `THERMAL_VIOLATION`, and `POWER_VIOLATION`.
- **Errors:** `DCGM_FI_DEV_XID_ERRORS`, the `ECC_SBE_*` and `ECC_DBE_*` counters, `RETIRED_*`, `UNCORRECTABLE_REMAPPED_ROWS`, `CORRECTABLE_REMAPPED_ROWS`, and `ROW_REMAP_FAILURE`.
- **Interconnect:** `DCGM_FI_DEV_PCIE_REPLAY_COUNTER`, `PCIE_LINK_GEN`, `PCIE_LINK_WIDTH`, `DCGM_FI_PROF_PCIE_TX_BYTES`, `PCIE_RX_BYTES`, and, on boards with NVLink, `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` and the `NVLINK_*_ERROR_COUNT_TOTAL` counters. The L40S has no NVLink and doesn't export them.

The `/metrics?region=<REGION>` form returns a single region. The simulator's own health is at `/sim/metrics`, with `gpusim_gpus`, `gpusim_active_faults`, and the `gpusim_logs_*` counters.

### Logs

Logs are pushed to VictoriaLogs as JSON lines with `region`, `datacenter`, `cluster`, `node`, and `app` as stream fields. Background logs come from five sources:

- **`kernel`:** The NVIDIA driver and PCIe messages, including `NVRM: Xid` lines.
- **`dcgm-exporter`:** Collection and health messages, plus thermal, power, and ECC warnings.
- **`nvidia-fabricmanager`:** NVSwitch and NVLink messages. Only nodes with NVLink emit them.
- **`nvidia-device-plugin`:** Device health and allocation messages.
- **`workload`:** The `training-job`, `inference-server`, or `batch-job` application of the node.

## Control the simulation

Open the simulator UI at <http://localhost:8090>. The page refreshes every two seconds and shows a live tail of the generated logs.

- **Scenarios:** One click applies a preset. Refer to [Presets](#presets).
- **Fleet:** Pause or resume the simulation, reset the fleet, and set the global load, the sensor noise, and the size of the fleet. A reset reboots every GPU, clears faults, and restores defaults. Resizing keeps the state of GPUs that remain.
- **Regions:** Switch a region off to stop all of its metrics and logs, like a network partition. Set a load multiplier and an ambient temperature offset per region. The table shows live aggregates.
- **Inject a fault:** Pick a fault, a scope from region down to a single GPU, an intensity, and a duration.
- **Logs:** Set the background rate, the share of warnings and errors, and which sources are on. Emit a burst of lines at a scope to test log panels and alerts.

### Fault kinds

| Fault | Effect |
| --- | --- |
| `thermal` | Raises the ambient temperature. GPUs heat up, throttle, and log slowdown warnings. |
| `power_cap` | Limits board power. Power violations grow and clocks drop. |
| `xid` | Emits Xid errors in the kernel log and sets `DCGM_FI_DEV_XID_ERRORS`. Pick a code or leave it random. |
| `ecc` | Adds single-bit errors. At intensity above 0.5 it adds double-bit errors with row remapping. |
| `nvlink` | Adds NVLink CRC and replay errors and lowers bandwidth. Has no effect on boards without NVLink. |
| `pcie` | Adds PCIe replays and retrains the link at a lower generation or width. |
| `mem_leak` | Fills the framebuffer over the fault duration, or over 10 minutes when it lasts until cleared, until the workload logs CUDA out-of-memory errors. |
| `load` | Pins utilization to a value. |
| `gpu_lost` | Xid 79. The GPU disappears from metrics and the device plugin marks it unhealthy. |
| `node_down` | The node stops exporting metrics and logs. |

A fault with duration `0` lasts until you clear it.

### Presets

| Preset | Effect |
| --- | --- |
| `healthy` | Clears all faults and restores default settings. |
| `heatwave` | Cooling failure in `eu-west-1` / `dub1` for 30 minutes. |
| `training-surge` | Global load at 120% and `us-east-1` pinned near 99% for 20 minutes. |
| `xid-storm` | Xid and ECC errors on one `ap-northeast-1` node for 15 minutes. |
| `region-outage` | `us-west-2` goes dark. |
| `gpu-lost` | One GPU in `eu-central-1` falls off the bus. |
| `chaos` | A random mix of faults across regions for 15 minutes. |

## Simulator API

The UI uses a JSON API that you can call from scripts. All bodies are JSON objects. Invalid input returns status `400` with `{"error": "<MESSAGE>"}`.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/state` | Settings, region aggregates, active faults, and log shipping status. |
| `GET` | `/api/meta` | Fault kinds, presets, log sources, and service links. |
| `GET` | `/api/topology` | Regions, datacenters, and node names. |
| `GET` | `/api/logs?limit={n}&level={level}&region={region}` | The most recent generated logs. `limit` is 1 to 500. |
| `PUT` | `/api/settings` | Update global settings. |
| `PUT` | `/api/regions/{region}` | Update `enabled`, `load` (0 to 2), or `ambient_offset` (-10 to 40). |
| `PUT` | `/api/topology` | Set `nodes_per_dc` (1 to 64) and `gpus_per_node` (1 to 16). The fleet is limited to 4096 GPUs. |
| `POST` | `/api/faults` | Inject a fault. |
| `DELETE` | `/api/faults/{id}` | Remove one fault. |
| `DELETE` | `/api/faults` | Remove all faults. |
| `POST` | `/api/logs/burst` | Emit log lines at once. |
| `POST` | `/api/presets/{name}` | Apply a preset. |
| `POST` | `/api/reset` | Reboot the fleet and restore defaults. |
| `GET` | `/metrics`, `/sim/metrics`, `/healthz` | Prometheus metrics and the health check. |

Global settings accepted by `PUT /api/settings`:

| Field | Type | Range | Default |
| --- | --- | --- | --- |
| `paused` | boolean | | `false` |
| `intensity` | number | 0 to 1.5 | `1.0` |
| `noise` | number | 0 to 1 | `0.5` |
| `logs_enabled` | boolean | | `true` |
| `log_rate` | number | 0 to 20 lines per node per second | `0.2` |
| `warn_ratio`, `error_ratio` | number | 0 to 1, sum at most 1 | `0.05`, `0.01` |
| `sources` | object | Keys `kernel`, `dcgm-exporter`, `nvidia-fabricmanager`, `nvidia-device-plugin`, `workload` with boolean values | all `true` |

Inject a cooling failure in one datacenter for 15 minutes:

```sh
curl -X POST http://localhost:8090/api/faults \
  -H 'Content-Type: application/json' \
  -d '{"kind": "thermal", "scope": {"region": "eu-west-1", "datacenter": "dub1"}, "duration_s": 900, "intensity": 0.8}'
```

The request fields are:

- `kind`: One of the fault kinds.
- `scope`: Optional object with `region`, `datacenter`, `node`, and `gpu`. `gpu` needs `node`. An empty scope covers the whole fleet.
- `duration_s`: Seconds, from 0 to 86400. `0` lasts until cleared.
- `intensity`: A number from 0 to 1.
- `params`: Optional. `xid` takes an Xid code for the `xid` fault. `value` takes a utilization from 0 to 100 for the `load` fault.

The response is `{"id": "<FAULT_ID>"}`.

Emit 50 error lines on one node:

```sh
curl -X POST http://localhost:8090/api/logs/burst \
  -H 'Content-Type: application/json' \
  -d '{"count": 50, "level": "error", "app": "kernel", "scope": {"node": "gpu-iad1-r01-n01"}, "message": "test alert"}'
```

Leave `message` empty to generate realistic lines. The API has no authentication, so the stack publishes it on `127.0.0.1` only. Change `OBS_BIND_ADDRESS` only on a network you trust.

## Query the data

Use the labels to move between metrics and logs. Grafana provisions three data sources:

- **VictoriaMetrics:** The official `victoriametrics-metrics-datasource` plugin, with the MetricsQL editor. It's the default and the dashboard uses it.
- **VictoriaMetrics (Prometheus API):** The same VictoriaMetrics through the built-in Prometheus data source. Grafana alerting and the mcp-grafana `query_prometheus` tool only work with Prometheus-type data sources, so use this one for them.
- **VictoriaLogs:** The official `victoriametrics-logs-datasource` plugin.

Average utilization per region, in PromQL:

```promql
avg by (region) (DCGM_FI_DEV_GPU_UTIL)
```

Errors and warnings of one node, in LogsQL:

```text
node:"gpu-iad1-r01-n01" level:in(warn,error)
```

Grafana provisions two copies of the GPU fleet dashboard so you can compare the data sources side by side:

- **GPU fleet overview:** `observability/grafana/dashboards/gpu-fleet.json`. The metrics panels use the **VictoriaMetrics** plugin data source.
- **GPU fleet overview (Prometheus API):** `observability/grafana/dashboards/gpu-fleet-prometheus.json`. The metrics panels use the **VictoriaMetrics (Prometheus API)** data source. The logs panels are the same.

When you change one dashboard, make the same change in the other, or the comparison drifts. The first dashboard has `region`, `datacenter`, and `node` variables, and panels for utilization, temperature, power, memory, throttling, hardware errors, Xid events, and logs. Provisioning applies to the main organization only, so users that Keycloak maps to the `TenantA` or `TenantB` organizations don't see the data sources until you add them there.

## Connect an AI agent through mcp-grafana

mcp-grafana serves Grafana as tools for dashboards, queries, and alerting. It listens on the streamable HTTP transport at `http://localhost:8000/mcp`. Add it to your MCP client. For example, in `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "grafana": { "url": "http://localhost:8000/mcp" }
  }
}
```

By default mcp-grafana signs in to Grafana as the admin user from `.env`, so it can change dashboards. For least privilege, create a service account in Grafana, set `MCP_GRAFANA_TOKEN` in `.env`, and run `make stack-up` again. To block writes, add `--disable-write` to the `command` of `mcp-grafana` in `docker-compose.observability-grafana.yml`.

## Environment variables

Set these in `.env`. `make obs-env` appends them with their defaults. All are optional.

| Variable | Default | Description |
| --- | --- | --- |
| `OBS_BIND_ADDRESS` | `127.0.0.1` | Address the simulator, VictoriaMetrics, VictoriaLogs, and mcp-grafana ports are published on. |
| `VICTORIAMETRICS_TAG` | `v1.156.0` | VictoriaMetrics image tag. |
| `VICTORIAMETRICS_PORT` | `8428` | Host port of VictoriaMetrics. |
| `VICTORIALOGS_TAG` | `v1.54.0` | VictoriaLogs image tag. |
| `VICTORIALOGS_PORT` | `9428` | Host port of VictoriaLogs. |
| `OBS_RETENTION` | `7d` | Retention of metrics and logs. |
| `GPU_SIM_PORT` | `8090` | Host port of the simulator UI and API. |
| `GPU_SIM_NODES_PER_DC` | `4` | Nodes per datacenter at start. |
| `GPU_SIM_GPUS_PER_NODE` | `8` | GPUs per node at start. |
| `GPU_SIM_TICK_SECONDS` | `5` | Seconds between simulation steps. |
| `GPU_SIM_LOG_RATE` | `0.2` | Background log lines per node per second at start. |
| `GPU_SIM_SEED` | empty | A number makes the simulation repeatable. |
| `MCP_GRAFANA_TAG` | `2.3.0-alpine` | mcp-grafana image tag. |
| `MCP_GRAFANA_PORT` | `8000` | Host port of mcp-grafana. |
| `MCP_GRAFANA_TOKEN` | empty | Grafana service account token. When empty, the admin user and password are used. |

The simulator also reads `SIM_PORT`, `SIM_LOG_LEVEL`, and `VICTORIALOGS_URL`. An empty `VICTORIALOGS_URL` keeps logs in the in-memory tail only.

The total number of GPUs is 6 regions × 2 datacenters × `GPU_SIM_NODES_PER_DC` × `GPU_SIM_GPUS_PER_NODE`. Series count grows with it, at about 46 series per GPU. State isn't persisted: restarting the simulator resets faults and settings.

## Tests

All tests live under `tests/` and run in containers. CI runs them on every commit through `.github/workflows/tests-folder.yml`.

- `make sim-test` runs the simulator unit tests in `tests/gpu_simulator/`. They cover the topology, the physics, every fault kind, log content, the shipper, and the HTTP API.
- `make obs-test` runs `tests/observability/` against the running services. It checks that all regions and GPUs reach VictoriaMetrics, that logs reach VictoriaLogs with the same labels as the metrics, and that Xid, GPU-lost, and burst events show up in both.
- `make stack-test` runs the same tests and also checks the Grafana data sources, the dashboard, and the mcp-grafana tools.

The end-to-end tests inject faults and remove them again. They're safe to run next to a stack you're watching, but you see the faults for a minute.

## Production

This stack is for local development and demos. The simulator generates fake data and has no authentication. Use Helm for production Kubernetes deployments of Grafana.

## Troubleshooting

Use these fixes for common problems.

- **`obs-up` can't pull or build an image.** Check your network access. The simulator image builds from `backend/services/gpu-simulator/`.
- **The VictoriaMetrics or VictoriaLogs data source is missing or shows an error.** Grafana installs the plugins on start and needs access to `grafana.com`. Run `make stack-logs` and look for `plugin.installer`. After you fix access, run `make stack-up` again.
- **The dashboard shows no data.** Wait about 30 seconds after a start for the first scrape. Then open <http://localhost:8428/targets> and check that `dcgm-exporter` is up.
- **No logs in VictoriaLogs.** Open the simulator UI and check **ship errors** in the header. Check that **Generate logs** is on and the background rate isn't zero.
- **A port is in use.** Set `GPU_SIM_PORT`, `VICTORIAMETRICS_PORT`, `VICTORIALOGS_PORT`, or `MCP_GRAFANA_PORT` in `.env`, then run the target again.
- **mcp-grafana returns `403` or rejects the host.** It allows `localhost`, `127.0.0.1`, and `mcp-grafana` with the configured port. Connect through one of those names.
- **Too many series or slow queries.** Lower `GPU_SIM_NODES_PER_DC` or use the **Fleet** card in the simulator UI.
- **Reset to a clean state.** Run `make stack-down`, then `make obs-clean`, then `make stack-up`.

## Next steps

- Refer to [Build and deploy with Docker Compose](../docker-compose/) for the Grafana image.
- Refer to [Configure team sync for OAuth in Grafana OSS](../../setup-grafana/configure-access/configure-team-sync-oss/) for the Keycloak demo users.
- Refer to the [VictoriaMetrics](https://docs.victoriametrics.com/) and [VictoriaLogs](https://docs.victoriametrics.com/victorialogs/) documentation.
- Refer to [Ask questions about your data with the chat assistant](../chat-assistant/) to query this data in plain language.
- Refer to the [mcp-grafana repository](https://github.com/grafana/mcp-grafana) for the full list of tools and flags.
