from pathlib import Path

import pytest

from graph_engineering.config import ConfigError, WorkspaceConfig


def valid_config(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "workspace": {"id": "arbitrary-flow", "repository": str(tmp_path)},
        "agents": {
            "maker": {"display_name": "任意制造者", "prompt": "完成工作", "skills": []},
            "checker": {"display_name": "任意检查者", "prompt": "检查工作"},
        },
        "states": {
            "begin": {
                "display_name": "可以任意命名",
                "allowed_writers": ["organizer"],
                "action": {"type": "activate", "target": "maker"},
            },
            "inspect": {
                "display_name": "进入检查",
                "allowed_writers": ["maker", "checker"],
                "action": {"type": "activate", "target": "checker"},
            },
            "redo": {
                "display_name": "形成循环",
                "allowed_writers": ["checker"],
                "action": {"type": "activate", "target": "maker"},
            },
            "done": {
                "display_name": "结束",
                "allowed_writers": ["checker"],
                "action": {"type": "complete"},
            },
        },
    }


def test_config_accepts_arbitrary_agents_states_and_cycles(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))

    assert config.agents["organizer"].system_managed is True
    assert config.states["redo"].action.target == "maker"
    assert config.content_hash == config.model_validate(valid_config(tmp_path)).content_hash


@pytest.mark.parametrize(
    "reserved",
    ["human_required", "human_resolved", "closed", "待人工", "人工已处理", "关闭"],
)
def test_config_rejects_reserved_human_states(tmp_path: Path, reserved: str) -> None:
    raw = valid_config(tmp_path)
    raw["states"][reserved] = raw["states"].pop("begin")

    with pytest.raises(ConfigError, match="reserved"):
        WorkspaceConfig.model_validate(raw)


def test_config_rejects_unknown_target_and_writer(tmp_path: Path) -> None:
    raw = valid_config(tmp_path)
    raw["states"]["begin"]["action"]["target"] = "missing"
    raw["states"]["inspect"]["allowed_writers"].append("ghost")

    with pytest.raises(ConfigError, match="missing|ghost"):
        WorkspaceConfig.model_validate(raw)


def test_config_requires_terminal_state(tmp_path: Path) -> None:
    raw = valid_config(tmp_path)
    raw["states"].pop("done")

    with pytest.raises(ConfigError, match="terminal"):
        WorkspaceConfig.model_validate(raw)


def test_config_yaml_roundtrip_preserves_organizer_protocol_and_hash(tmp_path: Path) -> None:
    first = WorkspaceConfig.model_validate(valid_config(tmp_path))

    second = WorkspaceConfig.from_yaml_text(first.to_yaml())

    assert second.content_hash == first.content_hash
    assert second.agents["organizer"].prompt.count("你是声明式工程状态图的组织者") == 1
