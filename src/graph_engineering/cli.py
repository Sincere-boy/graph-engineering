from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Annotated

import typer

from graph_engineering.config import ConfigError, WorkspaceConfig
from graph_engineering.config_builder import config_from_markdown
from graph_engineering.engine import StateGraphEngine
from graph_engineering.models import Event
from graph_engineering.provisioner import BotmuxAdminClient, Provisioner
from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.storage import storage_from_environment

DEFAULT_CONTROL_DIR = Path.home() / ".graph_engineering"

app = typer.Typer(help="Declarative engineering state graph control plane.")
config_app = typer.Typer(help="Validate and inspect workspace configuration.")
workspace_app = typer.Typer(help="Register and control workspaces.")
event_app = typer.Typer(help="Append authorized state events.")
service_app = typer.Typer(help="Run the graph-engineering service.")
app.add_typer(config_app, name="config")
app.add_typer(workspace_app, name="workspace")
app.add_typer(event_app, name="event")
app.add_typer(service_app, name="service")


ControlDir = Annotated[Path, typer.Option(help="Graph engineering control directory")]


def _run(coroutine):
    return asyncio.run(coroutine)


def _registry(control_dir: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(control_dir, storage_from_environment(control_dir))


def _dashboard_connection() -> tuple[str, str]:
    base_url = os.getenv("GE_BOTMUX_URL")
    botmux_dir = Path.home() / ".botmux"
    if not base_url:
        port_path = botmux_dir / ".dashboard-port"
        if not port_path.exists():
            raise ConfigError("botmux dashboard port is unavailable")
        base_url = f"http://127.0.0.1:{port_path.read_text(encoding='utf-8').strip()}"
    token = os.getenv("GE_BOTMUX_TOKEN")
    if token is None:
        token_path = botmux_dir / ".dashboard-token"
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ConfigError("botmux dashboard token is unavailable")
    return base_url, token


def _fail(exc: Exception) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=2)


@config_app.command("validate")
def validate_config(path: Path) -> None:
    try:
        config = WorkspaceConfig.from_yaml(path)
    except Exception as exc:
        _fail(exc)
    typer.echo(
        json.dumps(
            {
                "workspace_id": config.workspace.id,
                "version": config.workspace.version,
                "content_hash": config.content_hash,
                "agents": sorted(config.agents),
                "states": sorted(config.states),
            },
            ensure_ascii=False,
        )
    )


@config_app.command("from-markdown")
def from_markdown(
    source: Path,
    output: Annotated[Path, typer.Option()],
    workspace_id: Annotated[str, typer.Option()],
    repository: Annotated[Path, typer.Option()],
    version: Annotated[int, typer.Option()] = 1,
    name: Annotated[str | None, typer.Option()] = None,
) -> None:
    try:
        config = config_from_markdown(
            source.read_text(encoding="utf-8"),
            workspace_id=workspace_id,
            repository=repository,
            version=version,
            name=name,
        )
        WorkspaceRegistry._atomic_write(output, config.to_yaml())
    except Exception as exc:
        _fail(exc)
    typer.echo(str(output.resolve()))


@workspace_app.command("register")
def register_workspace(
    path: Path,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    try:
        config = WorkspaceConfig.from_yaml(path)
        runtime = _run(_registry(control_dir).register(config))
    except Exception as exc:
        _fail(exc)
    typer.echo(runtime.model_dump_json())


@workspace_app.command("pause")
def pause_workspace(
    workspace_id: str,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    try:
        runtime = _run(_registry(control_dir).pause(workspace_id))
    except Exception as exc:
        _fail(exc)
    typer.echo(runtime.model_dump_json())


@workspace_app.command("resume")
def resume_workspace(
    workspace_id: str,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    try:
        runtime = _run(_registry(control_dir).resume(workspace_id))
    except Exception as exc:
        _fail(exc)
    typer.echo(runtime.model_dump_json())


@workspace_app.command("status")
def workspace_status(
    workspace_id: str,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    async def load():
        registry = _registry(control_dir)
        await registry._initialize()
        return await registry.storage.get_runtime(workspace_id)

    try:
        runtime = _run(load())
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
    except Exception as exc:
        _fail(exc)
    typer.echo(json.dumps(runtime.model_dump(mode="json"), ensure_ascii=False, indent=2))


@workspace_app.command("diagram")
def workspace_diagram(
    workspace_id: str,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    try:
        registry = _registry(control_dir)
        typer.echo(registry.render_mermaid(registry.load_config(workspace_id)), nl=False)
    except Exception as exc:
        _fail(exc)


@workspace_app.command("provision")
def provision_workspace(
    workspace_id: str,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
    cli_id: Annotated[str, typer.Option(help="botmux CLI selection id")] = "aiden-x-codex",
    name_prefix: Annotated[str, typer.Option(help="New Feishu application name prefix")] = "GE",
) -> None:
    async def provision() -> object:
        registry = _registry(control_dir)
        await registry._initialize()
        runtime = await registry.storage.get_runtime(workspace_id)
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
        config = registry.load_config(workspace_id)
        base_url, token = _dashboard_connection()
        client = BotmuxAdminClient(base_url, token=token)
        try:
            return await Provisioner(control_dir, client, storage=registry.storage).provision(
                config, cli_id=cli_id, name_prefix=name_prefix
            )
        finally:
            await client.close()

    try:
        result = _run(provision())
    except Exception as exc:
        _fail(exc)
    typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


@event_app.command("append")
def append_event(
    workspace_id: str,
    actor: Annotated[str, typer.Option()],
    state: Annotated[str, typer.Option()],
    message: Annotated[str, typer.Option()],
    causation_id: Annotated[str | None, typer.Option()] = None,
    event_id: Annotated[str | None, typer.Option()] = None,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    async def append() -> tuple[Event, int]:
        registry = _registry(control_dir)
        await registry._initialize()
        config = registry.load_config(workspace_id)
        runtime = await registry.storage.get_runtime(workspace_id)
        if runtime is None or runtime.status != "running":
            raise ConfigError("workspace must be running before appending events")
        item = Event(
            event_id=event_id or str(uuid.uuid4()),
            workspace_id=workspace_id,
            config_version=config.workspace.version,
            actor_id=actor,
            state_id=state,
            message=message,
            causation_id=causation_id,
        )
        all_events, _ = registry.event_log(workspace_id).read_from(0)
        StateGraphEngine(config).decide(
            [item], event_lookup={existing.event_id: existing for existing in all_events}
        )
        cursor = registry.event_log(workspace_id).append(item)
        return item, cursor

    try:
        item, cursor = _run(append())
    except Exception as exc:
        _fail(exc)
    typer.echo(json.dumps({"event_id": item.event_id, "cursor": cursor}))


@service_app.command("run")
def run_service(
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    from graph_engineering.runtime import run_from_environment

    try:
        _run(run_from_environment(control_dir))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()
