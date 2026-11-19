#!/bin/sh
# Bumps a chart's version and image tag after a successful commit to main.
#
#   helm/bump.sh <chart-dir> --message "<commit message>" [--repository <image repository>]
#
# The level follows the commit message (Conventional Commits):
#   "type!: ..." or a "BREAKING CHANGE" line  -> major
#   "feat: ..." or "feat(scope): ..."         -> minor
#   anything else                             -> patch
#
# Updates `version` and `appVersion` in Chart.yaml and `image.tag` in values.yaml, and
# prints the new version. Plain POSIX sh, so it runs in the same minimal container as the tests.
set -eu

chart=""
message=""
repository=""

while [ $# -gt 0 ]; do
  case "$1" in
    --message)    message=${2-}; shift 2 ;;
    --repository) repository=${2-}; shift 2 ;;
    -*)           echo "unknown option: $1" >&2; exit 2 ;;
    *)            chart=$1; shift ;;
  esac
done

[ -n "$chart" ] || { echo "usage: bump.sh <chart-dir> --message <message> [--repository <repo>]" >&2; exit 2; }
[ -f "$chart/Chart.yaml" ] && [ -f "$chart/values.yaml" ] || { echo "not a chart directory: $chart" >&2; exit 2; }

current=$(sed -n 's/^version: \([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$/\1/p' "$chart/Chart.yaml")
[ -n "$current" ] || { echo "Chart.yaml has no semver version" >&2; exit 1; }

first_line=$(printf '%s\n' "$message" | sed -n '1p')
if printf '%s\n' "$first_line" | grep -Eq '^[a-zA-Z]+(\([^)]*\))?!:' || printf '%s\n' "$message" | grep -Eq '^BREAKING[ -]CHANGE'; then
  level=major
elif printf '%s\n' "$first_line" | grep -Eq '^feat(\([^)]*\))?:'; then
  level=minor
else
  level=patch
fi

major=${current%%.*}
rest=${current#*.}
minor=${rest%%.*}
patch=${rest#*.}

case "$level" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
esac
next="$major.$minor.$patch"

# Write to temp files and move them over, so a failure never leaves a half-edited chart.
sed "s/^version: .*/version: $next/; s/^appVersion: .*/appVersion: \"$next\"/" "$chart/Chart.yaml" > "$chart/Chart.yaml.new"
sed "s/^  tag: .*/  tag: \"$next\"/" "$chart/values.yaml" > "$chart/values.yaml.new"
if [ -n "$repository" ]; then
  sed "s#^  repository: .*#  repository: $repository#" "$chart/values.yaml.new" > "$chart/values.yaml.new2"
  mv "$chart/values.yaml.new2" "$chart/values.yaml.new"
fi
mv "$chart/Chart.yaml.new" "$chart/Chart.yaml"
mv "$chart/values.yaml.new" "$chart/values.yaml"

echo "$next"
