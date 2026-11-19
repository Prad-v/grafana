"""Tools the agent calls directly, next to the ones mcp-grafana provides.

mcp-grafana returns raw query results, which is the right shape for a human to read
but costly in tokens and no help for finding patterns. These tools do the numeric
work in code and hand the model a short, ranked summary. They query the metrics and
logs backends from the settings.

No `from __future__ import annotations` here: ADK builds the model-facing function
declaration from the real annotations.
"""

import math
import statistics
import time
from typing import Callable

from . import anomaly
from .backends import Backends, BackendError, fill_missing_buckets, pick_step, resolve_window
from .config import Settings

MAX_SERIES_ANALYZED = 300
MAX_QUERY_CHARS = 4000


def _check_query(query: str) -> str:
    query = (query or "").strip()
    if not query:
        raise BackendError("The query is empty.")
    if len(query) > MAX_QUERY_CHARS:
        raise BackendError(f"The query is longer than {MAX_QUERY_CHARS} characters.")
    return query


def _label(labels: dict[str, str]) -> dict[str, str]:
    return dict(labels)


def build_tools(settings: Settings, backends: Backends, clock: Callable[[], float] = time.time) -> list[Callable]:
    """Create the tool functions bound to one set of settings and backends."""

    async def summarize_metric(
        promql: str,
        lookback_minutes: int = 60,
        step_seconds: int = 0,
        end_time: str = "",
        top: int = 10,
        rank_by: str = "max",
        order: str = "desc",
    ) -> dict:
        """Run a PromQL/MetricsQL range query and return a compact per-series summary.

        Use this to answer "what is X", "which are highest/lowest", "how did X change".
        It is cheaper and clearer than a raw range query. Each series gets min, max,
        average, last value, change over the window, and a text sparkline of its shape.

        Args:
            promql: The PromQL or MetricsQL expression. Aggregate with `avg by (label) (...)`
                when there are many series.
            lookback_minutes: How far back to look, ending at `end_time`. Default 60.
            step_seconds: Resolution in seconds. 0 picks about 180 points automatically.
            end_time: End of the window as ISO 8601 (for example 2026-01-31T14:00:00Z).
                Empty means now.
            top: How many series to return, 1 to 50. Default 10.
            rank_by: Which statistic orders the series: max, min, avg, last, or change.
            order: desc for highest first, asc for lowest first.

        Returns:
            A dict with `status`, the resolved `window`, `series_total`, and `series`.
        """
        try:
            query = _check_query(promql)
            start, end = resolve_window(lookback_minutes, end_time, clock())
            step = pick_step(end - start, step_seconds)
            series = await backends.metrics_range(query, start, end, step)
        except BackendError as exc:
            return {"status": "error", "error": str(exc)}

        rows = []
        for labels, points in series:
            pts = anomaly.clean(points)
            if not pts:
                continue
            values = [v for _, v in pts]
            ordered = sorted(values)
            first, last = values[0], values[-1]
            rows.append(
                {
                    "labels": _label(labels),
                    "samples": len(values),
                    "min": anomaly._round(ordered[0]),
                    "max": anomaly._round(ordered[-1]),
                    "avg": anomaly._round(statistics.fmean(values)),
                    "p95": anomaly._round(anomaly._quantile(ordered, 0.95)),
                    "first": anomaly._round(first),
                    "last": anomaly._round(last),
                    "change_pct": round((last - first) / abs(first) * 100, 1) if first else None,
                    "sparkline": anomaly.sparkline(values),
                    "_change": last - first,
                }
            )

        key = {"max": "max", "min": "min", "avg": "avg", "last": "last", "change": "_change"}.get(rank_by, "max")
        rows.sort(key=lambda r: r[key], reverse=(order != "asc"))
        top = max(1, min(int(top), 50))
        shown = rows[:top]
        for row in shown:
            row.pop("_change", None)
        result = {
            "status": "success",
            "query": query,
            "window": {"start": anomaly.iso(start), "end": anomaly.iso(end), "step_seconds": step},
            "series_total": len(rows),
            "series_shown": len(shown),
            "ranked_by": f"{rank_by} {'ascending' if order == 'asc' else 'descending'}",
            "series": shown,
        }
        if not rows:
            result["note"] = "The query returned no samples in this window. Check the metric name, labels, and time range."
        elif len(rows) > len(shown):
            result["note"] = f"{len(rows) - len(shown)} more series not shown. Aggregate with `by (...)` or raise `top`."
        return result

    async def detect_metric_anomalies(
        promql: str,
        lookback_minutes: int = 180,
        step_seconds: int = 0,
        end_time: str = "",
        sensitivity: float = 4.5,
        max_series: int = 5,
    ) -> dict:
        """Find anomalous behaviour in a metric: spikes, dips, level shifts, flatlines, and gaps.

        Use this for "is anything unusual", "when did X start misbehaving", or "what is
        different about Y". It analyses every series the query returns and ranks them. It
        also reports which label values the anomalous series share (for example all in one
        region or rack), and the times when many series changed together.

        The method compares each point to a rolling median using a robust spread (MAD).
        It has no memory beyond the window, so use a lookback long enough to include
        normal behaviour: at least a few times longer than the anomaly you expect.

        Args:
            promql: The PromQL or MetricsQL expression. Return one series per entity you
                want compared, for example `DCGM_FI_DEV_GPU_TEMP` for every GPU.
            lookback_minutes: Window to analyse, ending at `end_time`. Default 180.
            step_seconds: Resolution in seconds. 0 picks about 180 points automatically.
            end_time: End of the window as ISO 8601. Empty means now.
            sensitivity: Score threshold in robust standard deviations. 4.5 is a sensible
                default. 3 finds more and noisier results, 7 finds only the clear ones.
            max_series: How many of the most anomalous series to describe, 1 to 20.

        Returns:
            A dict with `status`, counts, `anomalous_series` (ranked, each with `findings`
            and a `sparkline`), `patterns` (shared labels), and `simultaneous` (times when
            many series changed at once).
        """
        try:
            query = _check_query(promql)
            start, end = resolve_window(lookback_minutes, end_time, clock())
            step = pick_step(end - start, step_seconds)
            series = await backends.metrics_range(query, start, end, step)
        except BackendError as exc:
            return {"status": "error", "error": str(exc)}
        return _report(
            kind="metric",
            query=query,
            series=series,
            start=start,
            end=end,
            step=step,
            sensitivity=sensitivity,
            max_series=max_series,
            min_scale=0.0,
        )

    async def detect_log_anomalies(
        logsql: str,
        lookback_minutes: int = 180,
        bucket_seconds: int = 0,
        end_time: str = "",
        group_by: str = "",
        sensitivity: float = 4.5,
        max_series: int = 5,
    ) -> dict:
        """Find unusual changes in how many log lines match a LogsQL query.

        Use this for "did errors increase", "is any node logging more than usual", or
        "when did the Xid errors start". It counts matching lines per time bucket and
        looks for bursts, drops, level shifts, and gaps (a source that went silent).
        To read the actual lines, use the Grafana log query tool afterwards.

        Args:
            logsql: A VictoriaLogs LogsQL filter, for example `level:error` or
                `app:kernel AND "NVRM: Xid"`. Use `*` for all logs.
            lookback_minutes: Window to analyse, ending at `end_time`. Default 180.
            bucket_seconds: Bucket size in seconds. 0 picks about 180 buckets automatically.
            end_time: End of the window as ISO 8601. Empty means now.
            group_by: Optional field to count separately, for example `node` or `region`.
                Leave empty for one total count.
            sensitivity: Score threshold in robust standard deviations. 4.5 is a sensible default.
            max_series: How many of the most anomalous groups to describe, 1 to 20.

        Returns:
            Same shape as `detect_metric_anomalies`, with line counts as values.
        """
        try:
            query = _check_query(logsql)
            start, end = resolve_window(lookback_minutes, end_time, clock())
            step = pick_step(end - start, bucket_seconds, target_points=180)
            # Whole buckets only: the first and last bucket are otherwise partial and look like dips.
            start = math.ceil(start / step) * step
            end = math.floor(end / step) * step - step
            if end <= start:
                raise BackendError("The window is shorter than one bucket. Raise lookback_minutes or lower bucket_seconds.")
            hits = await backends.log_hits(query, start, end + step, step, group_by.strip())
        except BackendError as exc:
            return {"status": "error", "error": str(exc)}

        series = [(labels, fill_missing_buckets(points, start, end, step)) for labels, points in hits]
        if not series:
            series = [({}, fill_missing_buckets([], start, end, step))]
        # Counts are discrete: a handful of lines is normal noise, not an anomaly.
        typical = statistics.median(v for _, pts in series for _, v in pts) if series else 0
        report = _report(
            kind="log",
            query=query,
            series=series,
            start=start,
            end=end,
            step=step,
            sensitivity=sensitivity,
            max_series=max_series,
            min_scale=max(1.0, math.sqrt(max(typical, 0))),
        )
        if report.get("status") == "success":
            report["total_lines"] = int(sum(v for _, pts in series for _, v in pts))
        return report

    return [summarize_metric, detect_metric_anomalies, detect_log_anomalies]


def _cycles(items) -> dict:
    """Summarise series that repeat on a steady rhythm, which is normal, so they are not anomalies."""
    counts: dict[tuple[str, int], int] = {}
    for _, _, _, rep in items:
        for cycle in rep.periodic:
            key = (cycle["kind"], round(cycle["period_s"] / 60))
            counts[key] = counts.get(key, 0) + 1
    names = {"cycle": "repeats", "step_up": "steps up", "step_down": "steps down", "spike": "spikes", "dip": "dips"}
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:4]
    return {
        "series": len(items),
        "note": "These series repeat on a steady rhythm (for example a batch job switching on and off). That is normal behaviour, not an anomaly.",
        "examples": [{"pattern": f"{names[k]} about every {m} min", "series": n} for (k, m), n in top],
    } if items else {"series": 0}


def _report(kind, query, series, start, end, step, sensitivity, max_series, min_scale) -> dict:
    sensitivity = max(1.5, min(float(sensitivity), 10.0))
    max_series = max(1, min(int(max_series), 20))
    truncated = len(series) > MAX_SERIES_ANALYZED
    series = series[:MAX_SERIES_ANALYZED]

    analysed = []
    insufficient = 0
    for idx, (labels, points) in enumerate(series):
        rep = anomaly.analyze(points, sensitivity=sensitivity, min_scale=min_scale)
        if rep.insufficient_data:
            insufficient += 1
            continue
        analysed.append((idx, labels, points, rep))

    flagged = [item for item in analysed if item[3].findings]
    cycling = [item for item in analysed if item[3].periodic]
    flagged.sort(key=lambda item: (item[3].score, len(item[3].findings)), reverse=True)

    described = []
    for idx, labels, points, rep in flagged[:max_series]:
        values = [v for _, v in anomaly.clean(points)]
        described.append(
            {
                "labels": _label(labels),
                "samples": rep.points,
                "typical_value": anomaly._round(rep.median),
                "latest_value": anomaly._round(rep.last),
                "anomalous_now": rep.current_score >= sensitivity,
                "worst_score": anomaly._round(rep.score, 3),
                "sparkline": anomaly.sparkline(values),
                "findings": [f.as_dict() for f in rep.findings[:3]],
            }
        )

    all_labels = [_label(item[1]) for item in analysed]
    bad_labels = [_label(item[1]) for item in flagged if item[3].score >= sensitivity]
    pairs = [(item[0], f) for item in flagged for f in item[3].findings]
    result = {
        "status": "success",
        "kind": kind,
        "query": query,
        "window": {"start": anomaly.iso(start), "end": anomaly.iso(end), "step_seconds": step},
        "sensitivity": sensitivity,
        "series_analyzed": len(analysed),
        "series_with_anomalies": len(flagged),
        "anomalous_series": described,
        "regular_cycles": _cycles(cycling),
        "patterns": anomaly.label_concentration(all_labels, bad_labels),
        "simultaneous": anomaly.time_clusters(pairs, window_s=step * 5),
    }
    notes = []
    if not series:
        notes.append("The query returned no series in this window. Check the metric name, labels, and time range.")
    if insufficient:
        notes.append(f"{insufficient} series had fewer than {anomaly.MIN_POINTS} samples and were skipped.")
    if truncated:
        notes.append(f"Only the first {MAX_SERIES_ANALYZED} series were analysed. Narrow the query with a label filter.")
    if analysed and not flagged:
        notes.append("Nothing unusual found at this sensitivity.")
    if len(flagged) > len(described):
        notes.append(f"{len(flagged) - len(described)} more anomalous series not shown. Raise max_series to see them.")
    if notes:
        result["notes"] = notes
    return result
