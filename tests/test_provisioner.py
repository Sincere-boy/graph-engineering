from pathlib import Path

import pytest

from graph_engineering.config import WorkspaceConfig
from graph_engineering.provisioner import Provisioner

CONFIG = """
schema_version: 1
workspace:
  id: provision-test
  version: 1
  repository: /tmp/worktree
agents:
  developer:
    display_name: Developer
    prompt: Implement the requested change.
    skills: []
states:
  ready:
    display_name: Ready
    allowed_writers: [organizer]
    action:
      type: activate
      target: developer
  done:
    display_name: Done
    allowed_writers: [developer]
    action:
      type: complete
"""


class FakeAdmin:
    def __init__(self, fail_after: int | None = None):
        self.created: list[str] = []
        self.fail_after = fail_after
        self.profile_entries: dict[tuple[str, str], str] = {}
        self.groups = 0
        self.topics = 0

    async def create_bot(self, *, name: str, working_dir: str, cli_id: str) -> str:
        if self.fail_after is not None and len(self.created) >= self.fail_after:
            raise RuntimeError("interrupted")
        app_id = f"app-{len(self.created)}"
        self.created.append(name)
        return app_id

    async def put_role_profile(self, profile_id: str, app_id: str, content: str) -> None:
        self.profile_entries[(profile_id, app_id)] = content

    async def create_group(
        self, *, name: str, app_ids: list[str], working_dir: str, profile_id: str
    ) -> str:
        self.groups += 1
        return "chat-1"

    async def create_topic(
        self, *, app_id: str, chat_id: str, title: str, instruction: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.topics += 1
        return f"root-{app_id}", f"session-{app_id}"


@pytest.mark.asyncio
async def test_provision_is_idempotent_and_creates_one_topic_per_agent(tmp_path: Path) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    admin = FakeAdmin()
    provisioner = Provisioner(tmp_path, admin)

    first = await provisioner.provision(config)
    second = await provisioner.provision(config)

    assert first == second
    assert len(admin.created) == 2  # organizer is injected by the engine
    assert admin.groups == 1
    assert admin.topics == 2
    assert {item.agent_id for item in first.bindings} == {"organizer", "developer"}
    assert "flowchart LR" in admin.profile_entries[(first.role_profile_id, "app-0")]


@pytest.mark.asyncio
async def test_provision_resumes_from_checkpoint_after_interruption(tmp_path: Path) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    first_admin = FakeAdmin(fail_after=1)

    with pytest.raises(RuntimeError, match="interrupted"):
        await Provisioner(tmp_path, first_admin).provision(config)

    second_admin = FakeAdmin()
    result = await Provisioner(tmp_path, second_admin).provision(config)

    assert len(first_admin.created) == 1
    assert len(second_admin.created) == 1
    assert len(result.bindings) == 2
