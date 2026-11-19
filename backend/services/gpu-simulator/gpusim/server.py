"""HTTP API, Prometheus endpoints and the control UI."""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import logs as logmod
from .engine import FAULT_KINDS, PRESETS, SCOPE_KEYS, Fleet
from .exposition import render_dcgm, render_self
from .topology import REGIONS_BY_NAME

log = logging.getLogger("gpusim.http")

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
TEXT = "text/plain; version=0.0.4; charset=utf-8"
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def make_server(fleet: Fleet, shipper, host: str, port: int, links: dict | None = None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "gpu-simulator"

        def log_message(self, fmt, *args):  # keep scrape noise out of the container log
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---------------------------------------------------------------- plumbing

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "request body too large")
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ApiError(400, "body must be valid JSON") from None
            if not isinstance(data, dict):
                raise ApiError(400, "body must be a JSON object")
            return data

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                result = self._route(method, url.path.rstrip("/") or "/", query)
                if result is not None:
                    self._json(result)
            except ApiError as exc:
                self._json({"error": exc.message}, exc.status)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception:  # pragma: no cover - last resort so a bug is a 500, not a dropped connection
                log.exception("unhandled error on %s %s", method, self.path)
                self._json({"error": "internal error"}, 500)

        do_GET = lambda self: self._dispatch("GET")  # noqa: E731
        do_POST = lambda self: self._dispatch("POST")  # noqa: E731
        do_PUT = lambda self: self._dispatch("PUT")  # noqa: E731
        do_DELETE = lambda self: self._dispatch("DELETE")  # noqa: E731

        # ------------------------------------------------------------------ routes

        def _route(self, method: str, path: str, query: dict):
            if method == "GET":
                if path == "/":
                    return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/healthz":
                    return {"status": "ok"}
                if path == "/metrics":
                    region = query.get("region", "")
                    if region and region not in REGIONS_BY_NAME:
                        raise ApiError(404, f"unknown region {region}")
                    return self._send(200, render_dcgm(fleet, region).encode(), TEXT)
                if path == "/sim/metrics":
                    return self._send(200, render_self(fleet, shipper).encode(), TEXT)
                if path == "/api/state":
                    state = fleet.summary()
                    state["shipper"] = {
                        "enabled": bool(shipper.url),
                        "url": shipper.url,
                        "shipped": shipper.shipped,
                        "errors": shipper.errors,
                        "dropped": shipper.dropped,
                        "queue": shipper.queue_length(),
                        "last_error": shipper.last_error,
                    }
                    return state
                if path == "/api/meta":
                    return {
                        "fault_kinds": FAULT_KINDS,
                        "presets": PRESETS,
                        "scope_keys": list(SCOPE_KEYS),
                        "log_sources": list(logmod.SOURCES),
                        "log_levels": list(logmod.LEVELS),
                        "log_apps": sorted({a for apps in logmod.SOURCES.values() for a in apps}),
                        "links": links or {},
                    }
                if path == "/api/topology":
                    return fleet.topology()
                if path == "/api/logs":
                    try:
                        limit = max(1, min(500, int(query.get("limit", 100))))
                    except ValueError:
                        raise ApiError(400, "limit must be an integer") from None
                    return fleet.recent_logs(limit, query.get("region", ""), query.get("level", ""))
            elif method == "PUT":
                if path == "/api/settings":
                    fleet.settings.update(self._body())
                    return fleet.settings.as_dict()
                if path == "/api/topology":
                    body = self._body()
                    fleet.resize(
                        body.get("nodes_per_dc", fleet.nodes_per_dc),
                        body.get("gpus_per_node", fleet.gpus_per_node),
                    )
                    return fleet.summary()["topology"]
                match = re.fullmatch(r"/api/regions/([^/]+)", path)
                if match:
                    name = match.group(1)
                    if name not in fleet.regions:
                        raise ApiError(404, f"unknown region {name}")
                    fleet.regions[name].update(self._body())
                    return {"name": name, **vars(fleet.regions[name])}
            elif method == "POST":
                if path == "/api/faults":
                    body = self._body()
                    fault = fleet.add_fault(
                        body.get("kind", ""),
                        body.get("scope"),
                        body.get("duration_s", 0),
                        body.get("intensity", 0.7),
                        body.get("params"),
                    )
                    return {"id": fault.id}
                if path == "/api/logs/burst":
                    body = self._body()
                    sent = fleet.burst(
                        body.get("count", 100),
                        body.get("level", "error"),
                        body.get("app", ""),
                        body.get("scope"),
                        body.get("message", ""),
                    )
                    return {"emitted": sent}
                match = re.fullmatch(r"/api/presets/([^/]+)", path)
                if match:
                    if not _ID.match(match.group(1)):
                        raise ApiError(400, "invalid preset name")
                    return {"applied": match.group(1), "description": fleet.apply_preset(match.group(1))}
                if path == "/api/reset":
                    fleet.reset()
                    return {"status": "reset"}
            elif method == "DELETE":
                if path == "/api/faults":
                    return {"removed": fleet.clear_faults()}
                match = re.fullmatch(r"/api/faults/([^/]+)", path)
                if match:
                    if not fleet.remove_fault(match.group(1)):
                        raise ApiError(404, "no such fault")
                    return {"removed": 1}
            raise ApiError(404, "not found")

    return ThreadingHTTPServer((host, port), Handler)
