#!/bin/sh
# Checks the grafana-chat-agent chart renders safely with every values file.
# Runs in the alpine/helm image (see tests/docker-compose.yml, service helm-chart).
set -u

CHART=/chart
fail=0
pass=0

ok()   { pass=$((pass + 1)); echo "ok   - $1"; }
bad()  { fail=$((fail + 1)); echo "FAIL - $1"; }

# check "name" <command...>: passes when the command succeeds.
check() { name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
# refuse "name" <command...>: passes when the command fails.
refuse() { name=$1; shift; if "$@" >/dev/null 2>&1; then bad "$name"; else ok "$name"; fi; }

SECRETS="--set auth.token=test-token-value --set anthropic.apiKey=sk-test-key-value"
EXISTING="--set auth.existingSecret=my-auth --set anthropic.existingSecret=my-anthropic"

render() { helm template rel "$CHART" "$@"; }

check  "lint with the dev values"  helm lint "$CHART" -f "$CHART/values-dev.yaml" $SECRETS
check  "lint with the prod values" helm lint "$CHART" -f "$CHART/values-prod.yaml"
check  "lint with defaults and secrets" helm lint "$CHART" $SECRETS

refuse "no secrets and no existing secrets fails the install" render
refuse "token alone is not enough" render --set auth.token=x
check  "existing secrets need no inline values" render $EXISTING

# Credentials never appear in the files that ship with the chart.
refuse "default values hold no credentials" grep -Eq '^\s*(token|apiKey):\s*"?[^" ]+' "$CHART/values.yaml"
refuse "values-prod holds no credentials" grep -Eq '(token|apiKey):\s*"?[A-Za-z0-9]' "$CHART/values-prod.yaml"

# With existing secrets the rendered output must not contain a Secret or any key material.
render $EXISTING > /tmp/existing.yaml
refuse "no Secret object is created for existing secrets" grep -q '^kind: Secret' /tmp/existing.yaml
check  "pods read the token from the existing secret" grep -q 'name: my-auth' /tmp/existing.yaml
check  "pods read the API key from the existing secret" grep -q 'name: my-anthropic' /tmp/existing.yaml

# With inline values the secrets are created, and the values never leak into the pod spec.
render $SECRETS > /tmp/inline.yaml
check  "inline values create the secrets" grep -q '^kind: Secret' /tmp/inline.yaml
awk '/^kind: Deployment/,/^---/' /tmp/inline.yaml > /tmp/deployment.yaml
refuse "the token is not written into the deployment" grep -q 'test-token-value' /tmp/deployment.yaml
refuse "the API key is not written into the deployment" grep -q 'sk-test-key-value' /tmp/deployment.yaml

# Pod hardening.
check  "runs as non-root"             grep -q 'runAsNonRoot: true' /tmp/deployment.yaml
check  "runs with a numeric uid"      grep -q 'runAsUser: 10001' /tmp/deployment.yaml
check  "root filesystem is read-only" grep -q 'readOnlyRootFilesystem: true' /tmp/deployment.yaml
check  "privilege escalation is off"  grep -q 'allowPrivilegeEscalation: false' /tmp/deployment.yaml
check  "all capabilities are dropped" grep -q -- '- "ALL"\|- ALL' /tmp/deployment.yaml
check  "has a readiness probe"        grep -q 'readinessProbe' /tmp/deployment.yaml
check  "has a liveness probe"         grep -q 'livenessProbe' /tmp/deployment.yaml
check  "does not mount the service account token" grep -q 'automountServiceAccountToken: false' /tmp/deployment.yaml

# Behaviour defaults.
check  "writes are off by default" grep -A1 'CHAT_AGENT_ALLOW_WRITES' /tmp/deployment.yaml
grep -A1 'CHAT_AGENT_ALLOW_WRITES' /tmp/deployment.yaml | grep -q '"false"' && ok "write tools are blocked by default" || bad "write tools are blocked by default"
render $SECRETS --set agent.allowWrites=true | grep -A1 'CHAT_AGENT_ALLOW_WRITES' | grep -q '"true"' && ok "writes can be switched on" || bad "writes can be switched on"

# Which LiteLLM proxy hosts the Settings page may choose.
grep -A1 'CHAT_AGENT_ALLOWED_LLM_HOSTS' /tmp/deployment.yaml | grep -q '"api.anthropic.com"' && ok "only the Anthropic API is allowed by default" || bad "only the Anthropic API is allowed by default"
render $SECRETS --set 'agent.allowedLlmHosts={api.anthropic.com,litellm.internal:4000}' | grep -A1 'CHAT_AGENT_ALLOWED_LLM_HOSTS' | grep -q '"api.anthropic.com,litellm.internal:4000"' && ok "extra LLM hosts can be allowed" || bad "extra LLM hosts can be allowed"

# Environments.
render -f "$CHART/values-prod.yaml" > /tmp/prod.yaml
check  "prod renders a network policy" grep -q '^kind: NetworkPolicy' /tmp/prod.yaml
check  "prod uses the existing secrets" grep -q 'name: grafana-chat-agent-auth' /tmp/prod.yaml
render -f "$CHART/values-dev.yaml" $SECRETS > /tmp/dev.yaml
refuse "dev renders no network policy" grep -q '^kind: NetworkPolicy' /tmp/dev.yaml
check  "dev pulls the image every time" grep -q 'imagePullPolicy: Always' /tmp/dev.yaml

# The image tag is what CI updates, and it must reach the pod.
render $SECRETS --set image.tag=9.9.9 | grep -q 'image: "kekaflow/grafana-chat-agent:9.9.9"' && ok "image tag reaches the deployment" || bad "image tag reaches the deployment"

# Chart metadata that CI bumps must be valid semver.
grep -Eq '^version: [0-9]+\.[0-9]+\.[0-9]+$' "$CHART/Chart.yaml" && ok "chart version is semver" || bad "chart version is semver"
grep -Eq '^appVersion: "[0-9]+\.[0-9]+\.[0-9]+"$' "$CHART/Chart.yaml" && ok "appVersion is semver" || bad "appVersion is semver"
grep -Eq '^  tag: "[0-9]+\.[0-9]+\.[0-9]+"' "$CHART/values.yaml" && ok "image tag in values.yaml is a version" || bad "image tag in values.yaml is a version"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
