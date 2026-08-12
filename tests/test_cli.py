import asyncio
from pathlib import Path

import yaml
from test_config import valid_config
from typer.testing import CliRunner

from graph_engineering.cli import app
from graph_engineering.eventlog import EventLog
from graph_engineering.models import Delivery
from graph_engineering.storage import SQLiteStorage

runner = CliRunner()
SQLITE_ENV = {"GE_STORAGE_BACKEND": "sqlite"}


def test_cli_register_resume_append_and_status(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.yaml"
    config_path.write_text(
        yaml.safe_dump(valid_config(tmp_path), allow_unicode=True), encoding="utf-8"
    )
    control = tmp_path / "control"

    assert runner.invoke(app, ["config", "validate", str(config_path)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["workspace", "register", str(config_path), "--control-dir", str(control)],
            env=SQLITE_ENV,
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["workspace", "resume", "arbitrary-flow", "--control-dir", str(control)],
            env=SQLITE_ENV,
        ).exit_code
        == 0
    )
    appended = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "maker",
            "--state",
            "inspect",
            "--message",
            "ready",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )

    assert appended.exit_code == 0
    assert "event_id" in appended.stdout
    assert len(EventLog(control / "workspaces/arbitrary-flow/eventlog.jsonl").read_from(0)[0]) == 1
    status = runner.invoke(
        app,
        ["workspace", "status", "arbitrary-flow", "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    assert status.exit_code == 0
    assert '"status": "running"' in status.stdout


def test_cli_rejects_unauthorized_event_before_append(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.yaml"
    config_path.write_text(
        yaml.safe_dump(valid_config(tmp_path), allow_unicode=True), encoding="utf-8"
    )
    control = tmp_path / "control"
    runner.invoke(
        app,
        ["workspace", "register", str(config_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        ["workspace", "resume", "arbitrary-flow", "--control-dir", str(control)],
        env=SQLITE_ENV,
    )

    result = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "checker",
            "--state",
            "begin",
            "--message",
            "invalid",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )

    assert result.exit_code != 0
    assert not (control / "workspaces/arbitrary-flow/eventlog.jsonl").exists()


def test_cli_exposes_workspace_provision_command() -> None:
    result = runner.invoke(app, ["workspace", "--help"])

    assert result.exit_code == 0
    assert "provision" in result.stdout


def test_cli_exposes_workspace_close_command() -> None:
    result = runner.invoke(app, ["workspace", "--help"])

    assert result.exit_code == 0
    assert "close" in result.stdout


def test_cli_provision_exposes_reuse_bots_source() -> None:
    result = runner.invoke(app, ["workspace", "provision", "--help"])

    assert result.exit_code == 0
    assert "--reuse-bots-from" in result.stdout


def test_cli_rejects_event_after_queued_terminal_event(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.yaml"
    config_path.write_text(
        yaml.safe_dump(valid_config(tmp_path), allow_unicode=True), encoding="utf-8"
    )
    control = tmp_path / "control"
    runner.invoke(
        app,
        ["workspace", "register", str(config_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        ["workspace", "resume", "arbitrary-flow", "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    terminal = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "checker",
            "--state",
            "done",
            "--message",
            "done",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )
    trailing = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "maker",
            "--state",
            "inspect",
            "--message",
            "late",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )

    assert terminal.exit_code == 0
    assert trailing.exit_code != 0
    assert "terminal" in trailing.stderr


def test_cli_organizer_activate_event_resumes_completed_workspace(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.yaml"
    config_path.write_text(
        yaml.safe_dump(valid_config(tmp_path), allow_unicode=True), encoding="utf-8"
    )
    control = tmp_path / "control"
    runner.invoke(
        app,
        ["workspace", "register", str(config_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    storage = SQLiteStorage(control / "state.db")

    async def complete() -> None:
        await storage.initialize()
        runtime = await storage.get_runtime("arbitrary-flow")
        runtime.status = "completed"
        runtime.active_node = None
        await storage.save_runtime(runtime)

    asyncio.run(complete())

    result = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "organizer",
            "--state",
            "begin",
            "--message",
            "start another task",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )
    runtime = asyncio.run(storage.get_runtime("arbitrary-flow"))

    assert result.exit_code == 0
    assert runtime.status == "running"
    assert runtime.active_node is None
    assert len(EventLog(control / "workspaces/arbitrary-flow/eventlog.jsonl").read_from(0)[0]) == 1


def test_cli_workspace_resume_records_configured_organizer_event(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.yaml"
    config_path.write_text(
        yaml.safe_dump(valid_config(tmp_path), allow_unicode=True), encoding="utf-8"
    )
    control = tmp_path / "control"
    runner.invoke(
        app,
        ["workspace", "register", str(config_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    storage = SQLiteStorage(control / "state.db")

    async def complete() -> None:
        await storage.initialize()
        runtime = await storage.get_runtime("arbitrary-flow")
        runtime.status = "completed"
        await storage.save_runtime(runtime)

    asyncio.run(complete())
    result = runner.invoke(
        app,
        [
            "workspace",
            "resume",
            "arbitrary-flow",
            "--state",
            "begin",
            "--message",
            "start another task",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )
    events, _ = EventLog(control / "workspaces/arbitrary-flow/eventlog.jsonl").read_from(0)

    assert result.exit_code == 0
    assert events[0].actor_id == "organizer"
    assert events[0].state_id == "begin"
    assert asyncio.run(storage.get_runtime("arbitrary-flow")).status == "running"


def test_cli_completed_workspace_rejects_non_organizer_restart_event(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.yaml"
    config_path.write_text(
        yaml.safe_dump(valid_config(tmp_path), allow_unicode=True), encoding="utf-8"
    )
    control = tmp_path / "control"
    runner.invoke(
        app,
        ["workspace", "register", str(config_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    storage = SQLiteStorage(control / "state.db")

    async def complete() -> None:
        await storage.initialize()
        runtime = await storage.get_runtime("arbitrary-flow")
        runtime.status = "completed"
        await storage.save_runtime(runtime)

    asyncio.run(complete())
    result = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "maker",
            "--state",
            "inspect",
            "--message",
            "not allowed to restart",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )

    assert result.exit_code != 0
    assert "organizer" in result.stderr
    assert not (control / "workspaces/arbitrary-flow/eventlog.jsonl").exists()


def test_cli_rejects_event_from_inactive_writer(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.yaml"
    config_path.write_text(
        yaml.safe_dump(valid_config(tmp_path), allow_unicode=True), encoding="utf-8"
    )
    control = tmp_path / "control"
    runner.invoke(
        app,
        ["workspace", "register", str(config_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        ["workspace", "resume", "arbitrary-flow", "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "organizer",
            "--state",
            "begin",
            "--message",
            "begin",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )

    inactive = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "checker",
            "--state",
            "inspect",
            "--message",
            "out of turn",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )

    assert inactive.exit_code != 0
    assert "active agent" in inactive.stderr


def test_cli_reconciles_uncertain_delivery_only_with_feishu_message_evidence(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    storage = SQLiteStorage(control / "state.db")

    async def seed() -> None:
        await storage.initialize()
        await storage.save_delivery(
            Delivery(
                delivery_id="delivery-uncertain",
                workspace_id="workspace",
                event_ids=["event-1"],
                target_agent="worker",
                status="needs_reconcile",
            )
        )

    asyncio.run(seed())
    invalid = runner.invoke(
        app,
        [
            "delivery",
            "reconcile",
            "delivery-uncertain",
            "--message-id",
            "not-a-message",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )
    reconciled = runner.invoke(
        app,
        [
            "delivery",
            "reconcile",
            "delivery-uncertain",
            "--message-id",
            "om_visible_delivery",
            "--evidence-note",
            "message contains the expected event id",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )
    saved = asyncio.run(storage.get_delivery("delivery-uncertain"))

    assert invalid.exit_code != 0
    assert reconciled.exit_code == 0
    assert saved.status == "delivered"
    assert saved.message_id == "om_visible_delivery"
    assert saved.reconciliation_source == "operator_evidence"
    assert saved.detail == "message contains the expected event id"


def test_cli_validates_new_version_from_version_start_cursor(tmp_path: Path) -> None:
    control = tmp_path / "control"
    first_raw = valid_config(tmp_path)
    first_path = tmp_path / "v1.yaml"
    first_path.write_text(yaml.safe_dump(first_raw, allow_unicode=True), encoding="utf-8")
    runner.invoke(
        app,
        ["workspace", "register", str(first_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        ["workspace", "resume", "arbitrary-flow", "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "organizer",
            "--state",
            "begin",
            "--message",
            "version one",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        ["workspace", "pause", "arbitrary-flow", "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    second_raw = valid_config(tmp_path)
    second_raw["workspace"]["version"] = 2
    second_raw["states"]["commence"] = second_raw["states"].pop("begin")
    second_path = tmp_path / "v2.yaml"
    second_path.write_text(yaml.safe_dump(second_raw, allow_unicode=True), encoding="utf-8")
    runner.invoke(
        app,
        ["workspace", "register", str(second_path), "--control-dir", str(control)],
        env=SQLITE_ENV,
    )
    runner.invoke(
        app,
        ["workspace", "resume", "arbitrary-flow", "--control-dir", str(control)],
        env=SQLITE_ENV,
    )

    result = runner.invoke(
        app,
        [
            "event",
            "append",
            "arbitrary-flow",
            "--actor",
            "organizer",
            "--state",
            "commence",
            "--message",
            "version two",
            "--control-dir",
            str(control),
        ],
        env=SQLITE_ENV,
    )
    events, _ = EventLog(control / "workspaces/arbitrary-flow/eventlog.jsonl").read_from(0)

    assert result.exit_code == 0
    assert [event.config_version for event in events] == [1, 2]
