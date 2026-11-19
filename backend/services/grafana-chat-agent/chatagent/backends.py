"""Thin async clients for the Prometheus-compatible and VictoriaLogs HTTP APIs."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import httpx

from .anomaly import Point

# VictoriaMetrics rejects more than 30000 points per series; stay well below.
MAX_POINTS = 1000
MIN_STEP_S = 5


class BackendError(Exception):
    """A query the backend rejected or could not answer. The message is safe to show the model."""


def parse_time(value: str, default: float) -> float:
    """Parse an ISO 8601 time or a unix timestamp. Empty means ``default``."""
    value = (value or "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackendError(f"Cannot parse time {value!r}. Use ISO 8601, for example 2026-01-31T14:00:00Z.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def pick_step(window_s: float, requested_s: int = 0, target_points: int = 180) -> int:
    """A step that gives about ``target_points`` samples, rounded to a readable number."""
    if requested_s and requested_s > 0:
        step = requested_s
    else:
        step = math.ceil(window_s / target_points)
    for nice in (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400):
        if step <= nice:
            step = nice
            break
    else:
        step = math.ceil(step / 86400) * 86400
    # Never ask for more points than the backend will return.
    return max(MIN_STEP_S, step, math.ceil(window_s / MAX_POINTS))


def resolve_window(lookback_minutes: int, end_time: str, now: float) -> tuple[float, float]:
    if lookback_minutes < 1 or lookback_minutes > 60 * 24 * 90:
        raise BackendError("lookback_minutes must be between 1 and 129600 (90 days).")
    end = parse_time(end_time, now)
    return end - lookback_minutes * 60, end


class Backends:
    def __init__(self, metrics_url: str, logs_url: str, timeout_s: float = 30.0, client: httpx.AsyncClient | None = None):
        self.metrics_url = metrics_url
        self.logs_url = logs_url
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def metrics_range(self, promql: str, start: float, end: float, step_s: int) -> list[tuple[dict[str, str], list[Point]]]:
        """Run a range query. Returns ``(labels, points)`` per series."""
        try:
            resp = await self._client.get(
                f"{self.metrics_url}/api/v1/query_range",
                params={"query": promql, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step_s}s"},
            )
        except httpx.HTTPError as exc:
            raise BackendError(f"Cannot reach the metrics backend at {self.metrics_url}: {exc.__class__.__name__}") from exc
        body = _json(resp)
        if resp.status_code >= 400 or body.get("status") != "success":
            raise BackendError(f"The metrics backend rejected the query: {body.get('error') or resp.text[:300]}")
        data = body.get("data", {})
        if data.get("resultType") != "matrix":
            raise BackendError(f"Expected a range vector, got {data.get('resultType')!r}. Use an expression that returns a series.")
        out = []
        for item in data.get("result", []):
            points = [(float(t), float(v)) for t, v in item.get("values", []) if _is_number(v)]
            out.append((dict(item.get("metric", {})), points))
        return out

    async def log_hits(self, logsql: str, start: float, end: float, step_s: int, field: str = "") -> list[tuple[dict[str, str], list[Point]]]:
        """Count log lines per time bucket, optionally split by one field."""
        data = {"query": logsql, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step_s}s"}
        if field:
            data["field"] = field
        try:
            resp = await self._client.post(f"{self.logs_url}/select/logsql/hits", data=data)
        except httpx.HTTPError as exc:
            raise BackendError(f"Cannot reach the logs backend at {self.logs_url}: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise BackendError(f"The logs backend rejected the query: {resp.text[:300].strip()}")
        body = _json(resp)
        out = []
        for hit in body.get("hits", []):
            points = []
            for stamp, count in zip(hit.get("timestamps", []), hit.get("values", [])):
                points.append((datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp(), float(count)))
            out.append((dict(hit.get("fields", {})), points))
        return out


def fill_missing_buckets(points: list[Point], start: float, end: float, step_s: int) -> list[Point]:
    """VictoriaLogs omits empty buckets. For counts, a missing bucket is a real zero."""
    have = {int(t // step_s): v for t, v in points}
    first, last = int(start // step_s), int(end // step_s)
    return [((b * step_s), have.get(b, 0.0)) for b in range(first, last + 1)]


def _is_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
