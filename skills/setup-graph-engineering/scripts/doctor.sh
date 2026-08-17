#!/usr/bin/env bash
set -uo pipefail

if [[ $# -gt 1 ]]; then
  echo "usage: doctor.sh [repository-root]" >&2
  exit 2
fi

GE_DOCTOR_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GE_REPOSITORY_DIR="${1:-$(cd -- "$GE_DOCTOR_SCRIPT_DIR/../../.." && pwd)}"
GE_ROOT_DIR="${GE_ROOT_DIR:-$HOME/.graph_engineering}"
GE_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
GE_CONDA_BIN="${GE_CONDA_BIN:-$GE_ROOT_DIR/miniconda3/bin/conda}"
GE_PYTHON_BIN="$GE_ROOT_DIR/miniconda3/envs/graph-engineering/bin/python"
GE_GRAPHCTL_BIN="$GE_ROOT_DIR/miniconda3/envs/graph-engineering/bin/graphctl"
GE_EXPECTED_SKILL="$GE_REPOSITORY_DIR/skills/graph-engineering"
GE_INSTALLED_SKILL="$GE_CODEX_HOME/skills/graph-engineering"
GE_PROBLEMS=0

ge_ok() {
  printf 'OK %s %s\n' "$1" "$2"
}

ge_missing() {
  printf 'MISSING %s %s\n' "$1" "$2"
  GE_PROBLEMS=$((GE_PROBLEMS + 1))
}

if [[ -e "$GE_REPOSITORY_DIR/.git" \
  && -f "$GE_REPOSITORY_DIR/pyproject.toml" \
  && -f "$GE_REPOSITORY_DIR/compose.yaml" \
  && -f "$GE_REPOSITORY_DIR/deploy/graph-engineering.service" \
  && -f "$GE_EXPECTED_SKILL/SKILL.md" \
  && -f "$GE_REPOSITORY_DIR/skills/setup-graph-engineering/SKILL.md" ]]; then
  ge_ok repository "$GE_REPOSITORY_DIR"
else
  ge_missing repository "expected complete graph-engineering clone at $GE_REPOSITORY_DIR"
fi

if [[ -e "$GE_INSTALLED_SKILL" || -L "$GE_INSTALLED_SKILL" ]] \
  && [[ "$(readlink -f "$GE_INSTALLED_SKILL" 2>/dev/null)" == "$(readlink -f "$GE_EXPECTED_SKILL" 2>/dev/null)" ]]; then
  ge_ok codex-skill "$GE_INSTALLED_SKILL"
else
  ge_missing codex-skill "run scripts/install-main-skill.sh"
fi

if [[ -x "$GE_CONDA_BIN" ]]; then
  ge_ok conda "$GE_CONDA_BIN"
  if "$GE_CONDA_BIN" env list 2>/dev/null | awk '{print $1}' | grep -qx graph-engineering; then
    ge_ok python-env graph-engineering
    GE_PYTHON_VERSION="$("$GE_PYTHON_BIN" --version 2>&1 || true)"
    if [[ "$GE_PYTHON_VERSION" == "Python 3.12."* ]]; then
      ge_ok python-version "$GE_PYTHON_VERSION"
    else
      ge_missing python-version "expected Python 3.12, found ${GE_PYTHON_VERSION:-unknown}"
    fi
  else
    ge_missing python-env "graph-engineering Conda environment"
    ge_missing python-version "expected Python 3.12"
  fi
else
  ge_missing conda "$GE_CONDA_BIN"
  ge_missing python-env "graph-engineering Conda environment"
  ge_missing python-version "expected Python 3.12"
fi

if [[ -x "$GE_GRAPHCTL_BIN" ]]; then
  ge_ok graphctl "$GE_GRAPHCTL_BIN"
else
  ge_missing graphctl "$GE_GRAPHCTL_BIN"
fi

GE_DOCKER_READY=0
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ge_ok docker-compose "docker compose"
  if docker info >/dev/null 2>&1; then
    ge_ok docker-daemon reachable
    GE_DOCKER_READY=1
  else
    ge_missing docker-daemon "daemon unavailable or current user lacks access"
  fi
else
  ge_missing docker-compose "Docker Engine with Compose v2"
fi

if [[ "$GE_DOCKER_READY" -eq 1 ]]; then
  GE_MONGO_STATUS="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' graph-engineering-mongodb 2>/dev/null || true)"
  if [[ "$GE_MONGO_STATUS" == "healthy" || "$GE_MONGO_STATUS" == "running" ]]; then
    ge_ok mongodb "$GE_MONGO_STATUS"
  else
    ge_missing mongodb "container is ${GE_MONGO_STATUS:-absent}"
  fi
else
  ge_missing mongodb "cannot inspect without Docker"
fi

GE_BOTMUX_CLI="${GE_BOTMUX_CLI:-$(command -v botmux 2>/dev/null || true)}"
GE_NODE_VERSION="$(node --version 2>/dev/null || true)"
GE_NODE_MAJOR="${GE_NODE_VERSION#v}"
GE_NODE_MAJOR="${GE_NODE_MAJOR%%.*}"
if [[ "$GE_NODE_MAJOR" =~ ^[0-9]+$ ]] && [[ "$GE_NODE_MAJOR" -ge 22 ]] \
  && command -v npm >/dev/null 2>&1; then
  ge_ok node "$GE_NODE_VERSION"
else
  ge_missing node "Node.js 22 or newer"
fi

if [[ -n "$GE_BOTMUX_CLI" && -x "$GE_BOTMUX_CLI" ]]; then
  ge_ok botmux "$GE_BOTMUX_CLI"
  GE_BOTMUX_VERSION="$("$GE_BOTMUX_CLI" --version 2>/dev/null || true)"
  if [[ "$GE_BOTMUX_VERSION" == "3.13.0" ]]; then
    ge_ok botmux-version "$GE_BOTMUX_VERSION"
  else
    ge_missing botmux-version "expected 3.13.0, found ${GE_BOTMUX_VERSION:-unknown}"
  fi
  if "$GE_BOTMUX_CLI" status >/dev/null 2>&1; then
    ge_ok botmux-daemon running
  else
    ge_missing botmux-daemon "run botmux setup, then botmux start"
  fi
else
  ge_missing botmux "install Node.js 22 and botmux"
  ge_missing botmux-daemon "botmux is unavailable"
fi

if [[ -s "$HOME/.botmux/.dashboard-port" && -s "$HOME/.botmux/.dashboard-token" ]]; then
  ge_ok botmux-dashboard ready
else
  ge_missing botmux-dashboard "finish botmux setup and open the dashboard"
fi

if command -v systemctl >/dev/null 2>&1 \
  && systemctl --user is-active --quiet graph-engineering.service 2>/dev/null; then
  ge_ok backend-service active
else
  ge_missing backend-service "graph-engineering.service is inactive"
fi

if command -v curl >/dev/null 2>&1 \
  && curl --fail --silent --show-error http://127.0.0.1:8765/livez >/dev/null 2>&1; then
  ge_ok livez healthy
else
  ge_missing livez "http://127.0.0.1:8765/livez"
fi
if command -v curl >/dev/null 2>&1 \
  && curl --fail --silent --show-error http://127.0.0.1:8765/readyz >/dev/null 2>&1; then
  ge_ok readyz ready
else
  ge_missing readyz "http://127.0.0.1:8765/readyz"
fi

if [[ "$GE_PROBLEMS" -eq 0 ]]; then
  echo "READY graph-engineering environment"
  exit 0
fi

printf 'NOT_READY %d check(s) failed\n' "$GE_PROBLEMS"
exit 1
