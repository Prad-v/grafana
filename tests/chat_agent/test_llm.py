"""Choosing the model per request: header parsing, the allowlist, the forwarding wrapper, and the
real LiteLLM client against a fake OpenAI-compatible proxy."""

import asyncio
import json
import unittest

from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from chatagent import llm
from chatagent.config import Settings
from chatagent.server import create_app

import fakes
from fake_openai import FakeOpenAI
from test_server import AUTH, TOKEN, parse_sse

ALLOWED = ("litellm.internal", "127.0.0.1", "proxy.local:4000")
FALLBACK = "anthropic/claude-sonnet-5-5"


def parse(headers, allowed=ALLOWED):
    return llm.from_headers({k.lower(): v for k, v in headers.items()}, FALLBACK, allowed)


class HeaderParsingTest(unittest.TestCase):
    def test_no_provider_means_use_the_agents_own_configuration(self):
        self.assertIsNone(parse({}))
        self.assertIsNone(parse({"X-LLM-Provider": ""}))

    def test_a_setting_that_was_never_saved_is_not_a_choice(self):
        # This is what Grafana's template engine renders for a missing key.
        self.assertIsNone(parse({"X-LLM-Provider": "<no value>", "X-LLM-Model": "<no value>"}))

    def test_anthropic_with_a_key_and_model(self):
        s = parse({"X-LLM-Provider": "anthropic", "X-LLM-Model": "claude-opus-5-5", "X-LLM-Api-Key": "sk-ant-abc"})
        self.assertEqual((s.provider, s.model, s.api_key), ("anthropic", "claude-opus-5-5", "sk-ant-abc"))
        self.assertEqual(s.litellm_model, "anthropic/claude-opus-5-5")
        self.assertEqual(s.client_kwargs(), {"api_key": "sk-ant-abc"})

    def test_anthropic_without_a_model_uses_the_agents_default(self):
        s = parse({"X-LLM-Provider": "anthropic"})
        self.assertEqual(s.litellm_model, "anthropic/claude-sonnet-5-5")

    def test_anthropic_without_a_key_leaves_it_to_the_environment(self):
        self.assertEqual(parse({"X-LLM-Provider": "anthropic"}).client_kwargs(), {})

    def test_anthropic_ignores_a_base_url_so_it_cannot_reach_other_hosts(self):
        s = parse({"X-LLM-Provider": "anthropic", "X-LLM-Base-Url": "http://169.254.169.254/"})
        self.assertEqual(s.base_url, "")
        self.assertNotIn("api_base", s.client_kwargs())

    def test_a_prefixed_anthropic_model_is_not_prefixed_twice(self):
        s = parse({"X-LLM-Provider": "anthropic", "X-LLM-Model": "anthropic/claude-x"})
        self.assertEqual(s.litellm_model, "anthropic/claude-x")

    def test_litellm_proxy(self):
        s = parse({"X-LLM-Provider": "LiteLLM", "X-LLM-Base-Url": "http://litellm.internal:4000", "X-LLM-Model": "gpt-4o", "X-LLM-Api-Key": "sk-virtual"}, ("litellm.internal",))
        self.assertEqual(s.provider, "litellm")
        self.assertEqual(s.litellm_model, "openai/gpt-4o")
        self.assertEqual(s.client_kwargs(), {"api_base": "http://litellm.internal:4000/v1", "api_key": "sk-virtual"})

    def test_an_alias_with_a_slash_keeps_its_whole_name(self):
        s = parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": "http://127.0.0.1:1", "X-LLM-Model": "team/claude-alias"})
        self.assertEqual(s.litellm_model, "openai/team/claude-alias")

    def test_the_v1_suffix_is_added_once(self):
        for url in ("http://127.0.0.1:1", "http://127.0.0.1:1/", "http://127.0.0.1:1/v1", "http://127.0.0.1:1/v1/"):
            s = parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": url, "X-LLM-Model": "m"})
            self.assertEqual(s.api_base, "http://127.0.0.1:1/v1", url)

    def test_a_proxy_with_no_key_gets_a_placeholder_because_the_client_requires_one(self):
        s = parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": "http://127.0.0.1:1", "X-LLM-Model": "m"})
        self.assertEqual(s.client_kwargs()["api_key"], "none")

    def test_litellm_needs_a_url_and_a_model(self):
        with self.assertRaisesRegex(llm.LlmSettingsError, "proxy URL is not set"):
            parse({"X-LLM-Provider": "litellm", "X-LLM-Model": "m"})
        with self.assertRaisesRegex(llm.LlmSettingsError, "model is not set"):
            parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": "http://127.0.0.1:1"})

    def test_unknown_providers_and_bad_urls_are_refused(self):
        with self.assertRaisesRegex(llm.LlmSettingsError, "Unknown provider"):
            parse({"X-LLM-Provider": "openai"})
        for bad in ("ftp://127.0.0.1", "127.0.0.1:4000", "file:///etc/passwd", "http://"):
            with self.assertRaises(llm.LlmSettingsError, msg=bad):
                parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": bad, "X-LLM-Model": "m"})

    def test_credentials_in_the_url_are_refused_so_they_are_not_logged(self):
        with self.assertRaisesRegex(llm.LlmSettingsError, "API key field"):
            parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": "http://user:pw@127.0.0.1:1", "X-LLM-Model": "m"})

    def test_a_long_provider_name_is_cut_in_the_error(self):
        with self.assertRaises(llm.LlmSettingsError) as ctx:
            parse({"X-LLM-Provider": "x" * 500})
        self.assertLess(len(str(ctx.exception)), 100)


class AllowlistTest(unittest.TestCase):
    def blocked(self, url, allowed=ALLOWED):
        with self.assertRaisesRegex(llm.LlmSettingsError, "not allowed"):
            parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": url, "X-LLM-Model": "m"}, allowed)

    def test_a_host_that_is_not_listed_is_refused(self):
        self.blocked("http://169.254.169.254/latest/meta-data")
        self.blocked("http://evil.example.com")
        self.blocked("http://localhost:4000")

    def test_the_message_names_the_host_and_the_setting_to_change(self):
        with self.assertRaises(llm.LlmSettingsError) as ctx:
            parse({"X-LLM-Provider": "litellm", "X-LLM-Base-Url": "http://evil.example.com:99", "X-LLM-Model": "m"})
        self.assertIn("evil.example.com:99", str(ctx.exception))
        self.assertIn("CHAT_AGENT_ALLOWED_LLM_HOSTS", str(ctx.exception))

    def test_matching_is_by_host_name_and_ignores_case_and_path(self):
        self.assertTrue(llm.host_allowed("http://LiteLLM.Internal:9999/some/path", ("litellm.internal",)))

    def test_a_host_port_entry_only_matches_that_port(self):
        self.assertTrue(llm.host_allowed("http://proxy.local:4000", ("proxy.local:4000",)))
        self.assertFalse(llm.host_allowed("http://proxy.local:5000", ("proxy.local:4000",)))
        self.assertFalse(llm.host_allowed("http://proxy.local", ("proxy.local:4000",)))

    def test_a_listed_name_is_not_a_prefix_match(self):
        self.assertFalse(llm.host_allowed("http://litellm.internal.evil.com", ("litellm.internal",)))
        self.assertFalse(llm.host_allowed("http://evil-litellm.internal", ("litellm.internal",)))

    def test_userinfo_tricks_do_not_pass(self):
        self.assertFalse(llm.host_allowed("http://litellm.internal@evil.com/", ("litellm.internal",)))

    def test_star_allows_everything_and_an_empty_list_nothing(self):
        self.assertTrue(llm.host_allowed("http://anything.example", ("*",)))
        self.assertFalse(llm.host_allowed("http://anything.example", ()))
        self.assertFalse(llm.host_allowed("http://", ("*",)))


class SettingsSecrecyTest(unittest.TestCase):
    def setUp(self):
        self.s = llm.LlmSettings(provider="litellm", model="m", base_url="http://127.0.0.1:1", api_key="sk-super-secret-key")

    def test_the_key_is_not_in_repr_describe_or_fingerprint(self):
        for text in (repr(self.s), str(self.s), json.dumps(self.s.describe()), self.s.fingerprint):
            self.assertNotIn("super-secret", text)

    def test_the_fingerprint_tells_keys_apart(self):
        other = llm.LlmSettings(provider="litellm", model="m", base_url="http://127.0.0.1:1", api_key="sk-other")
        self.assertNotEqual(self.s.fingerprint, other.fingerprint)
        self.assertEqual(self.s.fingerprint, llm.LlmSettings(provider="litellm", model="m", api_key="sk-super-secret-key").fingerprint)
        self.assertEqual(llm.LlmSettings(provider="anthropic", model="m").fingerprint, "")


SEEN: list = []  # module level: pydantic copies a list default, so an instance field would not be shared


class RecordingLlm(BaseLlm):
    """Records which settings built it and what request it was given."""

    model: str = "recording"
    label: str = ""

    async def generate_content_async(self, llm_request, stream=False):
        SEEN.append((self.label, llm_request.model))
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=f"from {self.label}")]))


def request():
    return LlmRequest(model="the-wrong-name", contents=[types.Content(role="user", parts=[types.Part(text="hi")])])


async def collect(model):
    return [r async for r in model.generate_content_async(request())]


class ConfigurableLlmTest(unittest.TestCase):
    def setUp(self):
        self.built = []
        self.seen = SEEN
        SEEN.clear()

        def factory(settings):
            self.built.append(settings)
            return RecordingLlm(model=settings.litellm_model, label=settings.provider + ":" + settings.model)

        self.model = llm.build_model("anthropic/default-model", factory)

    def test_uses_the_agents_own_configuration_when_no_request_settings_are_set(self):
        asyncio.run(collect(self.model))
        self.assertEqual(self.built[0].provider, "environment")
        self.assertEqual(self.seen, [("environment:anthropic/default-model", "anthropic/default-model")])

    def test_uses_the_settings_of_the_current_request(self):
        async def run():
            llm.current_llm.set(llm.LlmSettings("litellm", "gpt-4o", "http://127.0.0.1:1", "k"))
            return await collect(self.model)

        out = asyncio.run(run())
        self.assertEqual(out[0].content.parts[0].text, "from litellm:gpt-4o")
        self.assertEqual(self.seen[0][1], "openai/gpt-4o")

    def test_the_request_carries_the_real_model_name_not_the_one_adk_filled_in(self):
        asyncio.run(collect(self.model))
        self.assertNotEqual(self.seen[0][1], "the-wrong-name")

    def test_concurrent_requests_do_not_see_each_others_settings(self):
        async def one(name):
            llm.current_llm.set(llm.LlmSettings("anthropic", name, api_key=name))
            await asyncio.sleep(0.01)  # let the other task run in between
            return (await collect(self.model))[0].content.parts[0].text

        async def both():
            return await asyncio.gather(*(one(f"model-{i}") for i in range(8)))

        self.assertEqual(asyncio.run(both()), [f"from anthropic:model-{i}" for i in range(8)])

    def test_a_client_is_built_once_per_distinct_settings(self):
        async def run():
            for _ in range(3):
                llm.current_llm.set(llm.LlmSettings("anthropic", "m", api_key="k1"))
                await collect(self.model)
            llm.current_llm.set(llm.LlmSettings("anthropic", "m", api_key="k2"))
            await collect(self.model)

        asyncio.run(run())
        self.assertEqual(len(self.built), 2)

    def test_the_client_cache_is_bounded(self):
        async def run():
            for i in range(llm.MAX_CLIENTS + 5):
                llm.current_llm.set(llm.LlmSettings("anthropic", "m", api_key=f"k{i}"))
                await collect(self.model)

        asyncio.run(run())
        self.assertLessEqual(len(self.model._clients), llm.MAX_CLIENTS)


class FakeProxyTest(unittest.TestCase):
    """The real LiteLLM client, talking to a fake proxy."""

    def settings(self, key="virtual-key-1", model="gpt-fake"):
        return llm.LlmSettings("litellm", model, self.fake.url, key)

    def setUp(self):
        self.fake = FakeOpenAI(required_key="virtual-key-1").start()
        self.addCleanup(self.fake.stop)

    def test_connection_test_sends_the_key_and_the_model(self):
        result = asyncio.run(llm.check_connection(self.settings(), timeout=20))
        self.assertTrue(result["ok"])
        self.assertEqual((result["provider"], result["model"]), ("litellm", "gpt-fake"))
        post = self.fake.posts()[0]
        self.assertEqual(post["path"], "/v1/chat/completions")
        self.assertEqual(post["headers"]["authorization"], "Bearer virtual-key-1")
        self.assertEqual(post["body"]["model"], "gpt-fake")
        self.assertNotIn("api_key", json.dumps(result))

    def test_a_wrong_key_raises_an_authentication_error(self):
        with self.assertRaises(Exception) as ctx:
            asyncio.run(llm.check_connection(self.settings(key="wrong"), timeout=20))
        self.assertEqual(type(ctx.exception).__name__, "AuthenticationError")

    def test_lists_the_models_the_proxy_offers(self):
        self.assertEqual(asyncio.run(llm.list_models(self.settings())), ["gpt-fake", "team/claude-alias"])
        self.assertEqual(self.fake.requests[-1]["headers"]["authorization"], "Bearer virtual-key-1")

    def test_listing_models_with_a_wrong_key_fails_with_the_status(self):
        with self.assertRaises(Exception) as ctx:
            asyncio.run(llm.list_models(self.settings(key="wrong")))
        self.assertEqual(ctx.exception.response.status_code, 401)

    def test_anthropic_model_listing_needs_a_key(self):
        with self.assertRaisesRegex(llm.LlmSettingsError, "No API key"):
            asyncio.run(llm.list_models(llm.LlmSettings("anthropic", "claude-x")))


class ServerWithRequestSettingsTest(unittest.TestCase):
    """The whole agent loop with the real LiteLLM client and no scripted model."""

    def setUp(self):
        self.fake = FakeOpenAI(reply="The fleet looks healthy.", required_key="virtual-key-1").start()
        self.addCleanup(self.fake.stop)
        self.backend = fakes.FakeBackend()
        self.backend.__enter__()
        self.addCleanup(self.backend.__exit__, None, None, None)
        self.settings = Settings(token=TOKEN, mcp_enabled=False, metrics_url=self.backend.url, logs_url=self.backend.url, allowed_llm_hosts=("127.0.0.1",))
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def proxy_headers(self, **overrides):
        h = {**AUTH, "X-LLM-Provider": "litellm", "X-LLM-Base-Url": self.fake.url, "X-LLM-Model": "gpt-fake", "X-LLM-Api-Key": "virtual-key-1"}
        h.update(overrides)
        return h

    def chat(self, headers, session="session-0001"):
        resp = self.client.post("/chat", json={"message": "How is the fleet?", "session_id": session}, headers=headers)
        return resp, (parse_sse(resp.text) if resp.status_code == 200 else [])

    def test_a_question_is_answered_by_the_model_from_the_settings(self):
        resp, events = self.chat(self.proxy_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        text = "".join(d["delta"] for n, d in events if n == "text")
        self.assertEqual(text, "The fleet looks healthy.")
        self.assertEqual(events[-1][0], "done")
        self.assertNotIn("error", [n for n, _ in events])
        post = self.fake.posts()[0]
        self.assertEqual(post["headers"]["authorization"], "Bearer virtual-key-1")
        self.assertEqual(post["body"]["model"], "gpt-fake")
        # The agent's tools reach the proxy as OpenAI tool declarations.
        names = {t["function"]["name"] for t in post["body"].get("tools", [])}
        self.assertIn("detect_metric_anomalies", names)

    def test_a_wrong_key_is_reported_without_leaking_it(self):
        resp, events = self.chat(self.proxy_headers(**{"X-LLM-Api-Key": "sk-wrong-secret-9"}))
        errors = [d["message"] for n, d in events if n == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("rejected the API key", errors[0])
        self.assertNotIn("wrong-secret", resp.text)

    def test_two_users_with_different_settings_in_a_row_each_get_their_own_key(self):
        self.chat(self.proxy_headers(), session="session-aaaa")
        self.chat(self.proxy_headers(**{"X-LLM-Api-Key": "sk-other"}), session="session-bbbb")
        keys = [p["headers"]["authorization"] for p in self.fake.posts()]
        self.assertEqual(keys, ["Bearer virtual-key-1", "Bearer sk-other"])

    def test_a_host_that_is_not_allowed_is_a_400_and_the_proxy_is_never_called(self):
        resp = self.client.post("/chat", json={"message": "hi"}, headers=self.proxy_headers(**{"X-LLM-Base-Url": "http://169.254.169.254"}))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not allowed", resp.json()["error"])
        self.assertEqual(self.fake.requests, [])

    def test_headers_without_the_agent_token_are_still_refused(self):
        headers = {k: v for k, v in self.proxy_headers().items() if k != "Authorization"}
        self.assertEqual(self.client.post("/chat", json={"message": "hi"}, headers=headers).status_code, 401)
        self.assertEqual(self.fake.requests, [])

    def test_config_reports_where_the_model_comes_from_and_never_the_key(self):
        body = self.client.get("/config", headers=self.proxy_headers()).json()
        self.assertEqual(body["llm"], {"source": "settings", "provider": "litellm", "model": "gpt-fake", "host": self.fake.url.removeprefix("http://")})
        self.assertEqual(body["model"], "gpt-fake")
        self.assertNotIn("virtual-key-1", json.dumps(body))

    def test_config_without_settings_reports_the_environment(self):
        body = self.client.get("/config", headers=AUTH).json()
        self.assertEqual(body["llm"]["source"], "environment")
        self.assertEqual(body["model"], self.settings.model)

    def test_config_survives_a_bad_saved_setting_so_the_settings_page_can_fix_it(self):
        resp = self.client.get("/config", headers=self.proxy_headers(**{"X-LLM-Base-Url": "http://evil.example"}))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not allowed", resp.json()["llm"]["error"])

    def test_the_test_endpoint_reports_success(self):
        body = self.client.post("/llm/test", headers=self.proxy_headers()).json()
        self.assertTrue(body["ok"], body)
        self.assertGreaterEqual(body["latency_ms"], 0)
        self.assertNotIn("virtual-key-1", json.dumps(body))

    def test_the_test_endpoint_reports_a_rejected_key_as_a_result_not_an_error(self):
        resp = self.client.post("/llm/test", headers=self.proxy_headers(**{"X-LLM-Api-Key": "bad"}))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("rejected the API key", body["error"])

    def test_the_test_endpoint_reports_an_unreachable_proxy(self):
        dead = FakeOpenAI().start()
        url = dead.url
        dead.stop()
        body = self.client.post("/llm/test", headers=self.proxy_headers(**{"X-LLM-Base-Url": url})).json()
        self.assertFalse(body["ok"])
        self.assertIn("Cannot reach the model provider", body["error"])

    def test_the_test_endpoint_needs_the_token_and_rejects_disallowed_hosts(self):
        self.assertEqual(self.client.post("/llm/test").status_code, 401)
        resp = self.client.post("/llm/test", headers=self.proxy_headers(**{"X-LLM-Base-Url": "http://evil.example"}))
        self.assertEqual(resp.status_code, 400)

    def test_models_are_listed_for_the_settings_page(self):
        body = self.client.get("/llm/models", headers=self.proxy_headers()).json()
        self.assertEqual(body["models"], ["gpt-fake", "team/claude-alias"])

    def test_models_with_a_rejected_key_explain_and_return_an_empty_list(self):
        body = self.client.get("/llm/models", headers=self.proxy_headers(**{"X-LLM-Api-Key": "bad"})).json()
        self.assertEqual(body["models"], [])
        self.assertIn("rejected the API key", body["error"])

    def test_models_needs_the_token(self):
        self.assertEqual(self.client.get("/llm/models").status_code, 401)


if __name__ == "__main__":
    unittest.main()
