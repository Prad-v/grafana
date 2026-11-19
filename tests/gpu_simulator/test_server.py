"""HTTP API and log shipper tests. Both run against real local sockets, no mocks."""

import json
import random
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from gpusim.engine import Fleet
from gpusim.logs import STREAM_FIELDS, LogShipper
from gpusim.server import make_server

from test_engine import Clock, run


class Sink:
    """A stand-in for VictoriaLogs that records what it receives."""

    def __init__(self, status=204):
        self.requests = []
        self.status = status
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                sink.requests.append((urlparse(self.path), parse_qs(urlparse(self.path).query), body))
                self.send_response(sink.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ShipperTest(unittest.TestCase):
    def test_posts_json_lines_with_stream_fields(self):
        sink = Sink()
        self.addCleanup(sink.close)
        shipper = LogShipper(sink.url)
        records = [{"_time": "2026-01-01T00:00:00.000Z", "_msg": f"m{i}", "level": "info", "app": "kernel"} for i in range(3)]
        for r in records:
            shipper.push(r)
        self.assertTrue(shipper.flush())
        (url, query, body), = sink.requests
        self.assertEqual(url.path, "/insert/jsonline")
        self.assertEqual(query["_stream_fields"], [STREAM_FIELDS])
        self.assertEqual(query["_msg_field"], ["_msg"])
        self.assertEqual(query["_time_field"], ["_time"])
        self.assertEqual([json.loads(line) for line in body.decode().splitlines()], records)
        self.assertEqual(shipper.shipped, 3)
        self.assertEqual(shipper.queue_length(), 0)

    def test_failed_push_keeps_records_in_order_for_retry(self):
        sink = Sink(status=500)
        self.addCleanup(sink.close)
        shipper = LogShipper(sink.url)
        for i in range(3):
            shipper.push({"_msg": f"m{i}"})
        self.assertFalse(shipper.flush())
        self.assertEqual(shipper.errors, 1)
        self.assertEqual([r["_msg"] for r in shipper.queue], ["m0", "m1", "m2"])
        sink.status = 204
        self.assertTrue(shipper.flush())
        self.assertEqual(shipper.shipped, 3)

    def test_unreachable_sink_is_survivable_and_bounded(self):
        shipper = LogShipper("http://127.0.0.1:1", max_queue=5)
        for i in range(8):
            shipper.push({"_msg": f"m{i}"})
        self.assertEqual(shipper.dropped, 3)
        self.assertFalse(shipper.flush())
        self.assertEqual(shipper.queue_length(), 5)
        self.assertTrue(shipper.last_error)

    def test_empty_url_disables_shipping(self):
        shipper = LogShipper("")
        shipper.push({"_msg": "x"})
        self.assertEqual(shipper.queue_length(), 0)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = Clock()
        cls.fleet = Fleet(2, 2, rng=random.Random(3), clock=cls.clock)
        cls.shipper = LogShipper("")
        cls.server = make_server(cls.fleet, cls.shipper, "127.0.0.1", 0, {"Grafana": "http://localhost:3000"})
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.fleet.reset()

    def call(self, method, path, body=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, (payload.decode() if raw else json.loads(payload))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_ui_and_health(self):
        status, html = self.call("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("GPU Fleet Simulator", html)
        self.assertEqual(self.call("GET", "/healthz"), (200, {"status": "ok"}))

    def test_metrics_endpoints(self):
        run(self.fleet, self.clock, 2)
        status, text = self.call("GET", "/metrics", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("DCGM_FI_DEV_GPU_UTIL{", text)
        status, text = self.call("GET", "/metrics?region=eu-west-1", raw=True)
        self.assertNotIn('region="us-east-1"', text)
        self.assertEqual(self.call("GET", "/metrics?region=mars-1")[0], 404)
        status, text = self.call("GET", "/sim/metrics", raw=True)
        self.assertIn('gpusim_gpus{state="total"} 48', text)

    def test_state_and_meta(self):
        _, state = self.call("GET", "/api/state")
        self.assertEqual(state["topology"]["total_gpus"], 6 * 2 * 2 * 2)
        self.assertEqual(len(state["regions"]), 6)
        self.assertIn("shipper", state)
        _, meta = self.call("GET", "/api/meta")
        self.assertIn("xid", meta["fault_kinds"])
        self.assertEqual(meta["links"]["Grafana"], "http://localhost:3000")
        _, topo = self.call("GET", "/api/topology")
        self.assertEqual(len(topo), 6)

    def test_fault_lifecycle(self):
        status, created = self.call("POST", "/api/faults", {"kind": "thermal", "scope": {"region": "eu-west-1"}, "duration_s": 300, "intensity": 0.8})
        self.assertEqual(status, 200)
        _, state = self.call("GET", "/api/state")
        self.assertEqual([f["id"] for f in state["faults"]], [created["id"]])
        self.assertEqual(state["faults"][0]["scope"], {"region": "eu-west-1"})
        self.assertEqual(self.call("DELETE", f"/api/faults/{created['id']}"), (200, {"removed": 1}))
        self.assertEqual(self.call("DELETE", f"/api/faults/{created['id']}")[0], 404)
        self.call("POST", "/api/faults", {"kind": "ecc"})
        self.assertEqual(self.call("DELETE", "/api/faults"), (200, {"removed": 1}))

    def test_bad_requests_are_400_not_500(self):
        for method, path, body in (
            ("POST", "/api/faults", {"kind": "nope"}),
            ("POST", "/api/faults", {"kind": "thermal", "scope": {"region": "mars-1"}}),
            ("POST", "/api/faults", {"kind": "thermal", "intensity": "max"}),
            ("PUT", "/api/settings", {"intensity": 99}),
            ("PUT", "/api/regions/us-east-1", {"load": -1}),
            ("PUT", "/api/topology", {"nodes_per_dc": 0}),
            ("POST", "/api/logs/burst", {"count": 0}),
            ("POST", "/api/presets/nope", {}),
        ):
            with self.subTest(method=method, path=path, body=body):
                status, payload = self.call(method, path, body)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)

    def test_not_found_and_invalid_json(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("PUT", "/api/regions/mars-1", {})[0], 404)
        req = urllib.request.Request(self.base + "/api/settings", data=b"{not json", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_settings_region_burst_and_logs(self):
        self.assertEqual(self.call("PUT", "/api/settings", {"log_rate": 2.5})[1]["log_rate"], 2.5)
        status, region = self.call("PUT", "/api/regions/us-east-1", {"enabled": False, "load": 0.5})
        self.assertEqual((status, region["enabled"], region["load"]), (200, False, 0.5))
        self.assertEqual(self.call("POST", "/api/logs/burst", {"count": 7, "level": "warn", "message": "hello"}), (200, {"emitted": 7}))
        _, logs = self.call("GET", "/api/logs?limit=5&level=warn")
        self.assertEqual(len(logs), 5)
        self.assertTrue(all(r["_msg"] == "hello" for r in logs))
        self.assertEqual(self.call("GET", "/api/logs?limit=x")[0], 400)

    def test_preset_and_reset(self):
        status, result = self.call("POST", "/api/presets/heatwave")
        self.assertEqual(status, 200)
        self.assertEqual(result["applied"], "heatwave")
        self.assertEqual(len(self.call("GET", "/api/state")[1]["faults"]), 1)
        self.call("POST", "/api/reset")
        self.assertEqual(self.call("GET", "/api/state")[1]["faults"], [])

    def test_resize(self):
        status, topo = self.call("PUT", "/api/topology", {"nodes_per_dc": 3})
        self.assertEqual((status, topo["total_gpus"]), (200, 6 * 2 * 3 * 2))
        self.assertEqual(self.call("PUT", "/api/topology", {"nodes_per_dc": 2})[1]["total_gpus"], 48)


if __name__ == "__main__":
    unittest.main()
