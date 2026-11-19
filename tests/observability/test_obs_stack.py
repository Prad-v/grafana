"""End-to-end check of the observability stack against the running containers.

Needs `make obs-up` (or `make stack-up`) first, so run it with `make obs-test`
(or `make stack-test`, which also checks Grafana and mcp-grafana).

It injects faults into the live simulator and removes them again, so it is safe
to run next to a stack you are looking at, but the faults will be visible for a
minute. Standard library only.
"""

import base64
import json
import os
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

SIM = os.environ["SIM_URL"].rstrip("/")
VM = os.environ["VICTORIAMETRICS_URL"].rstrip("/")
VL = os.environ["VICTORIALOGS_URL"].rstrip("/")
GRAFANA = os.environ.get("GRAFANA_INTERNAL_URL", "").rstrip("/")
MCP = os.environ.get("MCP_GRAFANA_URL", "").rstrip("/")
GRAFANA_AUTH = base64.b64encode(
    f"{os.environ.get('GF_SECURITY_ADMIN_USER', 'admin')}:{os.environ.get('GF_SECURITY_ADMIN_PASSWORD', 'admin')}".encode()
).decode()

SHARED_LABELS = ("region", "availability_zone", "datacenter", "cluster", "rack", "node")
REGIONS = {"us-east-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-south-1", "ap-northeast-1"}


def http(method, url, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def sim(method, path, body=None):
    return json.loads(http(method, SIM + path, body))


def vm_query(expr):
    out = json.loads(http("GET", f"{VM}/api/v1/query?" + urllib.parse.urlencode({"query": expr})))
    assert out["status"] == "success", out
    return out["data"]["result"]


def vl_query(expr, limit=50):
    req = urllib.request.Request(
        f"{VL}/select/logsql/query",
        data=urllib.parse.urlencode({"query": expr, "limit": limit}).encode(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def eventually(fn, what, timeout=120, interval=3):
    """Poll until fn() returns something truthy; fail with the last error otherwise."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
            last = "condition not met yet"
        except (urllib.error.URLError, OSError, AssertionError, KeyError) as exc:
            last = repr(exc)
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}: {last}")


class ObservabilityStackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        eventually(lambda: sim("GET", "/healthz")["status"] == "ok", "the simulator to be healthy", 60)
        sim("DELETE", "/api/faults")
        cls.node = sim("GET", "/api/topology")[0]["datacenters"][0]["nodes"][0]
        cls.region = sim("GET", "/api/topology")[0]["name"]

    @classmethod
    def tearDownClass(cls):
        sim("DELETE", "/api/faults")

    # ---------------------------------------------------------------- metrics

    def test_metrics_for_every_region_reach_victoriametrics(self):
        def regions():
            found = {r["metric"]["region"] for r in vm_query("count by (region) (DCGM_FI_DEV_GPU_UTIL)")}
            return found >= REGIONS and found

        eventually(regions, "all regions to appear in VictoriaMetrics")
        # Polled, because a GPU restored by another test only returns on the next scrape.
        total = sim("GET", "/api/state")["topology"]["total_gpus"]
        eventually(
            lambda: int(vm_query("count(DCGM_FI_DEV_GPU_UTIL)")[0]["value"][1]) == total,
            f"VictoriaMetrics to hold exactly {total} GPU series",
        )

    def test_simulator_own_metrics_are_scraped(self):
        eventually(lambda: vm_query('gpusim_gpus{state="total"}'), "the gpu-simulator job to be scraped")
        eventually(lambda: vm_query('up{job="dcgm-exporter"} == 1'), "the dcgm-exporter target to be up")

    # ------------------------------------------------------------------- logs

    def test_logs_reach_victorialogs_with_metric_labels(self):
        rows = eventually(lambda: vl_query(f'_time:10m node:"{self.node}"', 5), "logs of a node in VictoriaLogs")
        row = rows[0]
        for key in (*SHARED_LABELS, "app", "level", "_msg", "_time"):
            self.assertIn(key, row)
        series = eventually(
            lambda: vm_query(f'DCGM_FI_DEV_GPU_UTIL{{node="{self.node}"}}'), "metrics of the same node"
        )
        for key in SHARED_LABELS:
            self.assertEqual(series[0]["metric"][key], row[key], f"label {key} differs between metrics and logs")

    # ----------------------------------------------------------------- faults

    def test_xid_fault_shows_up_in_metrics_and_logs(self):
        fault = sim("POST", "/api/faults", {"kind": "xid", "scope": {"node": self.node}, "duration_s": 120, "intensity": 1.0, "params": {"xid": 48}})
        self.addCleanup(lambda: sim("DELETE", f"/api/faults/{fault['id']}") if _alive(fault["id"]) else None)
        logs = eventually(lambda: vl_query(f'_time:5m node:"{self.node}" app:kernel xid:48', 5), "the Xid 48 kernel log")
        self.assertIn("Xid", logs[0]["_msg"])
        self.assertEqual(logs[0]["level"], "error")
        eventually(
            lambda: vm_query(f'DCGM_FI_DEV_XID_ERRORS{{node="{self.node}"}} == 48'), "DCGM_FI_DEV_XID_ERRORS to report 48"
        )

    def test_gpu_lost_removes_the_series(self):
        scope = {"node": self.node, "gpu": 5}
        selector = f'DCGM_FI_DEV_GPU_UTIL{{node="{self.node}",gpu="5"}}'
        eventually(lambda: vm_query(selector), "the GPU to be present before the fault")
        fault = sim("POST", "/api/faults", {"kind": "gpu_lost", "scope": scope, "duration_s": 90})
        self.addCleanup(lambda: sim("DELETE", f"/api/faults/{fault['id']}") if _alive(fault["id"]) else None)
        eventually(lambda: not vm_query(selector), "the lost GPU's series to disappear", 90)
        eventually(lambda: vl_query(f'_time:5m node:"{self.node}" xid:79', 3), "the Xid 79 log")

    def test_log_burst(self):
        marker = f"e2e-burst-{int(time.time())}"
        sim("POST", "/api/logs/burst", {"count": 20, "level": "warn", "app": "kernel", "scope": {"node": self.node}, "message": marker})
        rows = eventually(lambda: vl_query(f'"{marker}"', 50), "burst lines in VictoriaLogs")
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(r["node"] == self.node and r["level"] == "warn" for r in rows))

    # ---------------------------------------------------------------- grafana

    def _grafana(self, path):
        if not GRAFANA:
            self.skipTest("GRAFANA_INTERNAL_URL not set; run `make stack-test` for the Grafana checks")
        return json.loads(http("GET", GRAFANA + path, headers={"Authorization": f"Basic {GRAFANA_AUTH}"}))

    def test_grafana_datasources_are_provisioned_and_healthy(self):
        sources = {d["uid"]: d for d in self._grafana("/api/datasources")}
        self.assertEqual(sources["victoriametrics"]["type"], "victoriametrics-metrics-datasource")
        self.assertEqual(sources["victoriametrics-prom"]["type"], "prometheus")
        self.assertEqual(sources["victorialogs"]["type"], "victoriametrics-logs-datasource")
        for uid in ("victoriametrics", "victoriametrics-prom"):
            health = eventually(lambda uid=uid: self._grafana(f"/api/datasources/uid/{uid}/health"), f"{uid} health")
            self.assertEqual(health["status"], "OK")

    def test_grafana_queries_metrics_through_the_datasource(self):
        query = urllib.parse.urlencode({"query": "count(DCGM_FI_DEV_GPU_UTIL)"})
        for uid in ("victoriametrics", "victoriametrics-prom"):
            out = eventually(
                lambda uid=uid: self._grafana(f"/api/datasources/proxy/uid/{uid}/api/v1/query?{query}")["data"]["result"],
                f"Grafana to query VictoriaMetrics through {uid}",
            )
            self.assertGreater(int(out[0]["value"][1]), 0)

    def test_dashboard_panels_use_the_victoriametrics_plugin(self):
        dash = self._grafana("/api/dashboards/uid/gpu-fleet")["dashboard"]
        types = {p["datasource"]["type"] for p in dash["panels"] if p.get("datasource", {}).get("uid") == "victoriametrics"}
        self.assertEqual(types, {"victoriametrics-metrics-datasource"})

    def test_prometheus_twin_dashboard_has_the_same_panels(self):
        plugin = self._grafana("/api/dashboards/uid/gpu-fleet")["dashboard"]
        twin = self._grafana("/api/dashboards/uid/gpu-fleet-prom")["dashboard"]
        self.assertEqual([p["title"] for p in twin["panels"]], [p["title"] for p in plugin["panels"]])
        metric_sources = {
            (p["datasource"]["type"], p["datasource"]["uid"])
            for p in twin["panels"]
            if p.get("datasource", {}).get("uid", "").startswith("victoriametrics")
        }
        self.assertEqual(metric_sources, {("prometheus", "victoriametrics-prom")})

    def test_grafana_has_the_gpu_dashboard(self):
        found = eventually(lambda: self._grafana("/api/search?query=GPU%20fleet%20overview"), "the provisioned dashboard")
        self.assertEqual({d["uid"] for d in found}, {"gpu-fleet", "gpu-fleet-prom"})
        dash = self._grafana("/api/dashboards/uid/gpu-fleet")["dashboard"]
        self.assertGreaterEqual(len(dash["panels"]), 10)

    # ------------------------------------------------------------ mcp-grafana

    def test_mcp_grafana_is_healthy_and_serves_tools(self):
        if not MCP:
            self.skipTest("MCP_GRAFANA_URL not set; run `make stack-test` for the mcp-grafana checks")
        self.assertIn(b"ok", http("GET", MCP + "/healthz").lower())
        headers = {"Accept": "application/json, text/event-stream"}
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}},
        }
        req = urllib.request.Request(MCP + "/mcp", data=json.dumps(init).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=15) as resp:
            session = resp.headers.get("Mcp-Session-Id")
            body = resp.read().decode()
        self.assertIn("serverInfo", body)
        session_headers = {**headers, **({"Mcp-Session-Id": session} if session else {})}
        http("POST", MCP + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_headers)
        tools = http("POST", MCP + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_headers).decode()
        self.assertIn("query_prometheus", tools)
        self.assertIn("search_dashboards", tools)


def _alive(fault_id):
    return any(f["id"] == fault_id for f in sim("GET", "/api/state")["faults"])


if __name__ == "__main__":
    unittest.main()
