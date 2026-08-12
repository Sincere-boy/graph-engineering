from pathlib import Path

import yaml
from test_config import valid_config
from typer.testing import CliRunner

from graph_engineering.cli import app
from graph_engineering.eventlog import EventLog

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
