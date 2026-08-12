import asyncio
from pathlib import Path

import httpx
import pytest

from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.provisioner import BotmuxAdminClient, Provisioner
from graph_engineering.storage import SQLiteStorage

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
        self.configured: list[str] = []
        self.groups = 0
        self.topics = 0
        self.memberships = 0
        self.applied: list[str] = []
        self.left: list[str] = []

    async def create_bot(self, *, name: str, working_dir: str, cli_id: str) -> str:
        if self.fail_after is not None and len(self.created) >= self.fail_after:
            raise RuntimeError("interrupted")
        app_id = f"app-{len(self.created)}"
        self.created.append(name)
        return app_id

    async def put_role_profile(self, profile_id: str, app_id: str, content: str) -> None:
        self.profile_entries[(profile_id, app_id)] = content

    async def configure_bot(self, *, app_id: str, cli_id: str) -> None:
        self.configured.append(f"{app_id}:{cli_id}")

    async def create_group(
        self, *, name: str, app_ids: list[str], working_dir: str, profile_id: str
    ) -> str:
        self.groups += 1
        return "chat-1"

    async def ensure_group_bots(self, *, chat_id: str, app_ids: list[str]) -> None:
        self.memberships += 1

    async def leave_group_bots(self, *, chat_id: str, app_ids: list[str]) -> None:
        self.left.extend(app_ids)

    async def apply_role_profile(
        self, *, profile_id: str, app_id: str, chat_id: str
    ) -> None:
        self.applied.append(app_id)

    async def create_topic(
        self, *, app_id: str, chat_id: str, title: str, instruction: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.topics += 1
        return f"om_{app_id}", f"session-{app_id}"


@pytest.mark.asyncio
async def test_provision_is_idempotent_and_creates_one_topic_per_agent(tmp_path: Path) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    admin = FakeAdmin()
    provisioner = Provisioner(tmp_path, admin)

    first = await provisioner.provision(config)
    second = await provisioner.provision(config)

    assert first == second
    assert len(admin.created) == 2  # organizer is injected by the engine
    # Safe settings are reconciled on every run, while resource creation is exactly once.
    assert admin.configured == [
        "app-0:codex",
        "app-1:codex",
        "app-0:codex",
        "app-1:codex",
    ]
    assert admin.groups == 1
    assert admin.memberships == 2
    assert admin.applied == ["app-0", "app-1", "app-0", "app-1"]
    assert admin.topics == 2
    assert {item.agent_id for item in first.bindings} == {"organizer", "developer"}
    assert "flowchart LR" in admin.profile_entries[(first.role_profile_id, "app-0")]


@pytest.mark.asyncio
async def test_provision_reuses_apps_but_creates_a_new_group_and_topics(tmp_path: Path) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    admin = FakeAdmin()

    result = await Provisioner(tmp_path, admin).provision(
        config,
        reuse_apps={"organizer": "existing-organizer", "developer": "existing-developer"},
    )

    assert admin.created == []
    assert admin.groups == 1
    assert admin.topics == 2
    assert {item.agent_id: item.lark_app_id for item in result.bindings} == {
        "organizer": "existing-organizer",
        "developer": "existing-developer",
    }
    assert {item.root_message_id for item in result.bindings} == {
        "om_existing-organizer",
        "om_existing-developer",
    }


@pytest.mark.asyncio
async def test_provision_rejects_incomplete_reused_app_set_before_side_effects(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    admin = FakeAdmin()

    with pytest.raises(ConfigError, match="missing agents: developer"):
        await Provisioner(tmp_path, admin).provision(
            config,
            reuse_apps={"organizer": "existing-organizer"},
        )

    assert admin.created == []
    assert admin.configured == []
    assert admin.groups == 0
    assert admin.topics == 0


@pytest.mark.asyncio
async def test_concurrent_provisioning_serializes_external_resource_creation(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)

    class SlowAdmin(FakeAdmin):
        async def create_bot(self, *, name: str, working_dir: str, cli_id: str) -> str:
            await asyncio.sleep(0.01)
            return await super().create_bot(name=name, working_dir=working_dir, cli_id=cli_id)

        async def create_group(
            self, *, name: str, app_ids: list[str], working_dir: str, profile_id: str
        ) -> str:
            await asyncio.sleep(0.01)
            return await super().create_group(
                name=name,
                app_ids=app_ids,
                working_dir=working_dir,
                profile_id=profile_id,
            )

        async def create_topic(
            self,
            *,
            app_id: str,
            chat_id: str,
            title: str,
            instruction: str,
            idempotency_key: str,
        ) -> tuple[str, str]:
            await asyncio.sleep(0.01)
            return await super().create_topic(
                app_id=app_id,
                chat_id=chat_id,
                title=title,
                instruction=instruction,
                idempotency_key=idempotency_key,
            )

    admin = SlowAdmin()
    first, second = await asyncio.gather(
        Provisioner(tmp_path, admin).provision(config),
        Provisioner(tmp_path, admin).provision(config),
    )

    assert first == second
    assert len(admin.created) == 2
    assert admin.groups == 1
    assert admin.topics == 2


@pytest.mark.asyncio
async def test_reprovision_refreshes_fixed_topic_protocol_without_new_topics(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    admin = FakeAdmin()
    provisioner = Provisioner(tmp_path, admin)

    await provisioner.provision(config)
    admin.profile_entries.clear()
    await provisioner.provision(config)

    assert admin.topics == 2
    assert len(admin.profile_entries) == 2
    assert all("禁止创建新话题" in prompt for prompt in admin.profile_entries.values())


@pytest.mark.asyncio
async def test_provision_keeps_declared_botmux_cli_selection(tmp_path: Path) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    admin = FakeAdmin()

    await Provisioner(tmp_path, admin).provision(config, cli_id="gemini")

    assert admin.configured == ["app-0:gemini", "app-1:gemini"]


@pytest.mark.asyncio
async def test_new_config_version_reuses_topics_and_reconciles_removed_agents(
    tmp_path: Path,
) -> None:
    first = WorkspaceConfig.from_yaml_text(CONFIG)
    storage = SQLiteStorage(tmp_path / "state.db")
    admin = FakeAdmin()
    provisioner = Provisioner(tmp_path, admin, storage=storage)
    original = await provisioner.provision(first)
    second = WorkspaceConfig.from_yaml_text(
        """
schema_version: 1
workspace:
  id: provision-test
  version: 2
  repository: /tmp/worktree
agents: {}
states:
  done:
    display_name: Done
    allowed_writers: [organizer]
    action:
      type: complete
"""
    )

    updated = await provisioner.provision(second)

    assert admin.topics == 2
    assert [binding.agent_id for binding in updated.bindings] == ["organizer"]
    assert updated.bindings[0].root_message_id == original.bindings[0].root_message_id
    assert admin.left == ["app-1"]


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


@pytest.mark.asyncio
async def test_reprovision_replaces_chat_scope_binding_with_fixed_native_topic(
    tmp_path: Path,
) -> None:
    config = WorkspaceConfig.from_yaml_text(CONFIG)
    storage = SQLiteStorage(tmp_path / "state.db")
    admin = FakeAdmin()
    provisioner = Provisioner(tmp_path, admin, storage=storage)
    original = await provisioner.provision(config)
    checkpoint_path = tmp_path / "workspaces" / "provision-test" / "provision-checkpoint.json"
    checkpoint = __import__("json").loads(checkpoint_path.read_text())
    checkpoint["topics"]["developer"] = {
        "root_message_id": original.chat_id,
        "session_id": "chat-scope-session",
    }
    checkpoint["provisioning"]["bindings"][1]["root_message_id"] = original.chat_id
    checkpoint["provisioning"]["bindings"][1]["session_id"] = "chat-scope-session"
    checkpoint_path.write_text(__import__("json").dumps(checkpoint))
    invalid = original.model_copy(deep=True)
    invalid.bindings[1].root_message_id = original.chat_id
    invalid.bindings[1].session_id = "chat-scope-session"
    await storage.save_provisioning(invalid)

    repaired = await provisioner.provision(config)

    developer = next(item for item in repaired.bindings if item.agent_id == "developer")
    assert developer.root_message_id.startswith("om_")
    assert developer.root_message_id != repaired.chat_id
    assert developer.session_id != "chat-scope-session"
    assert admin.topics == 3


@pytest.mark.asyncio
async def test_onboarding_uses_confirmed_session_without_managed_activation_gate() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/cli-options":
            return httpx.Response(
                200,
                json={
                    "webSession": {
                        "status": "ready",
                        "identity": {"userId": "user", "tenantId": "tenant"},
                    }
                },
            )
        if request.url.path == "/api/bot-onboarding/start":
            return httpx.Response(202, json={"job": {"id": "job-1"}})
        if request.url.path == "/api/bot-onboarding/job-1":
            return httpx.Response(200, json={"job": {"status": "completed", "appId": "app-new"}})
        if request.url.path == "/api/bots":
            return httpx.Response(200, json={"bots": [{"larkAppId": "app-new", "online": True}]})
        raise AssertionError(request.url.path)

    client = BotmuxAdminClient(
        "http://botmux.test", token="token", transport=httpx.MockTransport(handler)
    )
    app_id = await client.create_bot(name="new", working_dir="/tmp", cli_id="codex")
    await client.close()

    start = next(request for request in seen if request.url.path.endswith("/start"))
    body = __import__("json").loads(start.content)
    assert app_id == "app-new"
    assert body["sessionMode"] == "reuse"
    assert "requireCriticalScopesBeforeActivation" not in body


@pytest.mark.asyncio
async def test_group_topic_uses_checkpoint_idempotency_not_invalid_virtual_key() -> None:
    seen: list[httpx.Request] = []
    reply_mode = "chat-topic"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reply_mode
        seen.append(request)
        if request.url.path.endswith("/card-prefs"):
            reply_mode = __import__("json").loads(request.content)["regularGroupReplyMode"]
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(200, json={"target": {"sessionId": "session-new"}})
        if request.url.path.endswith("/trigger-result"):
            return httpx.Response(200, json={"state": "completed", "output": {"content": "ok"}})
        if request.url.path == "/api/sessions":
            return httpx.Response(
                200,
                json={
                    "sessions": [
                        {
                            "sessionId": "session-new",
                            "rootMessageId": (
                                "om_topic-root" if reply_mode == "new-topic" else "chat"
                            ),
                        }
                    ]
                },
            )
        raise AssertionError(request.url.path)

    client = BotmuxAdminClient(
        "http://botmux.test", token="token", transport=httpx.MockTransport(handler)
    )
    root, session = await client.create_topic(
        app_id="app",
        chat_id="chat",
        title="topic",
        instruction="initialize",
        idempotency_key="checkpoint-key",
    )
    await client.close()

    trigger = next(
        request
        for request in seen
        if request.method == "POST" and request.url.path == "/api/trigger"
    )
    body = __import__("json").loads(trigger.content)
    assert (root, session) == ("om_topic-root", "session-new")
    assert body["target"]["chatId"] == "chat"
    assert "idempotencyKey" not in body["options"]
    modes = [
        __import__("json").loads(request.content)["regularGroupReplyMode"]
        for request in seen
        if request.url.path.endswith("/card-prefs")
    ]
    assert modes == ["new-topic", "chat-topic"]


@pytest.mark.asyncio
async def test_bot_configuration_disables_per_mention_topic_forks() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = BotmuxAdminClient(
        "http://botmux.test", token="token", transport=httpx.MockTransport(handler)
    )
    await client.configure_bot(app_id="app", cli_id="codex")
    await client.close()

    prefs = next(request for request in seen if request.url.path.endswith("/card-prefs"))
    body = __import__("json").loads(prefs.content)
    assert body["regularGroupReplyMode"] == "chat-topic"
