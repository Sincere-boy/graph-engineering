#!/usr/bin/env bash
set -euo pipefail

GE_ROOT_DIR="${GE_ROOT_DIR:-$HOME/.graph_engineering}"
GE_REPOSITORY_URL="${GE_REPOSITORY_URL:-https://github.com/Sincere-boy/graph-engineering.git}"
GE_SOURCE_DIR="$GE_ROOT_DIR/graph-engineering"
GE_CONDA_BIN="${GE_CONDA_BIN:-$GE_ROOT_DIR/miniconda3/bin/conda}"

if [[ ! -d "$GE_SOURCE_DIR/.git" ]]; then
  mkdir -p "$GE_ROOT_DIR"
  gh repo clone "$GE_REPOSITORY_URL" "$GE_SOURCE_DIR"
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
ln -sfn "$GE_ROOT_DIR/miniconda3/envs/graph-engineering/bin/graphctl" "$HOME/.local/bin/graphctl"
sudo -n /usr/bin/docker-compose -f "$GE_SOURCE_DIR/compose.yaml" up -d mongodb

mkdir -p "$HOME/.config/systemd/user"
cp "$GE_SOURCE_DIR/deploy/graph-engineering.service" "$HOME/.config/systemd/user/graph-engineering.service"
systemctl --user daemon-reload
systemctl --user enable --now graph-engineering.service
