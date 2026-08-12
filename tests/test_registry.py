from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.models import Event
from graph_engineering.registry import WorkspaceRegistry


@pytest.mark.asyncio
async def test_registry_freezes_active_config_and_versions_paused_updates(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    first = WorkspaceConfig.model_validate(valid_config(tmp_path))

    runtime = await registry.register(first)
    active = await registry.resume(first.workspace.id)
    active.active_node = "maker"
    await registry.storage.save_runtime(active)
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
    assert updated.active_node == "maker"
    assert (registry.workspace_dir(first.workspace.id) / "config-v1.yaml").exists()
    assert (registry.workspace_dir(first.workspace.id) / "config-v2.yaml").exists()


@pytest.mark.asyncio
async def test_new_config_version_starts_after_existing_event_log(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    first = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(first)
    event_log = registry.event_log(first.workspace.id)
    end = event_log.append(
        Event(
            event_id="old-version-event",
            workspace_id=first.workspace.id,
            config_version=1,
            actor_id="organizer",
            state_id="begin",
            message="old version",
        )
    )
    await registry.pause(first.workspace.id)
    changed_raw = valid_config(tmp_path)
    changed_raw["workspace"]["version"] = 2
    changed_raw["agents"]["maker"]["prompt"] = "version two"

    runtime = await registry.register(WorkspaceConfig.model_validate(changed_raw))

    assert runtime.cursor == end
    assert runtime.event_log_identity == event_log.file_identity()


@pytest.mark.asyncio
async def test_completed_workspace_cannot_be_reopened_via_pause(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    runtime = await registry.register(config)
    runtime.status = "completed"
    await registry.storage.save_runtime(runtime)

    with pytest.raises(ConfigError, match="completed"):
        await registry.pause(config.workspace.id)


@pytest.mark.asyncio
async def test_closed_workspace_stops_until_explicit_resume(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(config)
    await registry.resume(config.workspace.id)

    closed = await registry.close(config.workspace.id)

    assert closed.status == "closed"
    assert (await registry.close(config.workspace.id)).status == "closed"
    with pytest.raises(ConfigError, match="closed"):
        await registry.pause(config.workspace.id)
    assert (await registry.resume(config.workspace.id)).status == "running"


@pytest.mark.asyncio
async def test_config_update_does_not_publish_snapshot_when_event_log_is_corrupt(
    tmp_path: Path,
) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    first = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(first)
    await registry.pause(first.workspace.id)
    registry.event_log(first.workspace.id).path.write_text("{truncated", encoding="utf-8")
    changed_raw = valid_config(tmp_path)
    changed_raw["workspace"]["version"] = 2
    changed_raw["agents"]["maker"]["prompt"] = "version two"

    with pytest.raises(Exception, match="truncated"):
        await registry.register(WorkspaceConfig.model_validate(changed_raw))

    assert registry.load_config(first.workspace.id).content_hash == first.content_hash
    assert not (registry.workspace_dir(first.workspace.id) / "config-v2.yaml").exists()


def test_registry_renders_generic_mermaid_and_agent_prompt(tmp_path: Path) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))

    diagram = registry.render_mermaid(config)
    prompt = registry.render_agent_prompt(config, "maker")

    assert 'maker["任意制造者"]' in diagram
    assert 'checker["任意检查者"]' in diagram
    assert 'maker -->|"进入检查"| checker' in diagram
    assert "graphctl event append" in prompt
    assert "禁止使用 `botmux report`" in prompt
    assert "禁止 @组织者" in prompt
    assert "不得直接向组织者发送消息" in prompt
    assert "禁止创建新话题" in prompt
    assert "inspect" in prompt
    assert "done" not in prompt


def test_agent_prompt_includes_declared_skills(tmp_path: Path) -> None:
    raw = valid_config(tmp_path)
    raw["agents"]["maker"]["skills"] = ["test-driven-development", "deep-code-read"]
    config = WorkspaceConfig.model_validate(raw)

    prompt = WorkspaceRegistry(tmp_path).render_agent_prompt(config, "maker")

    assert "test-driven-development" in prompt
    assert "deep-code-read" in prompt
