#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
  echo "usage: install-main-skill.sh [repository-root]" >&2
  exit 2
fi

GE_SETUP_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GE_REPOSITORY_DIR="${1:-$(cd -- "$GE_SETUP_SCRIPT_DIR/../../.." && pwd)}"
GE_SKILL_SOURCE="$GE_REPOSITORY_DIR/skills/graph-engineering"
GE_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
GE_SKILL_TARGET="$GE_CODEX_HOME/skills/graph-engineering"

if [[ ! -f "$GE_SKILL_SOURCE/SKILL.md" ]]; then
  echo "graph-engineering skill is missing from repository: $GE_SKILL_SOURCE" >&2
  exit 2
fi

if [[ -L "$GE_SKILL_TARGET" ]]; then
  if [[ "$(readlink -f "$GE_SKILL_TARGET")" == "$(readlink -f "$GE_SKILL_SOURCE")" ]]; then
    echo "graph-engineering skill already linked: $GE_SKILL_TARGET"
    exit 0
  fi
  echo "refusing to overwrite existing skill link: $GE_SKILL_TARGET" >&2
  exit 2
fi
if [[ -e "$GE_SKILL_TARGET" ]]; then
  echo "refusing to overwrite existing skill directory: $GE_SKILL_TARGET" >&2
  exit 2
fi

mkdir -p "$GE_CODEX_HOME/skills"
ln -s "$GE_SKILL_SOURCE" "$GE_SKILL_TARGET"
echo "installed graph-engineering skill: $GE_SKILL_TARGET -> $GE_SKILL_SOURCE"
