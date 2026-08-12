from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.registry import WorkspaceRegistry


@pytest.mark.asyncio
async def test_registry_freezes_active_config_and_versions_paused_updates(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    first = WorkspaceConfig.model_validate(valid_config(tmp_path))

    runtime = await registry.register(first)
    await registry.resume(first.workspace.id)
    changed_raw = valid_config(tmp_path)
    changed_raw["agents"]["maker"]["prompt"] = "changed"
    changed = WorkspaceConfig.model_validate(changed_raw)

    with pytest.raises(ConfigError, match="paused"):
        await registry.register(changed)

    await registry.pause(first.workspace.id)
    with pytest.raises(ConfigError, match="version"):
        await registry.register(changed)

    changed_raw["workspace"]["version"] = 2
    second = WorkspaceConfig.model_validate(changed_raw)
    updated = await registry.register(second)

    assert runtime.config_hash == first.content_hash
    assert updated.config_version == 2
    assert (registry.workspace_dir(first.workspace.id) / "config-v1.yaml").exists()
    assert (registry.workspace_dir(first.workspace.id) / "config-v2.yaml").exists()


def test_registry_renders_generic_mermaid_and_agent_prompt(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))

    diagram = registry.render_mermaid(config)
    prompt = registry.render_agent_prompt(config, "maker")

    assert 'maker["任意制造者"]' in diagram
    assert 'checker["任意检查者"]' in diagram
    assert 'maker -->|"进入检查"| checker' in diagram
    assert "graphctl event append" in prompt
    assert "inspect" in prompt
    assert "done" not in prompt
