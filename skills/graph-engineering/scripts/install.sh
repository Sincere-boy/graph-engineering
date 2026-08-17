#!/usr/bin/env bash
set -euo pipefail

GE_ROOT_DIR="${GE_ROOT_DIR:-$HOME/.graph_engineering}"
GE_REPOSITORY_URL="${GE_REPOSITORY_URL:-https://github.com/Sincere-boy/graph-engineering.git}"
GE_SOURCE_DIR="${GE_SOURCE_DIR:-$GE_ROOT_DIR/graph-engineering}"
GE_CONDA_BIN="${GE_CONDA_BIN:-$GE_ROOT_DIR/miniconda3/bin/conda}"

if [[ ! -d "$GE_SOURCE_DIR/.git" ]]; then
  mkdir -p "$(dirname "$GE_SOURCE_DIR")"
  git clone "$GE_REPOSITORY_URL" "$GE_SOURCE_DIR"
fi

if [[ ! -x "$GE_CONDA_BIN" ]]; then
  echo "Missing Conda at $GE_CONDA_BIN" >&2
  exit 2
fi

if ! "$GE_CONDA_BIN" env list | awk '{print $1}' | grep -qx graph-engineering; then
  "$GE_CONDA_BIN" create -y --override-channels -c conda-forge -n graph-engineering python=3.12
fi

"$GE_CONDA_BIN" run -n graph-engineering python -m pip install -e "$GE_SOURCE_DIR[dev]"
mkdir -p "$HOME/.local/bin"
GE_GRAPHCTL_SOURCE="$GE_ROOT_DIR/miniconda3/envs/graph-engineering/bin/graphctl"
GE_GRAPHCTL_TARGET="$HOME/.local/bin/graphctl"
if [[ -L "$GE_GRAPHCTL_TARGET" ]]; then
  if [[ "$(readlink -f "$GE_GRAPHCTL_TARGET")" != "$(readlink -f "$GE_GRAPHCTL_SOURCE")" ]]; then
    echo "refusing to overwrite existing graphctl link: $GE_GRAPHCTL_TARGET" >&2
    exit 2
  fi
elif [[ -e "$GE_GRAPHCTL_TARGET" ]]; then
  echo "refusing to overwrite existing graphctl file: $GE_GRAPHCTL_TARGET" >&2
  exit 2
else
  ln -s "$GE_GRAPHCTL_SOURCE" "$GE_GRAPHCTL_TARGET"
fi

if docker compose version >/dev/null 2>&1; then
  GE_COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  GE_COMPOSE=(docker-compose)
else
  echo "Docker Compose is unavailable" >&2
  exit 2
fi
"${GE_COMPOSE[@]}" -f "$GE_SOURCE_DIR/compose.yaml" up -d --wait mongodb

GE_BOTMUX_CLI="${GE_BOTMUX_CLI:-$(command -v botmux || true)}"
if [[ -z "$GE_BOTMUX_CLI" || ! -x "$GE_BOTMUX_CLI" ]]; then
  echo "botmux executable is unavailable" >&2
  exit 2
fi
if [[ "$GE_BOTMUX_CLI" =~ [[:space:]] ]]; then
  echo "botmux executable path cannot contain whitespace: $GE_BOTMUX_CLI" >&2
  exit 2
fi

mkdir -p "$HOME/.config/systemd/user"
cp "$GE_SOURCE_DIR/deploy/graph-engineering.service" "$HOME/.config/systemd/user/graph-engineering.service"
mkdir -p "$HOME/.config/systemd/user/graph-engineering.service.d"
printf '[Service]\nEnvironment=GE_BOTMUX_CLI=%s\n' "$GE_BOTMUX_CLI" \
  > "$HOME/.config/systemd/user/graph-engineering.service.d/override.conf"
systemctl --user daemon-reload
systemctl --user enable --now graph-engineering.service
