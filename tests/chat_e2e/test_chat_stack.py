"""End-to-end check of the chat assistant against the running stack.

Needs `make stack-up`, so run it with `make chat-e2e`. It talks to Grafana the way
the browser does (through the plugin proxy) and to the agent directly. It does not
call the model, so it passes with or without an ANTHROPIC_API_KEY. Standard library only.
"""

import base64
import json
import os
import re
import time
import unittest
import urllib.error
import urllib.request

GRAFANA = os.environ["GRAFANA_INTERNAL_URL"].rstrip("/")
AGENT = os.environ["CHAT_AGENT_INTERNAL_URL"].rstrip("/")
TOKEN = os.environ.get("CHAT_AGENT_TOKEN", "")
BASIC = base64.b64encode(f"{os.environ.get('GF_SECURITY_ADMIN_USER', 'admin')}:{os.environ.get('GF_SECURITY_ADMIN_PASSWORD', 'admin')}".encode()).decode()
PROXY = f"{GRAFANA}/api/plugin-proxy/kekaflow-chat-app/agent"

WRITE_TOOL = re.compile(r"^(create|update|delete|install|add|remove|import|patch|apply)_|_write$")


def call(method, url, headers=None, body=None, timeout=60):
    """Return (status, headers, text) without raising on HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read().decode()


def as_admin():
    return {"Authorization": f"Basic {BASIC}"}


def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        event, data = "message", []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            events.append((event, json.loads("\n".join(data))))
    return events


class ChatStackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TOKEN:
            raise AssertionError("CHAT_AGENT_TOKEN is empty; run this through `make chat-e2e`, which reads .env.")
        # Through Grafana, so this also waits for the proxy route to be usable.
        deadline = time.time() + 90
        status = None
        while time.time() < deadline:
            try:
                status = call("GET", f"{PROXY}/config", as_admin(), timeout=15)[0]
            except OSError:
                status = None
            if status == 200:
                return
            time.sleep(3)
        raise AssertionError(f"the agent did not become reachable through Grafana in 90s (last status {status})")

    def test_agent_is_healthy_without_a_token(self):
        status, _, body = call("GET", f"{AGENT}/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_agent_refuses_calls_without_the_right_token(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            status, _, _ = call("GET", f"{AGENT}/config", headers)
            self.assertEqual(status, 401, headers)
        status, _, _ = call("POST", f"{AGENT}/chat", {}, {"message": "hi"})
        self.assertEqual(status, 401)

    def test_plugin_is_installed_and_enabled_in_grafana(self):
        status, _, body = call("GET", f"{GRAFANA}/api/plugins/kekaflow-chat-app/settings", as_admin())
        self.assertEqual(status, 200, body)
        settings = json.loads(body)
        self.assertTrue(settings["enabled"], settings)
        self.assertEqual(settings["type"], "app")
        self.assertEqual(settings["jsonData"]["agentUrl"], "http://chat-agent:8080")
        # The token is stored encrypted and never returned.
        self.assertNotIn(TOKEN, body)

    def test_plugin_bundle_is_served(self):
        status, _, body = call("GET", f"{GRAFANA}/public/plugins/kekaflow-chat-app/module.js", as_admin())
        self.assertEqual(status, 200)
        self.assertIn("define(", body)

    def test_the_proxy_requires_a_grafana_login(self):
        status, _, _ = call("GET", f"{PROXY}/config")
        self.assertEqual(status, 401)

    def test_grafana_adds_the_token_so_the_browser_never_needs_it(self):
        status, _, body = call("GET", f"{PROXY}/config", as_admin())
        self.assertEqual(status, 200, body)
        self.assertIn("model", json.loads(body))

    def test_agent_is_connected_to_mcp_grafana_with_read_tools_only(self):
        status, _, body = call("GET", f"{PROXY}/config", as_admin())
        self.assertEqual(status, 200, body)
        config = json.loads(body)
        mcp = config["mcp"]
        self.assertTrue(mcp["connected"], mcp)
        names = set(mcp["names"])
        for expected in ("query_prometheus", "list_datasources", "search_dashboards", "alerting_rules_read"):
            self.assertIn(expected, names)
        self.assertEqual(sorted(n for n in names if WRITE_TOOL.search(n)), [], "write tools must be filtered out")
        self.assertFalse(config["allow_writes"])
        self.assertEqual(set(config["native_tools"]), {"summarize_metric", "detect_metric_anomalies", "detect_log_anomalies"})

    def test_a_chat_turn_streams_session_first_and_done_last(self):
        status, headers, body = call("POST", f"{PROXY}/chat", as_admin(), {"message": "How many GPUs are there?", "session_id": "e2e-stream-1"})
        self.assertEqual(status, 200, body)
        self.assertIn("text/event-stream", headers["Content-Type"])
        events = parse_sse(body)
        self.assertEqual(events[0], ("session", {"session_id": "e2e-stream-1"}))
        self.assertEqual(events[-1][0], "done")
        for name, data in events:
            self.assertIn(name, {"session", "tool_call", "tool_result", "text", "error", "done"})
            self.assertNotIn(TOKEN, json.dumps(data))
            self.assertNotIn("Traceback", json.dumps(data))

    def test_bad_requests_are_rejected_with_a_readable_error(self):
        status, _, body = call("POST", f"{PROXY}/chat", as_admin(), {"message": "   "})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        status, _, _ = call("POST", f"{PROXY}/chat", as_admin(), {"message": "hi", "session_id": "../etc"})
        self.assertEqual(status, 400)

    def test_a_conversation_can_be_reset(self):
        status, _, body = call("DELETE", f"{PROXY}/sessions/e2e-reset-12345", as_admin())
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
