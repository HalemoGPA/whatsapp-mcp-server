#!/usr/bin/env bash
# Run the CI jobs from .github/workflows/ci.yml locally, each in a clean,
# throwaway Docker container. Needs only Docker; no Go, Python or uv on the host.
#
#   tests/ci-docker.sh            # check the checkout this script lives in
#   tests/ci-docker.sh <path>     # check another checkout
#
# The checkout is mounted read-only and copied inside each container, so the
# host tree is never modified. Containers run with --rm and no volumes are
# created. Keep the steps in step with ci.yml: if a CI step changes, change it
# here too.
#
# Exit status: 0 all jobs passed, 1 a job failed, 2 no job failed but at least
# one could not run because its image could not be pulled.
#
# Written for bash 3.2 (the macOS default): no associative arrays.

set -uo pipefail

REPO=$(cd "${1:-$(dirname "$0")/..}" && pwd)
# Use the Go version the bridge's go.mod declares, as setup-go does in CI.
GOVER=$(awk '/^go /{split($2, v, "."); print v[1] "." v[2]}' "$REPO/whatsapp-bridge/go.mod")
SUMMARY=""
rc=0

ensure_image() { # <image>: already present, or pulled within three tries
  docker image inspect "$1" >/dev/null 2>&1 && return 0
  local try
  for try in 1 2 3; do
    docker pull -q "$1" >/dev/null && return 0
    echo "pull of $1 failed (attempt $try of 3)" >&2
    sleep $((try * 5))
  done
  return 1
}

run_job() { # <name> <image> <script>
  local name=$1 image=$2 script=$3
  echo
  echo "=== $name ($image) ==="
  # A registry timeout is not a CI failure. Report it apart, so nobody goes
  # looking for a bug in the code that never ran.
  if ! ensure_image "$image"; then
    SUMMARY="$SUMMARY$(printf '  %-26s NOT RUN (could not pull image)' "$name")\n"
    [ "$rc" -eq 0 ] && rc=2
    return
  fi
  if docker run --rm -v "$REPO":/src:ro "$image" bash -c "$script"; then
    SUMMARY="$SUMMARY$(printf '  %-26s pass' "$name")\n"
  else
    SUMMARY="$SUMMARY$(printf '  %-26s FAIL' "$name")\n"
    rc=1
  fi
}

run_job "go (lint + test + build)" "golang:${GOVER}-bookworm" '
  set -e; cp -a /src /w; cd /w/whatsapp-bridge
  echo "-- vet";         go vet -tags sqlite_fts5 ./...
  echo "-- staticcheck"; go run honnef.co/go/tools/cmd/staticcheck@2025.1 -tags sqlite_fts5 ./...
  echo "-- test";        go test -tags sqlite_fts5 ./...
  echo "-- build";       go build -tags sqlite_fts5 -trimpath -ldflags="-s -w" -o /dev/null .'

# A host .venv is skipped: it holds host-platform binaries the container cannot use.
run_job "python (lint + tests)" "ghcr.io/astral-sh/uv:python3.12-bookworm-slim" '
  set -e; mkdir /w; cd /src
  tar --exclude=./.venv --exclude="*/.venv" -cf - . | tar -xf - -C /w; cd /w
  echo "-- ruff";  uvx ruff@0.8 check .
  echo "-- sync";  uv sync --locked --project whatsapp-mcp-server
  echo "-- tests"; uv run --locked --project whatsapp-mcp-server pytest -q'

# Same gitleaks version and full-history scan as CI.
run_job "secret scan" "golang:${GOVER}-bookworm" '
  set -e; cp -a /src /w; cd /w; git config --global --add safe.directory /w
  arch=$(dpkg --print-architecture | sed s/amd64/x64/)
  curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v8.24.3/gitleaks_8.24.3_linux_${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks
  gitleaks git . --no-banner --redact --exit-code 1'

echo
echo "=== summary ($(git -C "$REPO" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO" rev-parse --short HEAD)) ==="
printf "$SUMMARY"
exit $rc
