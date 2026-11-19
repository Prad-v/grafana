"""The chat agent's HTTP API, end to end through ADK with a scripted model.

Everything real except the model and the backends: ADK runs the agent loop, calls the
analysis tools, and the tools query a fake metrics server. Only Claude is replaced.
"""

import json
import unittest

from fastapi.testclient import TestClient

from chatagent import agent as agent_module
from chatagent.config import Settings
from chatagent.server import StreamState, classify_error, create_app, friendly_error, preview, translate

import fakes

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def parse_sse(text):
    """Parse an event stream into (event, data) pairs."""
    events = []
    for block in text.strip().split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = fakes.FakeBackend()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        self.llm = fakes.ScriptedLlm()
        self.llm.calls = []
        self.settings = Settings(token=TOKEN, mcp_enabled=False, metrics_url=self.fake.url, logs_url=self.fake.url, max_message_chars=200)
        self.client = TestClient(create_app(self.settings, model=self.llm))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def chat(self, message, session_id="session-0001", headers=None):
        resp = self.client.post("/chat", json={"message": message, "session_id": session_id}, headers={**AUTH, **(headers or {})})
        return resp, parse_sse(resp.text) if resp.status_code == 200 else []


class AuthTest(ServerTestCase):
    def test_health_needs_no_token(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_chat_without_a_token_is_rejected(self):
        resp = self.client.post("/chat", json={"message": "hi"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.llm.calls, [])

    def test_chat_with_a_wrong_token_is_rejected(self):
        resp = self.client.post("/chat", json={"message": "hi"}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)

    def test_a_token_of_a_different_scheme_is_rejected(self):
        resp = self.client.post("/chat", json={"message": "hi"}, headers={"Authorization": f"Basic {TOKEN}"})
        self.assertEqual(resp.status_code, 401)

    def test_config_and_reset_need_the_token_too(self):
        self.assertEqual(self.client.get("/config").status_code, 401)
        self.assertEqual(self.client.delete("/sessions/session-0001").status_code, 401)

    def test_refuses_to_start_without_a_token(self):
        with self.assertRaises(SystemExit):
            create_app(Settings(token="", mcp_enabled=False))

    def test_auth_can_be_disabled_explicitly_for_throwaway_runs(self):
        app = create_app(Settings(token="", disable_auth=True, mcp_enabled=False), model=self.llm)
        with TestClient(app) as client:
            self.assertEqual(client.get("/config").status_code, 200)


class ChatTest(ServerTestCase):
    def test_plain_answer_streams_in_pieces_without_duplicates(self):
        resp, events = self.chat("hello")
        self.assertEqual(resp.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(resp.headers["cache-control"], "no-cache, no-transform")
        names = [n for n, _ in events]
        self.assertEqual(names[0], "session")
        self.assertEqual(names[-1], "done")
        text = [d["delta"] for n, d in events if n == "text"]
        self.assertEqual(text, ["Hello ", "there."])  # the aggregated final copy is not sent again

    def test_a_session_id_is_created_when_none_is_given(self):
        resp = self.client.post("/chat", json={"message": "hello"}, headers=AUTH)
        events = parse_sse(resp.text)
        self.assertRegex(events[0][1]["session_id"], r"^[0-9a-f]{32}$")

    def test_the_agent_calls_a_tool_and_the_tool_reaches_the_backend(self):
        _, events = self.chat("any anomalies in temperature?")
        names = [n for n, _ in events]
        self.assertEqual(names, ["session", "tool_call", "tool_result", "text", "text", "done"])
        call = dict(events)["tool_call"]
        self.assertEqual(call["name"], "detect_metric_anomalies")
        self.assertEqual(call["args"]["promql"], "DCGM_FI_DEV_GPU_TEMP")
        result = dict(events)["tool_result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], call["id"])
        self.assertIn("eu1", result["preview"])
        self.assertEqual("".join(d["delta"] for n, d in events if n == "text"), "Result of detect_metric_anomalies: ok")
        self.assertEqual(self.fake.requests[-1][1], "/api/v1/query_range")

    def test_a_failing_tool_is_reported_as_failed_and_the_model_still_answers(self):
        _, events = self.chat("bad query please")
        result = dict(events)["tool_result"]
        self.assertFalse(result["ok"])
        self.assertIn("empty", result["preview"])
        self.assertIn("an error", "".join(d["delta"] for n, d in events if n == "text"))
        self.assertEqual(events[-1][0], "done")

    def test_model_failure_becomes_a_safe_error_event(self):
        resp, events = self.chat("please break")
        names = [n for n, _ in events]
        self.assertIn("error", names)
        self.assertEqual(names[-1], "done")
        self.assertNotIn("sk-ant-123", resp.text)
        self.assertNotIn("boom", resp.text)

    def test_follow_up_messages_keep_the_conversation(self):
        self.chat("hello")
        self.chat("hello again")
        turns = [c["turns"] for c in self.llm.calls]
        self.assertGreater(turns[1], turns[0])

    def test_a_different_user_does_not_share_the_conversation(self):
        self.chat("hello", headers={"X-Grafana-User": "alice"})
        self.chat("hello again", headers={"X-Grafana-User": "alice"})
        self.chat("hello", headers={"X-Grafana-User": "bob"})
        turns = [c["turns"] for c in self.llm.calls]
        self.assertEqual(turns[2], turns[0])

    def test_reset_forgets_the_conversation(self):
        self.chat("hello")
        self.assertEqual(self.client.delete("/sessions/session-0001", headers=AUTH).status_code, 200)
        self.chat("hello")
        self.assertEqual(self.llm.calls[0]["turns"], self.llm.calls[1]["turns"])

    def test_the_model_is_given_the_analysis_tools_and_the_clock(self):
        self.chat("hello")
        call = self.llm.calls[0]
        self.assertEqual(sorted(call["tools"]), ["detect_log_anomalies", "detect_metric_anomalies", "summarize_metric"])
        self.assertIn("Current time (UTC)", call["instruction"])
        self.assertIn("read-only access", call["instruction"])


class ValidationTest(ServerTestCase):
    def test_empty_message(self):
        resp, _ = self.chat("   ")
        self.assertEqual(resp.status_code, 400)

    def test_too_long_message(self):
        resp, _ = self.chat("x" * 201)
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.llm.calls, [])

    def test_bad_session_ids(self):
        for bad in ("short", "has space in it", "../../etc/passwd", "x" * 65):
            resp, _ = self.chat("hello", session_id=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_body_without_a_message(self):
        resp = self.client.post("/chat", json={}, headers=AUTH)
        self.assertEqual(resp.status_code, 422)

    def test_errors_use_one_json_shape(self):
        resp, _ = self.chat("")
        self.assertEqual(list(resp.json()), ["error"])


class ConfigTest(ServerTestCase):
    def test_reports_model_tools_and_limits(self):
        body = self.client.get("/config", headers=AUTH).json()
        self.assertEqual(body["model"], "anthropic/claude-sonnet-5-5")
        self.assertEqual(body["mcp"], {"enabled": False, "connected": False, "tools": 0})
        self.assertEqual(body["native_tools"], ["summarize_metric", "detect_metric_anomalies", "detect_log_anomalies"])
        self.assertFalse(body["allow_writes"])
        self.assertEqual(body["limits"]["max_message_chars"], 200)

    def test_never_exposes_secrets(self):
        text = self.client.get("/config", headers=AUTH).text
        self.assertNotIn(TOKEN, text)


class TranslateTest(unittest.TestCase):
    """The ADK event to browser event mapping, which is where streaming bugs would hide."""

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def event(self, parts, partial=False, **kw):
        return self.Obj(content=self.Obj(parts=parts), partial=partial, error_code=None, error_message=None, usage_metadata=None, **kw)

    def part(self, **kw):
        base = dict(text=None, function_call=None, function_response=None, thought=False)
        return self.Obj(**{**base, **kw})

    def test_partial_text_is_streamed_and_the_aggregate_is_dropped(self):
        state = StreamState()
        self.assertEqual(translate(self.event([self.part(text="Hel")], partial=True), state), [("text", {"delta": "Hel"})])
        self.assertEqual(translate(self.event([self.part(text="Hello")]), state), [])

    def test_text_without_streaming_is_sent_once(self):
        self.assertEqual(translate(self.event([self.part(text="Hello")]), StreamState()), [("text", {"delta": "Hello"})])

    def test_the_next_model_call_streams_again_after_an_aggregate(self):
        state = StreamState()
        translate(self.event([self.part(text="a")], partial=True), state)
        translate(self.event([self.part(text="a")]), state)
        self.assertEqual(translate(self.event([self.part(text="b")], partial=True), state), [("text", {"delta": "b"})])

    def test_thinking_is_not_shown(self):
        self.assertEqual(translate(self.event([self.part(text="secret plan", thought=True)]), StreamState()), [])

    def test_tool_calls_are_sent_once_even_if_repeated(self):
        call = self.Obj(id="c1", name="t", args={"a": 1})
        state = StreamState()
        first = translate(self.event([self.part(function_call=call)]), state)
        again = translate(self.event([self.part(function_call=call)]), state)
        self.assertEqual(first, [("tool_call", {"id": "c1", "name": "t", "args": {"a": 1}})])
        self.assertEqual(again, [])

    def test_partial_tool_call_fragments_are_ignored(self):
        call = self.Obj(id="c1", name="t", args={})
        self.assertEqual(translate(self.event([self.part(function_call=call)], partial=True), StreamState()), [])

    def test_mcp_error_results_are_marked_failed(self):
        resp = self.Obj(id="c1", name="query_prometheus", response={"isError": True, "content": [{"text": "boom"}]})
        (name, data), = translate(self.event([self.part(function_response=resp)]), StreamState())
        self.assertEqual(name, "tool_result")
        self.assertFalse(data["ok"])

    def test_token_usage_is_summed_across_model_calls(self):
        state = StreamState()
        usage = self.Obj(prompt_token_count=100, candidates_token_count=20)
        for _ in range(2):
            translate(self.Obj(content=None, partial=False, error_code=None, error_message=None, usage_metadata=usage), state)
        self.assertEqual((state.input_tokens, state.output_tokens), (200, 40))

    def test_error_events_are_forwarded(self):
        event = self.Obj(content=None, partial=False, error_code="X", error_message="Quota exceeded", usage_metadata=None)
        (name, data), = translate(event, StreamState())
        self.assertEqual(name, "error")
        self.assertIn("rate limiting", data["message"])

    def test_error_events_never_pass_provider_text_through(self):
        event = self.Obj(content=None, partial=False, error_code="X", error_message="bad key sk-ant-123 for org 42", usage_metadata=None)
        (_, data), = translate(event, StreamState())
        self.assertNotIn("sk-ant-123", data["message"])

    def test_only_one_error_event_per_turn(self):
        state = StreamState()
        event = self.Obj(content=None, partial=False, error_code="X", error_message="boom", usage_metadata=None)
        self.assertEqual(len(translate(event, state)), 1)
        self.assertEqual(translate(event, state), [])

    def test_long_previews_are_cut(self):
        out = preview({"data": "x" * 5000})
        self.assertLess(len(out), 1700)
        self.assertIn("more characters", out)

    def test_friendly_errors_do_not_leak_details(self):
        class AuthenticationError(Exception):
            pass

        self.assertIn("ANTHROPIC_API_KEY", friendly_error(AuthenticationError("sk-ant-secret")))
        self.assertNotIn("secret", friendly_error(AuthenticationError("sk-ant-secret")))
        self.assertIn("RuntimeError", friendly_error(RuntimeError("password=hunter2")))
        self.assertNotIn("hunter2", friendly_error(RuntimeError("password=hunter2")))

    def test_a_missing_key_is_told_apart_from_a_rejected_one(self):
        class AuthenticationError(Exception):
            pass

        missing = friendly_error(AuthenticationError("Missing Anthropic API Key - set ANTHROPIC_API_KEY"))
        self.assertIn("No API key is configured", missing)
        self.assertIn("No API key is configured", classify_error("litellm.AuthenticationError: Missing Anthropic API Key"))
        self.assertIn("rejected", friendly_error(AuthenticationError("invalid x-api-key")))
        self.assertIn("rejected", classify_error("401 invalid x-api-key"))


class AgentConfigTest(unittest.TestCase):
    def test_write_tools_are_filtered_out_by_default(self):
        allowed = agent_module.tool_allowed(False)
        tool = lambda name: type("T", (), {"name": name})()
        for name in (
            "query_prometheus", "query_loki_logs", "list_datasources", "search_dashboards", "get_dashboard_summary",
            "alerting_rules_read", "alerting_silences_read", "generate_deeplink", "get_annotations", "user_info",
            "list_incidents", "get_panel_image", "find_error_pattern_logs",
        ):
            self.assertTrue(allowed(tool(name)), name)
        for name in (
            "update_dashboard", "create_folder", "delete_annotation", "create_annotation", "alerting_rules_write",
            "alerting_silences_write", "alerting_routing_write", "alerting_manage_routing", "install_plugin",
            "add_activity_to_incident", "update_alert_group", "create_datasource", "grafana_api_request",
        ):
            self.assertFalse(allowed(tool(name)), name)

    def test_writes_can_be_enabled_explicitly(self):
        allowed = agent_module.tool_allowed(True)
        self.assertTrue(allowed(type("T", (), {"name": "update_dashboard"})()))

    def test_instruction_has_the_time_and_the_write_policy(self):
        text = agent_module.make_instruction(Settings(allow_writes=True, extra_instruction="Our regions are named like us-east-1."), clock=lambda: 1_800_000_000)(None)
        self.assertIn("2027-01-15 08:00 UTC", text)
        self.assertIn("Write tools are enabled", text)
        self.assertIn("Our regions are named like us-east-1.", text)
        self.assertNotIn("{", text.replace("{{", ""))  # no unfilled template fields

    def test_read_only_is_the_default_policy(self):
        text = agent_module.make_instruction(Settings(), clock=lambda: 1_800_000_000)(None)
        self.assertIn("you cannot", text)


if __name__ == "__main__":
    unittest.main()
