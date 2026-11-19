---
title: Ask questions about your data with the Kekaflow chat assistant
menuTitle: Chat assistant
description: Add a chat page to Grafana, built with Google ADK and Claude, that answers questions about metrics, logs, alerts, and anomalies through mcp-grafana.
weight: 120
---

# Ask questions about your data with the Kekaflow chat assistant

This guide shows how to run the Kekaflow chat assistant, a Grafana app plugin that adds a chat page to Grafana. You ask in plain language about any metric, logs, alerts, or unusual behavior, and a Claude model answers from live data. The agent is built with Google Agent Development Kit (ADK) and uses Grafana's MCP server, mcp-grafana, for Grafana's own tools.

Everything runs in containers. The agent lives in `backend/services/grafana-chat-agent/` and the plugin in `plugins/kekaflow-chat-app/`.

Before you begin, ensure you have the following:

- Docker with the Compose plugin
- `make`
- An [Anthropic API key](https://console.anthropic.com/)
- The Grafana image built once with `make compose-build`. Refer to [Build and deploy with Docker Compose](../docker-compose/).

You don't need Node.js, Python, or Go on the host.

## Quick start

The assistant is part of the full stack. Start it with the observability services, Keycloak, Grafana, and mcp-grafana.

```sh
make stack-up
```

The command creates `CHAT_AGENT_TOKEN` in `.env`. Add your Claude API key, then recreate the agent:

```sh
# In .env
ANTHROPIC_API_KEY=<YOUR_API_KEY>
```

```sh
make chat-restart
```

Open <http://localhost:3000/a/kekaflow-chat-app>, or choose **More apps** > **Kekaflow Assistant** in the Grafana menu. The header shows the model, the number of Grafana tools the agent can use, and whether it's read-only.

Without a model, the page loads and answers every question with "No API key is configured". Instead of setting `ANTHROPIC_API_KEY`, you can choose the model, including a LiteLLM proxy, on the plugin's Settings page. Refer to [Choose the model](#choose-the-model).

## What you can ask

The assistant picks the tools it needs. Open a tool call in the chat to see the query that ran.

- **Metrics:** "Which GPUs are running hottest right now? Group them by region."
- **Logs:** "Did the number of error logs change in the last hour? Which nodes log the most errors?"
- **Alerts:** "Which alert rules are firing or pending right now?"
- **Dashboards:** "Summarize the GPU fleet overview dashboard and what each panel shows."
- **Anomalies:** "Is there anything unusual in GPU temperature over the last 3 hours? Tell me when it started and where."

Name a time range in your question when it matters. The assistant knows the current time and resolves "the last hour" or "since 14:00 UTC" for you.

## Architecture

The browser never talks to the agent directly. Grafana's plugin proxy checks the user's login, adds the shared token, and forwards the request.

```text
 Browser (Grafana page)
    |  POST /api/plugin-proxy/kekaflow-chat-app/agent/chat   (Grafana session)
    v
 Grafana plugin proxy  --- adds Authorization: Bearer <token>, X-Grafana-User --->  chat-agent
                                                                                      |   |
                                          Grafana tools over streamable HTTP (MCP)    |   |  anomaly toolkit: HTTP
                                                                                      v   v
                                                                    mcp-grafana   VictoriaMetrics / VictoriaLogs
                                                                         |
                                                                         v
                                                                      Grafana
```

The agent has two kinds of tools:

- **Grafana tools from mcp-grafana:** search dashboards, run Prometheus and Loki queries, read alert rules, and more. The agent filters out every tool that changes Grafana unless you set `CHAT_AGENT_ALLOW_WRITES=true`.
- **Three built-in anomaly tools:** `summarize_metric`, `detect_metric_anomalies`, and `detect_log_anomalies`. They query VictoriaMetrics and VictoriaLogs directly and need no language model for the math.

Conversations are kept per user in the agent's memory for an hour, and in the browser tab until you close it. The agent doesn't persist anything to disk.

## Anomaly detection

`detect_metric_anomalies` analyzes every series a query returns and ranks them. `detect_log_anomalies` does the same for the number of log lines that match a LogsQL query, per time bucket.

The method uses only robust statistics (median and median absolute deviation, MAD), so a few extreme points don't distort the baseline. It reports these kinds of findings:

| Finding | Meaning |
| --- | --- |
| `spike` | A short run of points far above the baseline. |
| `dip` | A short run of points far below the baseline. |
| `level_shift` | The series moved to a new level and stayed there. The timestamp is the best-fit step, not the first outlier. |
| `flatline` | A series that normally varies became constant. |
| `gap` | Samples are missing for four steps or more. |

It also looks across series:

- **Cycles:** Workloads that repeat, such as training jobs that cycle, are detected with autocorrelation. The assistant compares each point to the same phase of the cycle, so normal cycling isn't reported as an anomaly.
- **Shared labels:** It reports label values that the anomalous series have in common. Each value includes its share of the anomalous series, its coverage of all series with that value, and its lift over chance. For example: all series with `datacenter=dub1` spiked together.
- **Simultaneous changes:** It reports times when many series changed at once.

Keep these limits in mind:

- The baseline comes from the window you ask about, so the window needs to include normal behavior. Use a lookback at least a few times longer than the anomaly you expect.
- The default `sensitivity` of 4.5 robust standard deviations suits noisy data. Ask the assistant to use 3 for more, noisier results or 7 for only the clear ones.
- A series needs at least 12 points. The assistant analyzes up to 300 series per query and describes the top few. Aggregate in the query, for example `avg by (rack) (...)`, to compare groups.
- The anomaly tools read VictoriaMetrics and VictoriaLogs only. Other data sources work through the Grafana tools, without anomaly scoring.

## Choose the model

By default the agent answers with the model and key that you set in `.env` (`CHAT_AGENT_MODEL` and `ANTHROPIC_API_KEY`). An organization admin can pick another model without restarting anything, on the plugin's Settings page:

1. In Grafana, open **Administration** > **Plugins and data** > **Plugins** > **Kekaflow Assistant**.
1. Select the **Settings** tab.
1. Under **Language model**, choose a provider.
1. Select **Save and test**.

| Provider | What it does | Fields |
| --- | --- | --- |
| **Agent default** | Uses `CHAT_AGENT_MODEL` and `ANTHROPIC_API_KEY` from the agent's environment. | None. |
| **Anthropic** | Calls the Anthropic API directly. | **Model**, for example `claude-sonnet-5-5`. **API key**. Leave the key empty to use `ANTHROPIC_API_KEY` from the agent. |
| **LiteLLM proxy** | Calls a [LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy), which routes the request to any model it hosts. | **LiteLLM proxy address**, for example `http://litellm:4000`. **Model**, the name your proxy exposes. **API key**, if the proxy requires one. |

**Save and test** stores the settings, then makes one small request to the model and shows whether it answered, how long it took, and the reason if it didn't. When the provider lists its models, the **Model** field suggests them. You can always type a name that isn't listed. The chat page header shows the model that answers, and a red badge appears when the saved settings are invalid.

The settings apply to everyone in the organization, and only organization admins can change them. Viewers can still use the chat page.

### How the settings reach the model

Grafana stores the provider, address, and model as plugin settings, and the API key encrypted in the secure settings. The plugin proxy adds them to each request to the agent as the `X-LLM-Provider`, `X-LLM-Base-Url`, `X-LLM-Model`, and `X-LLM-Api-Key` headers. The proxy sets these headers itself and replaces any that a browser sends, so a user can't choose their own model or address. The agent doesn't store the key, and it never returns it.

### Allow your proxy host

Because an admin chooses the proxy address, the agent only calls hosts that you list in `CHAT_AGENT_ALLOWED_LLM_HOSTS`. This stops the agent from being used to reach other services on your network. The Compose default is `api.anthropic.com,litellm,fake-llm`. The `fake-llm` entry is the stand-in proxy that the end-to-end tests use, so remove it for any shared deployment.

```sh
# In .env: a host name matches any port, host:port matches that port only
CHAT_AGENT_ALLOWED_LLM_HOSTS=api.anthropic.com,litellm,litellm.internal:4000
```

Run `make chat-restart` after you change it. The agent refuses addresses that aren't http or https, that contain credentials, or whose host isn't listed. The Anthropic provider ignores the address field and always calls the Anthropic API.

### Keep the model across restarts

Grafana applies `observability/grafana/provisioning/plugins/chat-app.yaml` on every start and replaces the plugin settings with it. A model that you chose on the Settings page is therefore reset when Grafana restarts, unless you also set it in `.env`:

```sh
CHAT_LLM_PROVIDER=litellm              # empty, anthropic, or litellm
CHAT_LLM_BASE_URL=http://litellm:4000
CHAT_LLM_MODEL=claude-sonnet
CHAT_LLM_API_KEY=<YOUR_PROXY_KEY>
```

Then recreate Grafana with `make stack-up`. Leave the variables empty to use the agent's own model.

## Configure the assistant

Set these variables in `.env`. The `make chat-env` target adds them with defaults.

| Variable | Default | Description |
| --- | --- | --- |
| `CHAT_AGENT_TOKEN` | Generated | Shared secret between Grafana and the agent. The agent refuses to start without it. |
| `ANTHROPIC_API_KEY` | Empty | Claude API key. |
| `CHAT_AGENT_MODEL` | `anthropic/claude-sonnet-5-5` | Any model that [LiteLLM](https://docs.litellm.ai/docs/providers) can reach with your key. |
| `CHAT_AGENT_ALLOWED_LLM_HOSTS` | `api.anthropic.com,litellm,fake-llm` | Hosts that the Settings page may point the agent at. Refer to [Allow your proxy host](#allow-your-proxy-host). The agent's own default, outside Compose, is `api.anthropic.com`. |
| `CHAT_LLM_PROVIDER`, `CHAT_LLM_BASE_URL`, `CHAT_LLM_MODEL`, `CHAT_LLM_API_KEY` | Empty | Provision the model on Grafana's side so it survives a Grafana restart. Read by Grafana, not by the agent. |
| `CHAT_AGENT_PORT` | `8081` | Host port for debugging with `curl`. Bound to `127.0.0.1`. |
| `CHAT_AGENT_ALLOW_WRITES` | `false` | Allow tools that change Grafana, such as creating dashboards. |
| `CHAT_AGENT_EXTRA_INSTRUCTION` | Empty | Text appended to the system prompt, such as naming conventions for your fleet. |
| `CHAT_AGENT_LOG_LEVEL` | `INFO` | Log level. |

The agent service reads more settings from its environment, and it listens on port `8080` in the container. The defaults suit most setups.

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_GRAFANA_URL` | `http://mcp-grafana:8000/mcp` | The mcp-grafana endpoint. |
| `MCP_GRAFANA_SERVER_TOKEN` | Empty | Bearer token, if you protect mcp-grafana. |
| `CHAT_AGENT_MCP_ENABLED` | `true` | Set to `false` to run with the built-in tools only. |
| `CHAT_AGENT_METRICS_URL` | `http://victoriametrics:8428` | VictoriaMetrics base URL. |
| `CHAT_AGENT_LOGS_URL` | `http://victorialogs:9428` | VictoriaLogs base URL. |
| `CHAT_AGENT_BACKEND_TIMEOUT_S` | `30` | Timeout for metric and log queries. |
| `CHAT_AGENT_MAX_MESSAGE_CHARS` | `4000` | Longest accepted question. |
| `CHAT_AGENT_MAX_LLM_CALLS` | `25` | Model calls allowed for one question. |
| `CHAT_AGENT_SESSION_TTL_S` | `3600` | Idle time before a conversation is forgotten. |
| `CHAT_AGENT_MAX_SESSIONS` | `500` | Conversations kept in memory. The least recently used are dropped first. |

## Agent API

The plugin proxy exposes the agent under `/api/plugin-proxy/kekaflow-chat-app/agent/`. Every endpoint except `/healthz` needs `Authorization: Bearer <CHAT_AGENT_TOKEN>`. Grafana adds the header, so you only add it when you call the agent directly.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/healthz` | Returns `{"status": "ok"}`. No token needed. |
| `GET` | `/config` | Returns the model that answers, the `llm` object (`source`, `provider`, `model`, `host`, or an `error` for invalid settings), the MCP connection state and tool names, the built-in tools, and limits. |
| `POST` | `/chat` | Asks a question and streams the answer as server-sent events. |
| `POST` | `/llm/test` | Makes one small request with the model settings in the `X-LLM-*` headers. Returns `{"ok": true, ...}` or `{"ok": false, "error": "..."}` with the provider, model, and host. A provider failure is a result and not an HTTP error. |
| `GET` | `/llm/models` | Lists the models that the provider offers for the settings in the `X-LLM-*` headers, as `{"models": [...]}`, or an empty list with an `error`. |
| `DELETE` | `/sessions/{sessionId}` | Forgets a conversation. |

The agent reads the model from these optional request headers, which Grafana's plugin proxy sets from the Settings page: `X-LLM-Provider` (`anthropic` or `litellm`), `X-LLM-Base-Url`, `X-LLM-Model`, and `X-LLM-Api-Key`. Without them, or with an empty provider, the agent uses its own model. Invalid settings answer `400` with a message that names the problem.

The `POST /chat` body is JSON with a `message` and an optional `session_id` of 8 to 64 letters, digits, `-`, or `_`. The agent generates an ID if you omit it. The agent keeps one conversation per user and session ID, and answers `409` if a conversation is still answering.

```sh
curl -N -H "Authorization: Bearer $CHAT_AGENT_TOKEN" -H "X-Grafana-User: me" \
  -H "Content-Type: application/json" \
  -d '{"message": "How many GPUs are reporting?", "session_id": "demo-session-1"}' \
  http://localhost:8081/chat
```

The stream has these events. Each `data` is a JSON object.

| Event | Data |
| --- | --- |
| `session` | `{"session_id": "..."}`. Always first. |
| `tool_call` | `{"id", "name", "args"}`. The agent started a tool. |
| `tool_result` | `{"id", "name", "ok", "preview"}`. A shortened result. |
| `text` | `{"delta": "..."}`. A piece of the answer. |
| `error` | `{"message": "..."}`. A message safe to show users. Details go to the agent log. |
| `done` | `{"input_tokens", "output_tokens"}`. Always last. |

Errors before the stream starts use the shape `{"error": "..."}` with status `400` (bad request), `401` (bad token), `409` (busy), or `413` (message too long).

## Security

- **Token:** Grafana stores `CHAT_AGENT_TOKEN` encrypted and never sends it to the browser. The agent compares it in constant time.
- **Model settings:** Only organization admins can change them. Grafana stores the API key encrypted, never returns it to the browser, and the agent doesn't store or return it. The plugin proxy replaces any `X-LLM-*` headers that a browser sends. The agent only calls the hosts in `CHAT_AGENT_ALLOWED_LLM_HOSTS`.
- **Key in transit:** The key goes from Grafana to the agent on every request. Between separate hosts or clusters, put TLS in front of the agent. In the Compose setup both containers share one private network.
- **Reach:** Any signed-in Grafana user with the Viewer role can use the page. The agent port is bound to `127.0.0.1`.
- **Read-only by default:** Tools that change Grafana are filtered out by name, and the system prompt says the assistant only reads.
- **Errors:** Provider errors are logged and replaced with a fixed message, so keys and request details don't reach the browser.
- **Data access:** The agent reads with the permissions of mcp-grafana, not with the permissions of the person asking. By default, mcp-grafana signs in as the Grafana admin from `.env`, and the anomaly tools read VictoriaMetrics and VictoriaLogs without per-tenant filtering. In the multi-tenant Keycloak demo, every user who can open the page can therefore ask about data from every tenant. Before you share the page with tenants who must stay apart, give mcp-grafana a service account token with the access you intend (`MCP_GRAFANA_TOKEN`), and don't expose data that tenants can't see to the agent.
- **Prompts:** Questions and the data the tools return are sent to the model provider you chose, which is Anthropic or your LiteLLM proxy and the models behind it. Don't use the assistant with data that you can't send there.

## Develop and test

All tests run in containers, and CI runs them on every commit.

```sh
make chat-test    # agent, plugin and Helm chart tests
make chat-e2e     # through Grafana's plugin proxy, needs `make stack-up`
```

The unit tests use a scripted model, a fake backend, and a fake LiteLLM-compatible proxy, so they need no API key and no network. The end-to-end tests check the login requirement on the proxy, the token handling, the MCP connection with read-only tools, the event stream, and that the plugin is enabled. They also save model settings through Grafana's settings API and confirm that the agent receives them, that a Viewer can't change or override them, that a clear key stops being sent, that a disallowed address is refused, and that a question gets answered through the fake proxy.

Other targets:

| Target | Description |
| --- | --- |
| `make chat-env` | Adds the chat settings to `.env` and generates `CHAT_AGENT_TOKEN`. |
| `make chat-build` | Builds the agent and plugin images. |
| `make chat-logs` | Follows the agent logs. |
| `make chat-restart` | Recreates the agent, for example after you change the key or model. |

After you change the plugin source, run `make chat-build` and `make stack-up` to copy the new bundle into Grafana. Reload the browser page.

## Deploy to Kubernetes

Use the Helm chart in `helm/grafana-chat-agent/` for the agent service. Its [README](../../../helm/grafana-chat-agent/README.md) covers the values, how to supply the secrets, and how to install the plugin into Grafana. CI bumps the chart version and image tag after every successful commit to `main`.

The chart hasn't been installed on a live cluster yet. The tests render and lint it with every values file, and the agent image is verified in Docker with the chart's security settings.

## Troubleshoot

The following problems are the most common.

### The page says the assistant service isn't reachable

The plugin proxy answered `404`, `502`, `503`, or `504`. Check that the agent is running and healthy:

```sh
docker compose ps chat-agent
make chat-logs
```

If the agent is healthy, confirm the plugin settings. In Grafana, open **Administration** > **Plugins and data** > **Plugins** > **Kekaflow Assistant** and check that it's enabled. For organizations other than the main one, enable the plugin and set `agentUrl` and `agentToken` in the same way as in `observability/grafana/provisioning/plugins/chat-app.yaml`.

### The page says Grafana could not authenticate to the assistant service

The token Grafana sends doesn't match the agent's. Both read `CHAT_AGENT_TOKEN` from `.env`. After you change it, run `make stack-up` to recreate Grafana and the agent.

### The assistant says no API key is configured, or that the key was rejected

Either set a valid `ANTHROPIC_API_KEY` in `.env` and run `make chat-restart`, or enter a key on the plugin's Settings page and select **Save and test**. The test names the provider and says whether the key was rejected.

### The Settings page says the proxy host isn't allowed

The agent only calls hosts in `CHAT_AGENT_ALLOWED_LLM_HOSTS`. Add the host from your LiteLLM proxy address, with the port if you want to limit it, and run `make chat-restart`.

### The header shows "Model settings invalid"

The saved settings are incomplete, for example a LiteLLM proxy with no address or a provider with no model. Open the Settings page, fix the field that the message names, and select **Save and test**.

### The model you chose resets after Grafana restarts

Grafana replaces the plugin settings with the provisioning file on every start. Set `CHAT_LLM_PROVIDER`, `CHAT_LLM_BASE_URL`, `CHAT_LLM_MODEL`, and `CHAT_LLM_API_KEY` in `.env`, then run `make stack-up`. Refer to [Keep the model across restarts](#keep-the-model-across-restarts).

### The Settings page says "Cannot reach the model provider"

The agent can't connect to the address. From the agent's network the address must resolve, so use the container or service name and not `localhost`. Check `make chat-logs` for the reason.

### The API key field says "configured" but you want no key

Select **Reset** next to the key, then **Save and test**. Grafana keeps the stored key until you reset it.

### The header shows "Grafana tools offline"

The agent can't reach mcp-grafana, and the tooltip shows the reason. The agent still answers with its built-in tools. Check `docker compose ps mcp-grafana` and the `MCP_GRAFANA_URL` value. mcp-grafana only accepts the hosts in its `--allowed-hosts` list, which includes `mcp-grafana:8000`.

### Grafana doesn't show the plugin

Grafana skips unsigned plugins unless you allow them. The compose file sets `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=kekaflow-chat-app`. Check the Grafana log for `kekaflow-chat-app`:

```sh
docker logs kekaflow-grafana-grafana-1 2>&1 | grep kekaflow-chat-app
```

### Answers say the data is missing or the anomaly tools fail

The anomaly tools call VictoriaMetrics and VictoriaLogs directly. Check `CHAT_AGENT_METRICS_URL` and `CHAT_AGENT_LOGS_URL`, and that your query returns data in Grafana. A series needs at least 12 points in the window.

### A conversation answers "still answering the previous message"

One conversation handles one question at a time. Wait for the answer, or choose **Stop**.

## Next steps

- [Run the GPU observability stack](../observability-stack/)
- [Build and deploy with Docker Compose](../docker-compose/)
- Read the [static user guide](../../../static/chat-assistant/index.html).
