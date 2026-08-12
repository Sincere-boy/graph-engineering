from __future__ import annotations

import re
from pathlib import Path

from graph_engineering.config import ConfigError, WorkspaceConfig


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _tables(markdown: str) -> dict[str, list[dict[str, str]]]:
    current = ""
    raw_tables: dict[str, list[list[str]]] = {}
    for line in markdown.splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip().lower()
            current = {
                "agents": "agents",
                "角色": "agents",
                "states": "states",
                "状态": "states",
            }.get(title, "")
            continue
        if current and line.strip().startswith("|"):
            raw_tables.setdefault(current, []).append(_cells(line))

    result: dict[str, list[dict[str, str]]] = {}
    for name, rows in raw_tables.items():
        if len(rows) < 2:
            continue
        header = rows[0]
        if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in rows[1]):
            raise ConfigError(f"Markdown {name} table is missing a separator row")
        result[name] = [dict(zip(header, row, strict=False)) for row in rows[2:] if any(row)]
    return result


def _list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，]", value) if item.strip()]


def config_from_markdown(
    markdown: str,
    *,
    workspace_id: str,
    repository: Path,
    version: int = 1,
    name: str | None = None,
) -> WorkspaceConfig:
    tables = _tables(markdown)
    if not tables.get("agents") or not tables.get("states"):
        raise ConfigError("Markdown must contain Agents and States tables")
    agents = {
        row["id"]: {
            "display_name": row.get("display_name") or row["id"],
            "prompt": row.get("prompt", ""),
            "skills": _list(row.get("skills", "")),
        }
        for row in tables["agents"]
        if row.get("id")
    }
    states = {}
    for row in tables["states"]:
        state_id = row.get("id", "")
        if not state_id:
            continue
        action = row.get("action", "")
        action_value: dict[str, str] = {"type": action}
        if action == "activate":
            action_value["target"] = row.get("target", "")
        states[state_id] = {
            "display_name": row.get("display_name") or state_id,
            "allowed_writers": _list(row.get("allowed_writers", "")),
            "action": action_value,
        }
    return WorkspaceConfig.model_validate(
        {
            "schema_version": 1,
            "workspace": {
                "id": workspace_id,
                "name": name,
                "version": version,
                "repository": str(repository.resolve()),
            },
            "agents": agents,
            "states": states,
        }
    )
