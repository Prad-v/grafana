"""Settings, read from environment variables once at start."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # LiteLLM model id. ANTHROPIC_API_KEY is read by LiteLLM itself.
    model: str = "anthropic/claude-sonnet-5-5"
    # Shared secret the Grafana plugin proxy sends as `Authorization: Bearer ...`.
    token: str = ""
    # Only for tests: skip the token check.
    disable_auth: bool = False
    port: int = 8080
    log_level: str = "info"

    mcp_enabled: bool = True
    mcp_url: str = "http://mcp-grafana:8000/mcp"
    mcp_token: str = ""
    allow_writes: bool = False

    # Prometheus-compatible API (VictoriaMetrics by default) and VictoriaLogs.
    metrics_url: str = "http://victoriametrics:8428"
    logs_url: str = "http://victorialogs:9428"
    backend_timeout_s: float = 30.0

    max_message_chars: int = 4000
    max_llm_calls: int = 25
    session_ttl_s: int = 3600
    max_sessions: int = 500

    extra_instruction: str = field(default="")

    # Hosts a LiteLLM proxy URL from the plugin's settings page may point at. The settings
    # are set by Grafana organization admins, so without this list an admin of one tenant
    # could make the agent call any address it can reach. `*` allows every host.
    allowed_llm_hosts: tuple[str, ...] = ("api.anthropic.com",)

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            model=os.environ.get("CHAT_AGENT_MODEL", defaults.model).strip() or defaults.model,
            token=os.environ.get("CHAT_AGENT_TOKEN", "").strip(),
            disable_auth=_bool("CHAT_AGENT_DISABLE_AUTH", False),
            port=_int("CHAT_AGENT_PORT", defaults.port),
            log_level=os.environ.get("CHAT_AGENT_LOG_LEVEL", defaults.log_level).strip().lower() or defaults.log_level,
            mcp_enabled=_bool("CHAT_AGENT_MCP_ENABLED", True),
            mcp_url=os.environ.get("MCP_GRAFANA_URL", defaults.mcp_url).strip() or defaults.mcp_url,
            mcp_token=os.environ.get("MCP_GRAFANA_SERVER_TOKEN", "").strip(),
            allow_writes=_bool("CHAT_AGENT_ALLOW_WRITES", False),
            metrics_url=os.environ.get("CHAT_AGENT_METRICS_URL", defaults.metrics_url).strip().rstrip("/"),
            logs_url=os.environ.get("CHAT_AGENT_LOGS_URL", defaults.logs_url).strip().rstrip("/"),
            backend_timeout_s=float(os.environ.get("CHAT_AGENT_BACKEND_TIMEOUT_S", defaults.backend_timeout_s)),
            max_message_chars=_int("CHAT_AGENT_MAX_MESSAGE_CHARS", defaults.max_message_chars),
            max_llm_calls=_int("CHAT_AGENT_MAX_LLM_CALLS", defaults.max_llm_calls),
            session_ttl_s=_int("CHAT_AGENT_SESSION_TTL_S", defaults.session_ttl_s),
            max_sessions=_int("CHAT_AGENT_MAX_SESSIONS", defaults.max_sessions),
            extra_instruction=os.environ.get("CHAT_AGENT_EXTRA_INSTRUCTION", "").strip(),
            allowed_llm_hosts=tuple(h.strip().lower() for h in os.environ.get("CHAT_AGENT_ALLOWED_LLM_HOSTS", ",".join(defaults.allowed_llm_hosts)).split(",") if h.strip()),
        )

    def validate(self) -> None:
        """Refuse to start in a state that would be unsafe or silently useless."""
        if not self.token and not self.disable_auth:
            raise SystemExit(
                "CHAT_AGENT_TOKEN is empty. Set a long random value (`make chat-env` creates one), "
                "or set CHAT_AGENT_DISABLE_AUTH=true for a throwaway local run."
            )
