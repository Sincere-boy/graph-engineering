#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: activate-workspace.sh /absolute/workspace.yaml" >&2
  exit 2
fi

GE_CONFIG_PATH="$1"
if [[ "$GE_CONFIG_PATH" != /* ]]; then
  echo "workspace config must be an absolute path" >&2
  exit 2
fi

GE_WORKSPACE_ID="$(graphctl config validate "$GE_CONFIG_PATH" | python -c 'import json,sys; print(json.load(sys.stdin)["workspace_id"])')"
graphctl workspace register "$GE_CONFIG_PATH"
graphctl workspace provision "$GE_WORKSPACE_ID"
graphctl workspace resume "$GE_WORKSPACE_ID"
graphctl workspace status "$GE_WORKSPACE_ID"
