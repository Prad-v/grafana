---
title: Anomaly detection for any metrics data source
menuTitle: Anomaly detection
description: The architecture, the detection engine, and the test commands for anomaly detection across Mimir, Prometheus, VictoriaMetrics, and other Grafana data sources.
weight: 120
---

# Anomaly detection for any metrics data source

This page describes how anomaly detection is built so that it works with any Grafana time series data source: Mimir, Prometheus, VictoriaMetrics, and OpenTelemetry metrics stored in any of them.

Before you begin, ensure you have the following:

- Docker with the Compose plugin
- `make`

You don't need Go on the host. The tests run in a container.

{{< admonition type="note" >}}
**Status:** the detection engine and its tests are complete. The Grafana data source plugin and the Explorer page are the next steps. Until they ship, use the [chat assistant](../chat-assistant/) for anomaly questions.
{{< /admonition >}}

## Architecture

Four layers, each with one job:

| Layer | Where | What it does | Status |
| --- | --- | --- | --- |
| Detection engine | `backend/services/anomaly-engine/` | Pure Go library. Takes samples in and returns findings out. No Grafana or backend dependency. | Done |
| Data source plugin | `plugins/kekaflow-anomaly-datasource/` | A backend data source that wraps another data source. A query reads "run query Q on data source X with detector D" and returns the series, an expected value, a band, and an anomaly score. Works in panels, Explore, and alert rules. | Planned |
| App plugin | `plugins/kekaflow-anomaly-app/` | An Explorer page, root-cause view, and a **Find anomalies** panel menu action. It displays what the data source returns and holds no detection logic. | Planned |
| Scheduling | Grafana alert rules | An alert rule on the wrapper data source. No custom scheduler. | Planned |

### Why a data source plugin

Grafana alert rules and recorded queries can only use backend data sources. A frontend-only plugin cannot alert. Building the feature as a backend data source gives alerting, dashboards, Explore, provisioning as code, and caching without extra services.

### Why the plugin never calls the metrics backend directly

The plugin asks Grafana to run the query (`/api/ds/query`) and reads the returned data frames. Any data source that returns time series frames then works the same way. OpenTelemetry is not a Grafana data source. OpenTelemetry metrics already work because they are stored in Mimir, Prometheus, or VictoriaMetrics.

## The detection engine

The engine lives in `backend/services/anomaly-engine/anomaly`. It uses only the Go standard library.

The default detector, `anomaly.Robust`, uses robust statistics (rolling median and MAD). It needs no history beyond the window it is given, and every finding says why it was raised.

| Finding | Meaning |
| --- | --- |
| `spike`, `dip` | A point, or a short run, far from the rolling median. |
| `level_shift` | The typical value moved and stayed there. |
| `flatline` | The value stopped changing in a series that normally moves. |
| `gap` | Samples are missing for much longer than the sampling step. |

Two behaviors keep the results quiet:

- **Steady rhythms are normal.** A series that repeats on a cycle (a batch job switching on and off) is compared with the same phase of other cycles. The cycle is reported under `Periodic`, not as anomalies. Three or more events at near-equal spacing are also treated as a rhythm.
- **Small changes are ignored.** A change smaller than 5% of the typical value, or smaller than a quarter of the series' normal swing, is not reported however clean it is statistically.

Across many series, `LabelConcentration` finds the label values the anomalous series share (for example `datacenter=dub1`), and `TimeClusters` finds series that changed together.

### Use the engine

Import the package, pass samples, and read the findings. A zero `Options` value uses the defaults.

```go
import "github.com/kekaflow/anomaly-engine/anomaly"

points := []anomaly.Point{{T: 1700000000, V: 61.2}, /* ... one per sample ... */}

report := anomaly.Analyze(points, anomaly.Options{}) // zero Options means the defaults
for _, f := range report.Findings {
    fmt.Println(f.Kind, anomaly.ISO(f.PeakTS), f.Score, f.Detail)
}
```

These options tune the detector:

| Option | Default | Effect |
| --- | --- | --- |
| `Sensitivity` | `4.5` | Score threshold in robust standard deviations. Lower finds more. |
| `MinScale` | `0` | Smallest noise level to assume. Set it for counts, where a jump from 1 to 3 is not unusual. |
| `MinEffectRatio` | `0.05` | Ignore changes smaller than this share of the typical value. |
| `Trace` | `false` | Also return `Times`, `Expected`, `Scores`, and `Scale` for drawing a band of `Sensitivity × Scale` around the expected value. |

The `anomaly.Detector` interface lets other detectors, such as a seasonal forecaster or an ML sidecar, plug in later without changing callers.

A `Finding` marshals to JSON with ISO timestamps: `kind`, `start`, `end`, `duration_s`, `peak_time`, `peak_value`, `baseline`, `score`, `detail`.

### Known limits

These are measured behaviors, not guesses. The tests state them.

- **Run the engine on the right query.** Detection runs on the series the query returns. Run it on `rate(...)` for counters. A series that only ever increases reports a shift.
- **Steps in a cycling series are imprecise.** A step is visible only on the average over one whole cycle, so it is placed within a quarter of a cycle of the true position. About 1 in 100 such steps does not clear the threshold.
- **Sensitivity 3 finds noise.** At 3 standard deviations, ordinary noise trips now and then. The default of 4.5 almost never does on a few hundred samples. In 300 clean series, 1 reported a finding.
- **Cost grows with the window.** Cycle detection costs about *n × n/3* operations. Ask Grafana for at most about 1000 points per series.

## Access to the wrapped data source

The next step, the data source plugin, must query another data source. This was tested against a running Grafana 13.4 on 2026-11-19.

- A service account token with the **None** role gets `403 datasources:query`. The token needs a role that can query.
- In open-source Grafana, a **Viewer** service account token can query every data source in the organization, including logs. The token cannot be limited to one data source. Limiting it needs Enterprise data source permissions, or the `externalServiceAccounts` feature, which is a public preview and off by default.
- Alert rules run as the built-in `grafana_scheduler` user, which can query all data sources. An alert rule carries no end-user identity.
- `/api/ds/query` returns the same frame shape for the Prometheus-type and the native VictoriaMetrics data sources: one frame per series, with the labels on the value field (`timeseries-multi`).

The design that follows from this:

1. Each wrapper data source instance is **bound to one inner data source**, set by an administrator at provisioning time. Users cannot choose an arbitrary inner data source from a query.
2. The wrapper holds a service account token in its secure settings. Give the wrapper the same permissions as the inner data source.
3. Document that in open-source Grafana the token is organization-wide, and recommend a dedicated service account.

## Run the tests

The tests are in `tests/anomaly_engine/`, a separate Go module so it does not pull in Grafana's dependency graph. They run in a container, with the race detector on, and in CI on every commit.

```sh
make anomaly-test
```

Or directly:

```sh
docker compose -f tests/docker-compose.yml run --rm anomaly-engine-unit
```

Every test series is synthetic with a known injected fault, so a failure means the detector stopped finding something real or started inventing something.

## Troubleshooting

These are the most common problems.

### The Go workspace error

If you see `directory prefix . does not contain modules listed in go.work or their selected dependencies`, you ran `go test` on the host. Run the tests through Docker Compose. The container sets `GOWORK=off`, so the repository's `go.work` doesn't take over.

### A statistical test fails on one seed

Don't loosen the test for that seed. The tests measure rates over many seeds. For example, a cycling series must report no findings across 100 seeds. A single failing seed means the false-positive rate moved.

### A report has `InsufficientData` set

The series has fewer than 12 usable samples after dropping `NaN` and infinite values. Widen the time range or use a smaller step.

## Related

- [Ask questions about your data with the chat assistant](../chat-assistant/)
- [Run the GPU observability stack](../observability-stack/)
- [User guide (static page)](../../../static/anomaly-detection/index.html)
