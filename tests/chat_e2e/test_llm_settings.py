"""End-to-end check of the model settings: the choice saved in Grafana reaches the agent and the model.

Needs `make stack-up`; run it with `make chat-e2e`. A stand-in LiteLLM proxy runs in this container
and the agent is pointed at it through Grafana's real settings API and plugin proxy, so this covers
the whole path: settings storage, the proxy route's headers, the agent, and the model client.
The tests restore the default settings when they finish. Standard library only.
"""

import base64
import json
import os
import unittest
import urllib.error
import urllib.request

from fake_openai import FakeOpenAI
from test_chat_stack import GRAFANA, PROXY, TOKEN, as_admin, call, parse_sse

SETTINGS = f"{GRAFANA}/api/plugins/kekaflow-chat-app/settings"
PROXY_NAME = "fake-llm"  # the alias this container has on the stack's network
PROXY_PORT = 4000
BASE_URL = f"http://{PROXY_NAME}:{PROXY_PORT}"
KEY = "e2e-virtual-key-7f3a"
VIEWER = {"name": "Chat E2E Viewer", "login": "chat-e2e-viewer", "email": "chat-e2e-viewer@example.com", "password": "Viewer-pass-123!"}


def basic(login, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()}


def current_settings():
    status, _, body = call("GET", SETTINGS, as_admin())
    assert status == 200, body
    return json.loads(body)


def save(json_data, secure=None, headers=None):
    body = {"enabled": True, "pinned": False, "jsonData": json_data}
    if secure is not None:
        body["secureJsonData"] = secure
    return call("POST", SETTINGS, headers or as_admin(), body)


def litellm_settings(**overrides):
    base = {"agentUrl": "http://chat-agent:8080", "llmProvider": "litellm", "llmBaseUrl": BASE_URL, "llmModel": "gpt-fake", "llmKeyCleared": False}
    base.update(overrides)
    return base


DEFAULT = {"agentUrl": "http://chat-agent:8080", "llmProvider": "", "llmBaseUrl": "", "llmModel": "", "llmKeyCleared": True}


def proxy_config(headers=None):
    status, _, body = call("GET", f"{PROXY}/config", headers or as_admin())
    return status, json.loads(body)


class ModelSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeOpenAI(required_key=KEY, host="0.0.0.0", port=PROXY_PORT).start()
        status, _, body = call("POST", f"{GRAFANA}/api/admin/users", as_admin(), VIEWER)
        assert status in (200, 412), body  # 412: left over from an interrupted run

    @classmethod
    def tearDownClass(cls):
        save(DEFAULT, {"llmApiKey": ""})
        cls.fake.stop()
        status, _, body = call("GET", f"{GRAFANA}/api/users/lookup?loginOrEmail={VIEWER['login']}", as_admin())
        if status == 200:
            call("DELETE", f"{GRAFANA}/api/admin/users/{json.loads(body)['id']}", as_admin())

    def setUp(self):
        status, _, body = save(DEFAULT, {"llmApiKey": ""})
        self.assertEqual(status, 200, body)
        self.fake.requests.clear()

    def configure(self, **overrides):
        status, _, body = save(litellm_settings(**overrides), {"llmApiKey": KEY})
        self.assertEqual(status, 200, body)

    # --- what the settings page does -------------------------------------------------------------

    def test_without_settings_the_agent_uses_its_own_configuration(self):
        status, config = proxy_config()
        self.assertEqual(status, 200)
        self.assertEqual(config["llm"]["source"], "environment")

    def test_saved_settings_reach_the_agent_and_never_come_back_with_the_key(self):
        self.configure()
        status, config = proxy_config()
        self.assertEqual(status, 200)
        self.assertEqual(config["llm"], {"source": "settings", "provider": "litellm", "model": "gpt-fake", "host": f"{PROXY_NAME}:{PROXY_PORT}"})
        self.assertEqual(config["model"], "gpt-fake")

        # Neither the agent's answer nor Grafana's settings API ever returns the key.
        self.assertNotIn(KEY, json.dumps(config))
        stored = current_settings()
        self.assertNotIn(KEY, json.dumps(stored))
        self.assertTrue(stored["secureJsonFields"]["llmApiKey"])
        self.assertTrue(stored["secureJsonFields"]["agentToken"], "saving the model settings must not drop the agent token")

    def test_the_connection_test_uses_the_saved_key_and_model(self):
        self.configure()
        status, _, body = call("POST", f"{PROXY}/llm/test", as_admin())
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertTrue(result["ok"], result)
        post = self.fake.posts()[0]
        self.assertEqual(post["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(post["body"]["model"], "gpt-fake")
        self.assertNotIn(KEY, body)

    def test_a_wrong_key_is_reported_in_words(self):
        status, _, _ = save(litellm_settings(), {"llmApiKey": "not-the-key"})
        self.assertEqual(status, 200)
        _, _, body = call("POST", f"{PROXY}/llm/test", as_admin())
        result = json.loads(body)
        self.assertFalse(result["ok"])
        self.assertIn("rejected the API key", result["error"])
        self.assertNotIn("not-the-key", body)

    def test_models_are_listed_from_the_proxy(self):
        self.configure()
        _, _, body = call("GET", f"{PROXY}/llm/models", as_admin())
        self.assertEqual(json.loads(body)["models"], ["gpt-fake", "team/claude-alias"])

    def test_clearing_the_key_really_stops_sending_it(self):
        self.configure()
        status, _, body = save(litellm_settings(llmKeyCleared=True), {"llmApiKey": ""})
        self.assertEqual(status, 200, body)
        self.fake.requests.clear()
        _, _, body = call("POST", f"{PROXY}/llm/test", as_admin())
        self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(self.fake.posts()[0]["headers"]["authorization"], "Bearer none")

    def test_saving_without_a_key_keeps_the_stored_one(self):
        self.configure()
        status, _, body = save(litellm_settings(llmModel="gpt-fake"))
        self.assertEqual(status, 200, body)
        _, _, body = call("POST", f"{PROXY}/llm/test", as_admin())
        self.assertTrue(json.loads(body)["ok"])

    # --- a real question ---------------------------------------------------------------------------

    def test_a_question_is_answered_by_the_model_from_the_settings(self):
        self.configure()
        status, headers, body = call("POST", f"{PROXY}/chat", as_admin(), {"message": "How many GPUs are there?", "session_id": "e2e-llm-0001"})
        self.assertEqual(status, 200, body)
        events = parse_sse(body)
        self.assertEqual(events[0][0], "session")
        self.assertEqual(events[-1][0], "done")
        self.assertNotIn("error", [name for name, _ in events], body)
        self.assertEqual("".join(d["delta"] for n, d in events if n == "text"), "Hello from the fake model.")

        post = self.fake.posts()[-1]
        self.assertEqual(post["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(post["body"]["model"], "gpt-fake")
        tools = {t["function"]["name"] for t in post["body"]["tools"]}
        self.assertIn("detect_metric_anomalies", tools, "the agent's own tools must reach the model")
        self.assertIn("query_prometheus", tools, "so must the Grafana tools from mcp-grafana")
        self.assertNotIn(KEY, body)

    # --- who may do what ----------------------------------------------------------------------------

    def test_a_viewer_cannot_change_the_settings(self):
        status, _, _ = save(litellm_settings(), {"llmApiKey": "attacker"}, headers=basic(VIEWER["login"], VIEWER["password"]))
        self.assertIn(status, (401, 403))
        self.assertEqual(current_settings()["jsonData"].get("llmProvider", ""), "")

    def test_a_viewer_can_chat_with_the_settings_an_admin_chose(self):
        self.configure()
        status, config = proxy_config(basic(VIEWER["login"], VIEWER["password"]))
        self.assertEqual(status, 200)
        self.assertEqual(config["llm"]["source"], "settings")
        self.assertNotIn(KEY, json.dumps(config))

    def test_a_browser_cannot_supply_its_own_model_headers(self):
        injected = {**basic(VIEWER["login"], VIEWER["password"]), "X-LLM-Provider": "litellm", "X-LLM-Base-Url": BASE_URL, "X-LLM-Model": "attacker-model", "X-LLM-Api-Key": KEY}
        status, config = proxy_config(injected)
        self.assertEqual(status, 200)
        self.assertEqual(config["llm"]["source"], "environment", "headers set by the browser must be replaced by the saved settings")
        self.assertEqual(self.fake.requests, [])

    # --- the address allowlist ------------------------------------------------------------------------

    def test_an_address_that_is_not_allowed_is_refused_and_never_called(self):
        self.configure(llmBaseUrl="http://169.254.169.254")
        status, config = proxy_config()
        self.assertEqual(status, 200, "a bad setting must not take the page down")
        self.assertIn("not allowed", config["llm"]["error"])
        self.assertIn("CHAT_AGENT_ALLOWED_LLM_HOSTS", config["llm"]["error"])

        status, _, body = call("POST", f"{PROXY}/chat", as_admin(), {"message": "hi", "session_id": "e2e-llm-0002"})
        self.assertEqual(status, 400)
        self.assertIn("not allowed", json.loads(body)["error"])
        status, _, _ = call("POST", f"{PROXY}/llm/test", as_admin())
        self.assertEqual(status, 400)
        self.assertEqual(self.fake.requests, [])

    def test_anthropic_ignores_a_stored_proxy_address(self):
        status, _, body = save(litellm_settings(llmProvider="anthropic", llmBaseUrl="http://169.254.169.254", llmModel="claude-sonnet-5-5"), {"llmApiKey": "sk-ant-fake"})
        self.assertEqual(status, 200, body)
        status, config = proxy_config()
        self.assertEqual(status, 200)
        self.assertEqual(config["llm"], {"source": "settings", "provider": "anthropic", "model": "claude-sonnet-5-5"})


if __name__ == "__main__":
    unittest.main()
