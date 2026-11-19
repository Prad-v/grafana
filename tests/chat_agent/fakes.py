"""Test doubles shared by the chat agent tests: fake metrics/logs backends and a scripted model."""

import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

NOW = 1_800_000_000  # a fixed clock keeps every window deterministic
STEP = 60


def gpu_series(region, dc, node, gpu, fault=False, seed=0):
    """One GPU temperature series over three hours. A `fault` heats it from minute 120."""
    rng = random.Random(f"{node}-{gpu}-{seed}")
    start = NOW - 180 * STEP
    values = []
    for i in range(180):
        v = 62 + rng.gauss(0, 0.7)
        if fault and i >= 120:
            v += 18
        values.append([start + i * STEP, f"{v:.3f}"])
    return {
        "metric": {"__name__": "DCGM_FI_DEV_GPU_TEMP", "region": region, "datacenter": dc, "node": node, "gpu": str(gpu)},
        "values": values,
    }


def fleet(hot_dc="eu1"):
    out = []
    for region, dcs in (("us", ("us1", "us2")), ("eu", ("eu1", "eu2"))):
        for dc in dcs:
            for n in range(2):
                for g in range(2):
                    out.append(gpu_series(region, dc, f"{dc}-n{n}", g, fault=(dc == hot_dc)))
    return out


class FakeBackend:
    """Serves the two HTTP APIs the agent calls: Prometheus query_range and VictoriaLogs hits."""

    def __init__(self):
        self.metric_series = fleet()
        self.log_hits = None
        self.requests = []
        self.metrics_status = 200
        self.metrics_body = None

        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                backend.requests.append(("GET", url.path, params))
                if url.path != "/api/v1/query_range":
                    return self._send(404, {})
                if backend.metrics_body is not None:
                    return self._send(backend.metrics_status, backend.metrics_body)
                self._send(200, {"status": "success", "data": {"resultType": "matrix", "result": backend.metric_series}})

            def do_POST(self):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length", 0))
                params = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                backend.requests.append(("POST", url.path, params))
                if url.path != "/select/logsql/hits":
                    return self._send(404, {})
                self._send(200, {"hits": backend.log_hits(params) if backend.log_hits else []})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def iso(ts):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_log_hits(params):
    """Error lines per minute: quiet, then a burst on one node. Follows the requested window."""
    step = int(params["step"].rstrip("s"))
    start, end = float(params["start"]), float(params["end"])
    field = params.get("field", "")
    stamps = []
    t = int(start // step) * step
    while t < end:
        stamps.append(t)
        t += step
    burst_from = NOW - 30 * STEP

    def group(node, base, burst):
        # Counts wobble between base-1 and base+1. Like VictoriaLogs, leave out buckets with no lines.
        values = [max((burst if ts >= burst_from else base) + (ts // step) % 3 - 1, 0) for ts in stamps]
        pairs = [(ts, v) for ts, v in zip(stamps, values) if v > 0]
        return {
            "fields": {field: node} if field else {},
            "timestamps": [iso(ts) for ts, _ in pairs],
            "values": [v for _, v in pairs],
            "total": sum(v for _, v in pairs),
        }

    if not field:
        return [group("", 1, 1)]
    return [group("quiet-node-a", 1, 1), group("quiet-node-b", 1, 1), group("noisy-node", 1, 60)]


class ScriptedLlm(BaseLlm):
    """A stand-in for Claude. It calls a tool when asked about anomalies, then summarises the result.

    It streams like a real model: partial text chunks, then one aggregated final response.
    """

    model: str = "scripted"
    calls: list = []

    @classmethod
    def supported_models(cls):
        return ["scripted"]

    async def generate_content_async(self, llm_request, stream=False):
        last = llm_request.contents[-1]
        user_text = " ".join(p.text or "" for c in llm_request.contents if c.role == "user" for p in c.parts if p.text)
        declared = [fd.name for tool in (llm_request.config.tools or []) for fd in (tool.function_declarations or [])]
        self.calls.append({"instruction": str(llm_request.config.system_instruction), "turns": len(llm_request.contents), "tools": declared})

        responded = any(p.function_response for p in last.parts)
        if responded:
            result = next(p.function_response for p in last.parts if p.function_response)
            reply = f"Result of {result.name}: " + ("an error" if result.response.get("status") == "error" else "ok")
            texts = [reply[: len(reply) // 2], reply[len(reply) // 2 :]]
        elif "break" in user_text:
            raise RuntimeError("boom with a secret: sk-ant-123")
        elif "anomal" in user_text:
            part = types.Part(function_call=types.FunctionCall(id="call-1", name="detect_metric_anomalies", args={"promql": "DCGM_FI_DEV_GPU_TEMP", "lookback_minutes": 180}))
            yield LlmResponse(content=types.Content(role="model", parts=[part]))
            return
        elif "bad query" in user_text:
            part = types.Part(function_call=types.FunctionCall(id="call-2", name="summarize_metric", args={"promql": ""}))
            yield LlmResponse(content=types.Content(role="model", parts=[part]))
            return
        else:
            texts = ["Hello ", "there."]

        if stream:
            for t in texts:
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]), partial=True)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="".join(texts))]))
