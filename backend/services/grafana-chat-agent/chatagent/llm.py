"""Which language model answers: the agent's own configuration, or per-request settings.

A Grafana administrator picks the provider on the plugin's settings page. Grafana's plugin
proxy sends the choice with every request as `X-LLM-*` headers, and the agent builds the model
client from them. The API key lives in Grafana's encrypted settings, not on the agent.

With no headers the agent falls back to `CHAT_AGENT_MODEL` and `ANTHROPIC_API_KEY`, so a
deployment that never opens the settings page keeps working.

No `from __future__ import annotations`: pydantic (through ADK's BaseLlm) reads these annotations.
"""

import hashlib
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Mapping
from urllib.parse import urlparse

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import PrivateAttr

log = logging.getLogger("chatagent")

ANTHROPIC = "anthropic"
LITELLM = "litellm"
ENVIRONMENT = "environment"
PROVIDERS = (ANTHROPIC, LITELLM)

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

# What Grafana's plugin proxy renders for a setting that was never saved.
_UNSET = {"", "<no value>", "<nil>"}

MAX_CLIENTS = 32


class LlmSettingsError(ValueError):
    """The settings are unusable. The message is safe to show to the person who set them."""


@dataclass(frozen=True)
class LlmSettings:
    provider: str  # "anthropic", "litellm", or "environment" for the agent's own configuration
    model: str
    base_url: str = ""
    api_key: str = field(default="", repr=False)

    @property
    def litellm_model(self) -> str:
        """The model name in LiteLLM's `<provider>/<model>` form."""
        if self.provider == ANTHROPIC:
            return self.model if self.model.startswith("anthropic/") else f"anthropic/{self.model}"
        if self.provider == LITELLM:
            # The proxy speaks the OpenAI API. The model is the alias it exposes, which can itself
            # contain a slash, so the prefix is always added.
            return f"openai/{self.model}"
        return self.model

    @property
    def api_base(self) -> str:
        if self.provider != LITELLM:
            return ""
        base = self.base_url.rstrip("/")
        return base if base.rsplit("/", 1)[-1] == "v1" else f"{base}/v1"

    @property
    def fingerprint(self) -> str:
        """Identifies a key without revealing it, for cache keys and logs."""
        return hashlib.sha256(self.api_key.encode()).hexdigest()[:10] if self.api_key else ""

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc if self.base_url else ""

    def describe(self) -> dict:
        """What may be shown to users: never the key."""
        out: dict[str, Any] = {"provider": self.provider, "model": self.model}
        if self.host:
            out["host"] = self.host
        return out

    def client_kwargs(self) -> dict[str, str]:
        kwargs: dict[str, str] = {}
        if self.provider == LITELLM:
            kwargs["api_base"] = self.api_base
            # The OpenAI client refuses to start without a key, and a proxy with no auth accepts any.
            kwargs["api_key"] = self.api_key or "none"
        elif self.api_key:
            kwargs["api_key"] = self.api_key
        return kwargs


def default_settings(model: str) -> LlmSettings:
    """The agent's own configuration. LiteLLM reads the key from the environment."""
    return LlmSettings(provider=ENVIRONMENT, model=model)


def _clean(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value in _UNSET else value


def host_allowed(url: str, allowed: tuple[str, ...]) -> bool:
    """An entry matches a bare host name or `host:port`. `*` allows everything."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    candidates = {host}
    if parsed.port:
        candidates.add(f"{host}:{parsed.port}")
    entries = {a.strip().lower() for a in allowed if a.strip()}
    return "*" in entries or bool(candidates & entries)


def from_headers(headers: Mapping[str, str], fallback_model: str, allowed_hosts: tuple[str, ...]) -> LlmSettings | None:
    """Settings sent by Grafana, or None when the administrator has not chosen a provider."""
    provider = _clean(headers.get("x-llm-provider")).lower()
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise LlmSettingsError(f"Unknown provider '{provider[:30]}'. Choose Anthropic or LiteLLM proxy.")

    model = _clean(headers.get("x-llm-model"))
    api_key = _clean(headers.get("x-llm-api-key"))

    if provider == ANTHROPIC:
        # The address is fixed, so a base URL is ignored: it cannot be used to reach other hosts.
        fallback = fallback_model.removeprefix("anthropic/")
        return LlmSettings(provider=ANTHROPIC, model=model or fallback, api_key=api_key)

    base_url = _clean(headers.get("x-llm-base-url"))
    if not base_url:
        raise LlmSettingsError("The LiteLLM proxy URL is not set.")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LlmSettingsError("The LiteLLM proxy URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise LlmSettingsError("Put the key in the API key field, not in the proxy URL.")
    if not host_allowed(base_url, allowed_hosts):
        raise LlmSettingsError(
            f"The proxy host '{parsed.netloc}' is not allowed. Add it to CHAT_AGENT_ALLOWED_LLM_HOSTS on the agent."
        )
    if not model:
        raise LlmSettingsError("The model is not set. Enter the model alias your LiteLLM proxy exposes.")
    return LlmSettings(provider=LITELLM, model=model, base_url=base_url, api_key=api_key)


# The settings for the request being served. A context variable keeps concurrent requests apart.
current_llm: ContextVar[LlmSettings | None] = ContextVar("current_llm", default=None)


def make_client(settings: LlmSettings) -> BaseLlm:
    return LiteLlm(model=settings.litellm_model, **settings.client_kwargs())


class ConfigurableLlm(BaseLlm):
    """Forwards each call to a LiteLLM client built from the settings of the current request."""

    default: LlmSettings
    factory: Callable[[LlmSettings], BaseLlm] = make_client
    _clients: dict = PrivateAttr(default_factory=dict)

    def _active(self) -> LlmSettings:
        return current_llm.get() or self.default

    def _client(self, settings: LlmSettings) -> BaseLlm:
        key = (settings.litellm_model, settings.api_base, settings.fingerprint)
        client = self._clients.get(key)
        if client is None:
            if len(self._clients) >= MAX_CLIENTS:
                self._clients.clear()
            client = self.factory(settings)
            self._clients[key] = client
        return client

    @property
    def capabilities(self):
        return self._client(self._active()).capabilities

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        client = self._client(self._active())
        # ADK fills this from the name of the model it was given, which is not the real one.
        request = llm_request.model_copy(update={"model": client.model})
        async for response in client.generate_content_async(request, stream=stream):
            yield response


def build_model(default_model: str, factory: Callable[[LlmSettings], BaseLlm] = make_client) -> ConfigurableLlm:
    return ConfigurableLlm(model=default_model, default=default_settings(default_model), factory=factory)


async def check_connection(settings: LlmSettings, timeout: float = 30.0) -> dict:
    """Make the smallest possible request. Raises the provider's exception on failure."""
    import litellm  # imported late: it is slow to load and only needed here

    started = time.monotonic()
    await litellm.acompletion(
        model=settings.litellm_model,
        messages=[{"role": "user", "content": "Reply with the word ok."}],
        max_tokens=8,
        timeout=timeout,
        **settings.client_kwargs(),
    )
    return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000), **settings.describe()}


async def list_models(settings: LlmSettings, api_key_fallback: str = "", timeout: float = 15.0) -> list[str]:
    """Model names the provider offers, for the settings page. Raises on failure."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        if settings.provider == LITELLM:
            headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
            resp = await http.get(f"{settings.api_base}/models", headers=headers)
        else:
            key = settings.api_key or api_key_fallback
            if not key:
                raise LlmSettingsError("No API key is set, so the model list cannot be loaded.")
            resp = await http.get(ANTHROPIC_MODELS_URL, headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, params={"limit": 100})
        resp.raise_for_status()
        body = resp.json()
    return sorted({str(m["id"]) for m in body.get("data", []) if isinstance(m, dict) and m.get("id")})
