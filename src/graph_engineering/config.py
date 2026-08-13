from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ConfigError(ValueError):
    pass


class WorkspaceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    repository: Path
    version: int = Field(default=1, ge=1)
    name: str | None = None


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(default="", max_length=100_000)
    skills: list[str] = Field(default_factory=list)
    system_managed: bool = False


class StateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["activate", "pause", "complete"]
    target: str | None = None


class StateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=100)
    allowed_writers: list[str] = Field(min_length=1)
    action: StateAction


ORGANIZER_PROTOCOL = """你是声明式工程状态图的组织者。路由由后端配置唯一决定，你不得改变目标。
你使用工作区群会话，不创建固定话题。收到用户需求时，只通过 graphctl 写入一个组织者有权写入的
状态事件并在群内答复用户；不要直接 @ 任何执行 Agent，也不要自行调用 botmux dispatch，后端会在
消费 Event Log 后再次调用组织者群 Session。只有收到 graph-engineering 投递 envelope 时，才将事件
批次压缩为清晰任务，保留 delivery_id 与 event_id，并按后端指令投递到指定执行 Agent 的固定话题。
遇到人工回复时，用 graphctl 写入 human_resolved 并引用原 human_required event_id。"""

RESERVED_STATE_IDS = {
    "human_required",
    "human_resolved",
    "closed",
    "待人工",
    "人工已处理",
    "关闭",
}


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    workspace: WorkspaceIdentity
    agents: dict[str, AgentConfig]
    states: dict[str, StateConfig]

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> WorkspaceConfig:
        if isinstance(obj, cls):
            config = obj
        else:
            if not isinstance(obj, dict):
                raise ConfigError("configuration must be a mapping")
            raw = dict(obj)
            raw_agents = dict(raw.get("agents") or {})
            organizer = dict(raw_agents.get("organizer") or {})
            user_prompt = str(organizer.get("prompt", "")).strip()
            if organizer.get("system_managed") is True and user_prompt.startswith(
                ORGANIZER_PROTOCOL
            ):
                effective_prompt = user_prompt
            else:
                effective_prompt = ORGANIZER_PROTOCOL + (
                    f"\n\n用户补充：\n{user_prompt}" if user_prompt else ""
                )
            organizer.update(
                {
                    "display_name": organizer.get("display_name", "组织者"),
                    "prompt": effective_prompt,
                    "skills": organizer.get("skills", []),
                    "system_managed": True,
                }
            )
            raw_agents["organizer"] = organizer
            raw["agents"] = raw_agents
            config = super().model_validate(raw, *args, **kwargs)
        config._validate_graph()
        return config

    def _validate_graph(self) -> None:
        if not self.agents:
            raise ConfigError("at least one agent is required")
        unknown_reserved = RESERVED_STATE_IDS.intersection(self.states)
        reserved_display = {state.display_name for state in self.states.values()}.intersection(
            RESERVED_STATE_IDS
        )
        if unknown_reserved or reserved_display:
            raise ConfigError(
                "reserved control states are engine-managed; remove "
                "human_required/human_resolved/closed/待人工/人工已处理/关闭"
            )
        agent_ids = set(self.agents)
        errors: list[str] = []
        managed_agents = [
            agent_id
            for agent_id, agent in self.agents.items()
            if agent.system_managed and agent_id != "organizer"
        ]
        if managed_agents:
            errors.append(f"only organizer may be system-managed: {managed_agents}")
        terminal = False
        for state_id, state in self.states.items():
            missing_writers = set(state.allowed_writers) - agent_ids
            if missing_writers:
                errors.append(f"state {state_id} has unknown writers: {sorted(missing_writers)}")
            if state.action.type == "activate":
                if not state.action.target or state.action.target not in agent_ids:
                    errors.append(f"state {state_id} has unknown target: {state.action.target}")
            elif state.action.target is not None:
                errors.append(f"state {state_id} must not set target for {state.action.type}")
            if state.action.type == "complete":
                terminal = True
        if not terminal:
            errors.append("at least one terminal complete state is required")
        if not self.workspace.repository.is_absolute():
            errors.append("workspace repository must be an absolute path")
        if errors:
            raise ConfigError("; ".join(errors))

    @property
    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def from_yaml(cls, path: Path) -> WorkspaceConfig:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read YAML: {exc}") from exc
        return cls.model_validate(raw)

    @classmethod
    def from_yaml_text(cls, content: str) -> WorkspaceConfig:
        try:
            raw = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ConfigError(f"cannot parse YAML: {exc}") from exc
        return cls.model_validate(raw)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
