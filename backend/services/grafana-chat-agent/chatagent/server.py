"""HTTP API: a streaming chat endpoint over server-sent events, plus health and config."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from .agent import APP_NAME, build_agent, build_toolset
from .backends import Backends
from .config import Settings
from .llm import ENVIRONMENT, LlmSettings, LlmSettingsError, check_connection, current_llm, default_settings, from_headers, list_models

log = logging.getLogger("chatagent")

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
RESULT_PREVIEW_CHARS = 1500


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@dataclass
class SessionEntry:
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class StreamState:
    """What the translator remembers between ADK events of one turn."""

    streamed_text: bool = False
    seen_calls: set[str] = field(default_factory=set)
    errored: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def preview(value, limit: int = RESULT_PREVIEW_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"… ({len(text) - limit} more characters)"


NO_API_KEY = "No API key is configured. Add one on the assistant's settings page, or set ANTHROPIC_API_KEY in .env and run `make chat-restart`."


def classify_error(text: str) -> str:
    """A safe message for an error that only exists as text, such as an ADK error event."""
    low = text.lower()
    if "missing" in low and "key" in low:
        return NO_API_KEY
    if any(k in low for k in ("api key", "api_key", "authentication", "unauthorized", "401", "403", "permission")):
        return "The model provider rejected the API key. Check the key on the assistant's settings page or ANTHROPIC_API_KEY."
    if any(k in low for k in ("rate limit", "rate_limit", "429", "quota", "overloaded")):
        return "The model provider is rate limiting requests. Try again in a minute."
    if "model" in low and any(k in low for k in ("not found", "404", "does not exist", "invalid")):
        return "The model provider does not know the configured model. Check the model on the assistant's settings page or CHAT_AGENT_MODEL."
    if any(k in low for k in ("connect", "timeout", "timed out", "unreachable")):
        return "Cannot reach the model provider. Check the network and try again."
    return "The model returned an error. See the chat-agent logs."


def _model_list_error(status: int) -> str:
    if status in (401, 403):
        return "The model provider rejected the API key. Check the key on the assistant's settings page or ANTHROPIC_API_KEY."
    return f"The model provider answered HTTP {status} when listing models. You can still type the model name."


def friendly_error(exc: BaseException) -> str:
    """A message safe to show a user. Details go to the log, not to the browser."""
    name = type(exc).__name__
    if name == "AuthenticationError" and "missing" in str(exc).lower():
        return NO_API_KEY
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return "The model provider rejected the API key. Check the key on the assistant's settings page or ANTHROPIC_API_KEY."
    if name == "RateLimitError":
        return "The model provider is rate limiting requests. Try again in a minute."
    if name in {"NotFoundError", "BadRequestError"} and "model" in str(exc).lower():
        return "The model provider does not know the configured model. Check the model on the assistant's settings page or CHAT_AGENT_MODEL."
    if name in {"APIConnectionError", "Timeout", "APITimeoutError", "ServiceUnavailableError"} or "connection error" in str(exc).lower():
        # LiteLLM reports a refused or unresolvable connection as an InternalServerError whose text says so.
        return "Cannot reach the model provider. Check the network and try again."
    if name in {"InternalServerError", "BadGatewayError"}:
        return "The model provider returned a server error. Try again in a minute."
    if "mcp" in name.lower() or "McpError" in name or "ConnectError" in name:
        return "Cannot reach mcp-grafana. Check that it is running."
    return f"The assistant hit an error ({name}). See the chat-agent logs."


def translate(event, state: StreamState) -> list[tuple[str, dict]]:
    """Turn one ADK event into zero or more events for the browser."""
    out: list[tuple[str, dict]] = []
    if getattr(event, "error_code", None) or getattr(event, "error_message", None):
        # The text comes from the provider and can carry request details, so it is logged, not shown.
        log.warning("model error event: code=%s message=%s", event.error_code, event.error_message)
        if not state.errored:
            state.errored = True
            out.append(("error", {"message": classify_error(f"{event.error_code} {event.error_message}")}))

    usage = getattr(event, "usage_metadata", None)
    if usage is not None and not event.partial:
        state.input_tokens += getattr(usage, "prompt_token_count", 0) or 0
        state.output_tokens += getattr(usage, "candidates_token_count", 0) or 0

    parts = (event.content.parts if event.content else None) or []
    for part in parts:
        if part.function_call and not event.partial:
            call = part.function_call
            call_id = call.id or f"{call.name}-{len(state.seen_calls)}"
            if call_id in state.seen_calls:
                continue
            state.seen_calls.add(call_id)
            out.append(("tool_call", {"id": call_id, "name": call.name, "args": dict(call.args or {})}))
        elif part.function_response:
            resp = part.function_response
            body = resp.response if isinstance(resp.response, dict) else {"result": resp.response}
            failed = body.get("status") == "error" or bool(body.get("isError"))
            out.append(("tool_result", {"id": resp.id, "name": resp.name, "ok": not failed, "preview": preview(body)}))
        elif part.text and not getattr(part, "thought", False):
            if event.partial:
                state.streamed_text = True
                out.append(("text", {"delta": part.text}))
            elif not state.streamed_text:
                out.append(("text", {"delta": part.text}))
    if not event.partial:
        # The aggregated event repeats what was streamed. Start fresh for the next model call.
        state.streamed_text = False
    return out


def create_app(settings: Settings, *, model=None, backends: Backends | None = None, toolset=None, clock=time.time) -> FastAPI:
    """Build the app. ``model``, ``backends`` and ``toolset`` are injectable for tests."""
    settings.validate()
    sessions = InMemorySessionService()
    registry: dict[tuple[str, str], SessionEntry] = {}
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        be = backends or Backends(settings.metrics_url, settings.logs_url, settings.backend_timeout_s)
        ts = toolset if toolset is not None else build_toolset(settings)
        holder.update(
            backends=be,
            toolset=ts,
            runner=Runner(app_name=APP_NAME, agent=build_agent(settings, be, ts, model=model), session_service=sessions),
        )
        log.info("chat agent ready: model=%s mcp=%s writes=%s", settings.model, settings.mcp_url if ts else "off", settings.allow_writes)
        try:
            yield
        finally:
            if ts is not None:
                try:
                    await ts.close()
                except Exception:  # shutting down; nothing useful to do with the error
                    log.debug("closing the MCP toolset failed", exc_info=True)
            await be.aclose()

    app = FastAPI(title="grafana-chat-agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def require_auth(authorization: str = Header(default="")) -> None:
        if settings.disable_auth:
            return
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), settings.token.encode()):
            raise HTTPException(status_code=401, detail="Missing or invalid token.")

    def llm_from(request: Request) -> LlmSettings:
        """The model settings Grafana sent with this request, or the agent's own configuration."""
        try:
            sent = from_headers(request.headers, settings.model, settings.allowed_llm_hosts)
        except LlmSettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return sent or default_settings(settings.model)

    def current_user(x_grafana_user: str = Header(default="")) -> str:
        # Set by the Grafana plugin proxy (dataproxy.send_user_header). Sessions are scoped to it,
        # so one user cannot read another's conversation by guessing a session id.
        cleaned = re.sub(r"[^\w.@+-]", "_", x_grafana_user.strip())[:128]
        return cleaned or "anonymous"

    def gc() -> None:
        now = clock()
        expired = [k for k, e in registry.items() if now - e.last_used > settings.session_ttl_s and not e.lock.locked()]
        overflow = len(registry) - len(expired) - settings.max_sessions
        if overflow > 0:
            idle = sorted((e.last_used, k) for k, e in registry.items() if k not in expired and not e.lock.locked())
            expired += [k for _, k in idle[:overflow]]
        for key in expired:
            registry.pop(key, None)
            asyncio.get_running_loop().create_task(sessions.delete_session(app_name=APP_NAME, user_id=key[0], session_id=key[1]))

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/config", dependencies=[Depends(require_auth)])
    async def config(request: Request) -> dict:
        ts = holder.get("toolset")
        mcp: dict = {"enabled": ts is not None, "connected": False, "tools": 0}
        if ts is not None:
            try:
                tools = await asyncio.wait_for(ts.get_tools(), timeout=8)
                mcp.update(connected=True, tools=len(tools), names=sorted(t.name for t in tools))
            except Exception as exc:
                log.warning("mcp-grafana is not reachable: %s", exc)
                mcp["error"] = "Cannot reach mcp-grafana."
        try:
            chosen = llm_from(request)
            llm_info: dict = {"source": "environment" if chosen.provider == ENVIRONMENT else "settings", **chosen.describe()}
        except HTTPException as exc:
            # A bad saved setting must not take the whole page down: report it and let the settings page fix it.
            chosen = default_settings(settings.model)
            llm_info = {"source": "settings", "error": exc.detail}
        return {
            "model": chosen.model,
            "llm": llm_info,
            "mcp": mcp,
            "native_tools": ["summarize_metric", "detect_metric_anomalies", "detect_log_anomalies"],
            "allow_writes": settings.allow_writes,
            "limits": {"max_message_chars": settings.max_message_chars, "max_llm_calls": settings.max_llm_calls},
        }

    @app.post("/chat", dependencies=[Depends(require_auth)])
    async def chat(body: ChatRequest, user: str = Depends(current_user), llm: LlmSettings = Depends(llm_from)):
        message = body.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="The message is empty.")
        if len(message) > settings.max_message_chars:
            raise HTTPException(status_code=413, detail=f"The message is longer than {settings.max_message_chars} characters.")
        sid = body.session_id or uuid.uuid4().hex
        if not SESSION_ID.match(sid):
            raise HTTPException(status_code=400, detail="session_id must be 8 to 64 letters, digits, '-' or '_'.")

        gc()
        entry = registry.setdefault((user, sid), SessionEntry(last_used=clock()))
        if entry.lock.locked():
            raise HTTPException(status_code=409, detail="This conversation is still answering the previous message.")
        entry.last_used = clock()

        async def stream():
            async with entry.lock:
                # Not reset afterwards: this runs in the task that serves this one response.
                current_llm.set(None if llm.provider == ENVIRONMENT else llm)
                state = StreamState()
                yield sse("session", {"session_id": sid})
                try:
                    if await sessions.get_session(app_name=APP_NAME, user_id=user, session_id=sid) is None:
                        await sessions.create_session(app_name=APP_NAME, user_id=user, session_id=sid)
                    run_config = RunConfig(streaming_mode=StreamingMode.SSE, max_llm_calls=settings.max_llm_calls)
                    content = types.Content(role="user", parts=[types.Part(text=message)])
                    async for event in holder["runner"].run_async(user_id=user, session_id=sid, new_message=content, run_config=run_config):
                        for name, data in translate(event, state):
                            yield sse(name, data)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("chat turn failed (user=%s session=%s)", user, sid)
                    if not state.errored:
                        yield sse("error", {"message": friendly_error(exc)})
                finally:
                    entry.last_used = clock()
                yield sse("done", {"input_tokens": state.input_tokens, "output_tokens": state.output_tokens})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            # No buffering anywhere on the way to the browser, or the answer arrives all at once.
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.delete("/sessions/{session_id}", dependencies=[Depends(require_auth)])
    async def reset(session_id: str, user: str = Depends(current_user)) -> dict:
        if not SESSION_ID.match(session_id):
            raise HTTPException(status_code=400, detail="Invalid session id.")
        registry.pop((user, session_id), None)
        await sessions.delete_session(app_name=APP_NAME, user_id=user, session_id=session_id)
        return {"status": "ok"}

    @app.post("/llm/test", dependencies=[Depends(require_auth)])
    async def llm_test(llm: LlmSettings = Depends(llm_from)) -> dict:
        """Make one tiny request with the settings Grafana sent. Provider failures are a result, not an HTTP error."""
        try:
            return await check_connection(llm, timeout=settings.backend_timeout_s)
        except Exception as exc:
            log.warning("llm connection test failed (%s): %s", type(exc).__name__, exc)
            return {"ok": False, "error": friendly_error(exc), **llm.describe()}

    @app.get("/llm/models", dependencies=[Depends(require_auth)])
    async def llm_models(llm: LlmSettings = Depends(llm_from)) -> dict:
        try:
            return {"models": await list_models(llm, api_key_fallback=os.environ.get("ANTHROPIC_API_KEY", ""), timeout=15), **llm.describe()}
        except LlmSettingsError as exc:
            return {"models": [], "error": str(exc)}
        except Exception as exc:
            log.warning("listing models failed (%s): %s", type(exc).__name__, exc)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            message = friendly_error(exc) if status is None else _model_list_error(status)
            return {"models": [], "error": message}

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    return app
