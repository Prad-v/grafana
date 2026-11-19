# grafana-chat-agent

Helm chart for the agent behind the Kekaflow chat assistant: a Google ADK and Claude service that Grafana's `kekaflow-chat-app` plugin calls through its plugin proxy. For what the assistant does and for local use with Docker Compose, refer to [the chat assistant guide](../../docs/sources/developers/chat-assistant.md).

The chart deploys only the agent. Grafana, mcp-grafana, and your metrics and logs backends are separate. You also install the plugin into Grafana, as described below.

## Install

Create the secrets first so they never pass through your values files or shell history:

```sh
kubectl create namespace observability
kubectl -n observability create secret generic grafana-chat-agent-auth \
  --from-literal=token="$(openssl rand -hex 24)"
kubectl -n observability create secret generic grafana-chat-agent-anthropic \
  --from-literal=anthropic-api-key="<YOUR_API_KEY>"
```

Then install with the values for your environment:

```sh
helm install chat-agent ./helm/grafana-chat-agent -n observability \
  -f helm/grafana-chat-agent/values.yaml -f helm/grafana-chat-agent/values-prod.yaml \
  --set mcp.url=http://mcp-grafana.observability.svc:8000/mcp \
  --set backends.metricsUrl=http://victoriametrics.observability.svc:8428 \
  --set backends.logsUrl=http://victorialogs.observability.svc:9428
```

The default values contain no credentials. The install fails unless you set `auth.existingSecret` and `anthropic.existingSecret`, or the inline `auth.token` and `anthropic.apiKey`, which make the chart create the Secrets.

| File | Use |
| --- | --- |
| `values.yaml` | Safe defaults. CI updates `image.tag` here. |
| `values-dev.yaml` | Development: debug logs, no network policy, `Always` pull. |
| `values-prod.yaml` | Production: existing Secrets, network policy, larger resources. |

## Values

| Value | Default | Description |
| --- | --- | --- |
| `image.repository`, `image.tag` | `kekaflow/grafana-chat-agent`, the chart version | The agent image. CI sets both. |
| `auth.existingSecret`, `auth.existingSecretKey` | empty, `token` | Secret that holds the shared token. |
| `auth.token` | empty | Creates a Secret when `auth.existingSecret` is empty. |
| `anthropic.existingSecret`, `anthropic.existingSecretKey` | empty, `anthropic-api-key` | Secret that holds the Claude API key. |
| `anthropic.apiKey` | empty | Creates a Secret when `anthropic.existingSecret` is empty. |
| `agent.model` | `anthropic/claude-sonnet-5-5` | Model, as a LiteLLM name. |
| `agent.allowedLlmHosts` | `[api.anthropic.com]` | Hosts the plugin's Settings page may point the agent at. Add your LiteLLM proxy, for example `litellm.internal:4000`. A host name matches any port. |
| `agent.allowWrites` | `false` | Allow tools that change Grafana. |
| `agent.extraInstruction` | empty | Text appended to the system prompt. |
| `agent.maxMessageChars`, `agent.maxLlmCalls` | `4000`, `25` | Request limits. |
| `agent.sessionTtlSeconds`, `agent.maxSessions` | `3600`, `500` | Conversation memory limits. |
| `mcp.enabled`, `mcp.url` | `true`, `http://mcp-grafana:8000/mcp` | The mcp-grafana endpoint. |
| `mcp.tokenSecret`, `mcp.tokenSecretKey` | empty, `token` | Secret with a bearer token, if mcp-grafana is protected. |
| `backends.metricsUrl`, `backends.logsUrl` | VictoriaMetrics and VictoriaLogs service URLs | Used by the anomaly tools. |
| `networkPolicy.enabled` | `true` | Only Grafana's pods can reach the agent. |
| `networkPolicy.grafanaPodLabels`, `networkPolicy.grafanaNamespace` | `app.kubernetes.io/name: grafana`, release namespace | Which pods count as Grafana. |
| `replicaCount` | `1` | Conversations live in memory. Use more than one replica only with session affinity. |

The pod runs as UID `10001` with a read-only root filesystem, no capabilities, and no service account token.

## Connect Grafana

1. Copy the plugin into Grafana's plugins directory with an init container that uses the plugin image (`chat-plugin`, published next to the agent image). Allow the unsigned plugin with `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=kekaflow-chat-app`, and set `GF_DATAPROXY_SEND_USER_HEADER=true` so the agent keeps one conversation per user.
2. Provision the plugin with the agent's address and the token, in the same way as `observability/grafana/provisioning/plugins/chat-app.yaml`. Read the token from the same Secret as the agent, for example as an environment variable.

```yaml
apps:
  - type: kekaflow-chat-app
    org_id: 1
    disabled: false
    jsonData:
      agentUrl: http://chat-agent-grafana-chat-agent.observability.svc:8080
    secureJsonData:
      agentToken: $CHAT_AGENT_TOKEN
```

Organization admins choose the model on the plugin's **Settings** page. Grafana replaces provisioned plugin settings on every start, so to keep a choice across restarts, add `llmProvider` (`anthropic` or `litellm`), `llmBaseUrl`, and `llmModel` to `jsonData`, and `llmApiKey` to `secureJsonData`, reading the key from a Secret. If you leave them out, the page uses the agent's own model (`agent.model` and the Anthropic Secret). A LiteLLM proxy address must be in `agent.allowedLlmHosts`. The key travels from Grafana to the agent on every request, so use TLS between them outside a single trusted cluster network.

Helm prints the exact `agentUrl` after the install. How you add the init container, environment variables, and provisioning file depends on how you deploy Grafana, for example the keys of the official Grafana chart. The image copies its files to `/plugin`, so mount the Grafana plugins volume there and into Grafana at `/var/lib/grafana/plugins/kekaflow-chat-app`.

> **Note:** The data access caveats in the [security section](../../docs/sources/developers/chat-assistant.md#security) apply: the agent reads with the permissions of mcp-grafana, not of the person asking.

## Versioning

On every successful run of the tests on `main`, the `Helm release (chat agent)` workflow:

1. Picks the bump level from the commit message: `type!:` or a `BREAKING CHANGE` line is major, `feat:` is minor, anything else is patch.
2. Builds and pushes the agent and plugin images with the new version as the tag.
3. Updates `version` and `appVersion` in `Chart.yaml` and `image.tag` and `image.repository` in `values.yaml`, and commits the change.

The workflow pushes to `ghcr.io/<owner>` by default. Set the repository variable `CHAT_AGENT_REGISTRY`, and the secrets `CHAT_AGENT_REGISTRY_USERNAME` and `CHAT_AGENT_REGISTRY_PASSWORD`, to use another registry.

## Status

The chart is linted and rendered with every values file in CI, and the agent image is verified under the pod's security settings. It hasn't been installed on a live cluster yet, so check the Grafana wiring against your Grafana deployment.

## Test

The chart tests run in a container and cover rendering with every values file, the secret handling, the pod hardening, and the version bump script:

```sh
docker compose -f tests/docker-compose.yml run --rm helm-chart
```
