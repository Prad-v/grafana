#!/bin/sh
# Tests helm/bump.sh, which CI runs after every successful commit to main.
set -u

pass=0
fail=0
ok()  { pass=$((pass + 1)); echo "ok   - $1"; }
bad() { fail=$((fail + 1)); echo "FAIL - $1 (got: $2)"; }

# expect <name> <expected> <actual>
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "$3"; fi; }

fresh() {
  rm -rf /tmp/chart && mkdir -p /tmp/chart
  cp /chart/Chart.yaml /chart/values.yaml /tmp/chart/
  sed -i 's/^version: .*/version: 1.4.7/; s/^appVersion: .*/appVersion: "1.4.7"/' /tmp/chart/Chart.yaml
  sed -i 's/^  tag: .*/  tag: "1.4.7"/' /tmp/chart/values.yaml
}

fresh; expect "a fix is a patch bump"            "1.4.8" "$(/bump/bump.sh /tmp/chart --message 'fix: stop dropping the first event')"
fresh; expect "a plain message is a patch bump"  "1.4.8" "$(/bump/bump.sh /tmp/chart --message 'Update docs')"
fresh; expect "a feature is a minor bump"        "1.5.0" "$(/bump/bump.sh /tmp/chart --message 'feat: add cycle detection')"
fresh; expect "a scoped feature is a minor bump" "1.5.0" "$(/bump/bump.sh /tmp/chart --message 'feat(agent): add a tool')"
fresh; expect "a ! marks a breaking change"      "2.0.0" "$(/bump/bump.sh /tmp/chart --message 'feat!: rename the token key')"
fresh; expect "a scoped ! is breaking too"       "2.0.0" "$(/bump/bump.sh /tmp/chart --message 'refactor(api)!: drop v1')"
fresh; expect "a BREAKING CHANGE line is major"  "2.0.0" "$(/bump/bump.sh /tmp/chart --message 'fix: rework sessions

BREAKING CHANGE: sessions are no longer shared')"
fresh; expect "'feat' inside the text is not a feature" "1.4.8" "$(/bump/bump.sh /tmp/chart --message 'fix: the feat: label was wrong')"
fresh; expect "an empty message is a patch bump" "1.4.8" "$(/bump/bump.sh /tmp/chart --message '')"

fresh; /bump/bump.sh /tmp/chart --message 'feat: x' >/dev/null
expect "Chart.yaml version is updated"    "version: 1.5.0"      "$(grep '^version:' /tmp/chart/Chart.yaml)"
expect "appVersion follows the version"   'appVersion: "1.5.0"' "$(grep '^appVersion:' /tmp/chart/Chart.yaml)"
expect "image.tag follows the version"    '  tag: "1.5.0"'       "$(grep '^  tag:' /tmp/chart/values.yaml)"

fresh; before=$(sed -n '/^image:/,/^imagePullSecrets/p' /tmp/chart/values.yaml | grep -c .)
/bump/bump.sh /tmp/chart --message 'fix: x' >/dev/null
after=$(sed -n '/^image:/,/^imagePullSecrets/p' /tmp/chart/values.yaml | grep -c .)
expect "no other line of values.yaml is lost" "$before" "$after"
expect "other tags are untouched" "$(grep -c 'tag:' /chart/values.yaml)" "$(grep -c 'tag:' /tmp/chart/values.yaml)"

fresh; /bump/bump.sh /tmp/chart --message 'fix: x' --repository ghcr.io/acme/grafana-chat-agent >/dev/null
expect "the image repository can be set" "  repository: ghcr.io/acme/grafana-chat-agent" "$(grep '^  repository:' /tmp/chart/values.yaml)"
fresh; /bump/bump.sh /tmp/chart --message 'fix: x' >/dev/null
expect "the repository is kept when not given" "  repository: kekaflow/grafana-chat-agent" "$(grep '^  repository:' /tmp/chart/values.yaml)"

fresh; /bump/bump.sh /tmp/chart --message 'fix: a' >/dev/null; second=$(/bump/bump.sh /tmp/chart --message 'fix: b')
expect "bumps accumulate" "1.4.9" "$second"

fresh; sed -i 's/^version: .*/version: 1.9.9/' /tmp/chart/Chart.yaml
expect "9 rolls over to the next number, not 1.9.10 or 1.10.0 by accident" "1.9.10" "$(/bump/bump.sh /tmp/chart --message 'fix: x')"

fresh; sed -i 's/^version: .*/version: banana/' /tmp/chart/Chart.yaml; cp /tmp/chart/Chart.yaml /tmp/chart.before
if /bump/bump.sh /tmp/chart --message 'fix: x' >/dev/null 2>&1; then bad "a non-semver version is refused" "accepted"; else ok "a non-semver version is refused"; fi
expect "and nothing is changed when it is refused" "$(cat /tmp/chart.before)" "$(cat /tmp/chart/Chart.yaml)"

if /bump/bump.sh /nonexistent --message 'fix: x' >/dev/null 2>&1; then bad "a missing chart is refused" "accepted"; else ok "a missing chart is refused"; fi

# The real chart must be bumpable as it is committed, and the result must still lint.
rm -rf /tmp/real && cp -R /chart /tmp/real
new=$(/bump/bump.sh /tmp/real --message 'feat: something' --repository ghcr.io/acme/grafana-chat-agent)
if helm lint /tmp/real --set auth.token=t --set anthropic.apiKey=k >/dev/null 2>&1; then ok "the bumped real chart ($new) still lints"; else bad "the bumped real chart still lints" "lint failed"; fi
helm template rel /tmp/real --set auth.token=t --set anthropic.apiKey=k | grep -q "image: \"ghcr.io/acme/grafana-chat-agent:$new\"" && ok "the bumped image reference reaches the pod" || bad "the bumped image reference reaches the pod" "missing"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
