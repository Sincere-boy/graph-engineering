from pathlib import Path

import pytest
from test_config import valid_config

from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.models import Event
from graph_engineering.registry import WorkspaceRegistry


def test_organizer_prompt_uses_group_and_workers_use_fixed_topics(tmp_path: Path) -> None:
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    registry = WorkspaceRegistry(tmp_path / "control")

    organizer = registry.render_agent_prompt(config, "organizer")
    worker = registry.render_agent_prompt(config, "maker")

    assert "组织者使用群会话，不创建固定话题" in organizer
    assert "收到用户需求时不要直接 @ 任何执行 Agent" in organizer
    assert "后端会再次调用组织者群 Session" in organizer
    assert "收到用户明确恢复指令" in organizer
    assert "graphctl workspace reopen arbitrary-flow --message" in organizer
    assert "始终在当前固定话题内协作" in worker


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
async def test_closed_workspace_cannot_bypass_audited_reopen_with_resume(
    tmp_path: Path,
) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(config)
    await registry.resume(config.workspace.id)

    closed = await registry.close(config.workspace.id)

    assert closed.status == "closed"
    assert (await registry.close(config.workspace.id)).status == "closed"
    with pytest.raises(ConfigError, match="closed"):
        await registry.pause(config.workspace.id)
    with pytest.raises(ConfigError, match="reopen"):
        await registry.resume(config.workspace.id)


@pytest.mark.asyncio
async def test_control_plane_close_records_audit_event_and_suspended_node(
    tmp_path: Path,
) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    await registry.register(config)
    runtime = await registry.resume(config.workspace.id)
    runtime.active_node = "checker"
    await registry.storage.save_runtime(runtime)

    closed = await registry.close(config.workspace.id)

    events, end_cursor = registry.event_log(config.workspace.id).read_from(0)
    assert len(events) == 1
    assert events[0].actor_id == "organizer"
    assert events[0].state_id == "closed"
    assert closed.status == "closed"
    assert closed.active_node is None
    assert closed.suspended_node == "checker"
    assert closed.cursor == end_cursor


@pytest.mark.asyncio
async def test_reopen_retry_finishes_persisted_audit_event_without_duplicate(
    tmp_path: Path,
) -> None:
    registry = WorkspaceRegistry(tmp_path / "control")
    config = WorkspaceConfig.model_validate(valid_config(tmp_path))
    runtime = await registry.register(config)
    log = registry.event_log(config.workspace.id)
    close_cursor = log.append(
        Event(
            event_id="close-1",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="organizer",
            state_id="closed",
            message="pause",
        )
    )
    end_cursor = log.append(
        Event(
            event_id="reopen-1",
            workspace_id=config.workspace.id,
            config_version=1,
            actor_id="organizer",
            state_id="reopened",
            message="continue original task",
            causation_id="close-1",
        )
    )
    runtime.status = "closed"
    runtime.cursor = close_cursor
    runtime.event_log_identity = log.file_identity()
    runtime.suspended_node = "maker"
    await registry.storage.save_runtime(runtime)

    reopened, event, cursor = await registry.reopen(
        config.workspace.id,
        "continue original task",
    )

    events, _ = log.read_from(0)
    assert [item.event_id for item in events] == ["close-1", "reopen-1"]
    assert event.event_id == "reopen-1"
    assert cursor == end_cursor
    assert reopened.status == "running"
    assert reopened.cursor == close_cursor
    assert reopened.active_node == "maker"


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
    assert 'closed -->|"重新打开"| suspended' in diagram
    assert 'suspended["关闭前活动节点"]' in diagram
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
