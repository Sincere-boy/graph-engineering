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
from graph_engineering.engine import StateGraphEngine, aggregate_consecutive
from graph_engineering.models import Event, utc_now
from graph_engineering.provisioner import BotmuxAdminClient, Provisioner
from graph_engineering.registry import WorkspaceRegistry
from graph_engineering.storage import storage_from_environment

DEFAULT_CONTROL_DIR = Path.home() / ".graph_engineering"

app = typer.Typer(help="Declarative engineering state graph control plane.")
config_app = typer.Typer(help="Validate and inspect workspace configuration.")
workspace_app = typer.Typer(help="Register and control workspaces.")
event_app = typer.Typer(help="Append authorized state events.")
delivery_app = typer.Typer(help="Inspect and reconcile external delivery results.")
service_app = typer.Typer(help="Run the graph-engineering service.")
app.add_typer(config_app, name="config")
app.add_typer(workspace_app, name="workspace")
app.add_typer(event_app, name="event")
app.add_typer(delivery_app, name="delivery")
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


async def _append_workspace_event(
    registry: WorkspaceRegistry,
    workspace_id: str,
    *,
    actor: str,
    state: str,
    message: str,
    causation_id: str | None = None,
    event_id: str | None = None,
) -> tuple[Event, int]:
    await registry._initialize()
    config = registry.load_config(workspace_id)
    runtime = await registry.storage.get_runtime(workspace_id)
    if runtime is None or runtime.status not in {"running", "completed"}:
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
    event_log = registry.event_log(workspace_id)
    engine = StateGraphEngine(config)

    if runtime.status == "completed":
        restart_state = config.states.get(state)
        if actor != "organizer":
            raise ConfigError("only organizer may restart a completed workspace")
        if (
            restart_state is None
            or restart_state.action.type != "activate"
            or actor not in restart_state.allowed_writers
        ):
            raise ConfigError(
                "completed workspace restart requires an organizer-writable activate state"
            )
        engine.decide([item], expected_active_node=None)
        existing_entries = event_log.read_entries_from(
            0,
            expected_identity=runtime.event_log_identity,
        )
        validated_cursor = existing_entries[-1][1] if existing_entries else 0
        cursor = event_log.append(item, expected_cursor=validated_cursor)
        # The new organizer event is the first record of the resumed run. Any
        # historical post-terminal records remain immutable audit evidence but
        # cannot influence the new run.
        runtime.cursor = validated_cursor
        runtime.status = "running"
        runtime.active_node = None
        runtime.health = "unknown"
        runtime.last_error = None
        runtime.updated_at = utc_now()
        await registry.storage.save_runtime(runtime)
        return item, cursor

    pending_entries = event_log.read_entries_from(
        runtime.cursor,
        expected_identity=runtime.event_log_identity,
    )
    pending_events = [existing for existing, _ in pending_entries]
    validated_cursor = pending_entries[-1][1] if pending_entries else runtime.cursor
    all_events = [
        existing
        for existing, _ in event_log.read_entries_from(
            0,
            expected_identity=runtime.event_log_identity,
        )
    ]
    lookup = {existing.event_id: existing for existing in all_events}
    active_node = runtime.active_node
    last_action: str | None = None
    for batch in aggregate_consecutive(pending_events):
        decision = engine.decide(batch, event_lookup=lookup, expected_active_node=active_node)
        active_node = decision.active_node
        last_action = decision.action
    if last_action in {"pause", "complete"}:
        raise ConfigError(f"event log already reached terminal action {last_action}")
    engine.decide([item], event_lookup=lookup, expected_active_node=active_node)
    cursor = event_log.append(item, expected_cursor=validated_cursor)
    return item, cursor


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
    state: Annotated[
        str | None,
        typer.Option(help="Organizer-writable activate state for restarting a completed run"),
    ] = None,
    message: Annotated[
        str | None,
        typer.Option(help="Auditable task message for restarting a completed run"),
    ] = None,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    async def resume():
        registry = _registry(control_dir)
        await registry._initialize()
        runtime = await registry.storage.get_runtime(workspace_id)
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
        if runtime.status != "completed":
            if state is not None or message is not None:
                raise ConfigError("--state and --message are only valid for a completed workspace")
            return await registry.resume(workspace_id), None, None
        if state is None or message is None:
            raise ConfigError(
                "completed workspace resume requires --state and --message to record "
                "an organizer restart event"
            )
        event, cursor = await _append_workspace_event(
            registry,
            workspace_id,
            actor="organizer",
            state=state,
            message=message,
        )
        return await registry.storage.get_runtime(workspace_id), event, cursor

    try:
        runtime, event, cursor = _run(resume())
    except Exception as exc:
        _fail(exc)
    if event is None:
        typer.echo(runtime.model_dump_json())
    else:
        typer.echo(
            json.dumps(
                {
                    "runtime": runtime.model_dump(mode="json"),
                    "event_id": event.event_id,
                    "cursor": cursor,
                },
                ensure_ascii=False,
            )
        )


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
    cli_id: Annotated[str, typer.Option(help="botmux onboarding CLI selection id")] = "codex",
    name_prefix: Annotated[str, typer.Option(help="New Feishu application name prefix")] = "GE",
    reuse_bots_from: Annotated[
        str | None,
        typer.Option(help="Reuse agent application identities from a provisioned workspace"),
    ] = None,
) -> None:
    async def provision() -> object:
        registry = _registry(control_dir)
        await registry._initialize()
        runtime = await registry.storage.get_runtime(workspace_id)
        if runtime is None:
            raise ConfigError(f"unknown workspace: {workspace_id}")
        config = registry.load_config(workspace_id)
        reuse_apps: dict[str, str] | None = None
        if reuse_bots_from is not None:
            if reuse_bots_from == workspace_id:
                raise ConfigError("reuse source must be a different workspace")
            source = await registry.storage.get_provisioning(reuse_bots_from)
            if source is None:
                raise ConfigError(
                    f"reuse source workspace is not provisioned: {reuse_bots_from}"
                )
            reuse_apps = {
                binding.agent_id: binding.lark_app_id for binding in source.bindings
            }
        base_url, token = _dashboard_connection()
        client = BotmuxAdminClient(base_url, token=token)
        try:
            return await Provisioner(control_dir, client, storage=registry.storage).provision(
                config,
                cli_id=cli_id,
                name_prefix=name_prefix,
                reuse_apps=reuse_apps,
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
    try:
        item, cursor = _run(
            _append_workspace_event(
                _registry(control_dir),
                workspace_id,
                actor=actor,
                state=state,
                message=message,
                causation_id=causation_id,
                event_id=event_id,
            )
        )
    except Exception as exc:
        _fail(exc)
    typer.echo(json.dumps({"event_id": item.event_id, "cursor": cursor}))


@delivery_app.command("list")
def list_deliveries(
    workspace_id: str,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    async def load():
        registry = _registry(control_dir)
        await registry._initialize()
        return await registry.storage.list_deliveries(workspace_id)

    try:
        deliveries = _run(load())
    except Exception as exc:
        _fail(exc)
    typer.echo(
        json.dumps(
            [delivery.model_dump(mode="json") for delivery in deliveries],
            ensure_ascii=False,
            indent=2,
        )
    )


@delivery_app.command("reconcile")
def reconcile_delivery(
    delivery_id: str,
    message_id: Annotated[str, typer.Option(help="Existing Feishu om_ message evidence")],
    evidence_note: Annotated[
        str | None, typer.Option(help="Concise operator verification note")
    ] = None,
    control_dir: ControlDir = DEFAULT_CONTROL_DIR,
) -> None:
    async def reconcile():
        if not message_id.startswith("om_") or len(message_id) < 6:
            raise ConfigError("message evidence must be an existing Feishu om_ message id")
        if evidence_note is not None and len(evidence_note) > 1000:
            raise ConfigError("evidence note must not exceed 1000 characters")
        registry = _registry(control_dir)
        await registry._initialize()
        delivery = await registry.storage.get_delivery(delivery_id)
        if delivery is None:
            raise ConfigError(f"unknown delivery: {delivery_id}")
        if delivery.status not in {"pending", "needs_reconcile"}:
            raise ConfigError(
                f"delivery {delivery_id} is {delivery.status}; only ambiguous results can reconcile"
            )
        delivery.status = "delivered"
        delivery.message_id = message_id
        delivery.reconciliation_source = "operator_evidence"
        delivery.detail = evidence_note or (
            "reconciled from an existing Feishu message; no resend performed"
        )
        delivery.updated_at = utc_now()
        await registry.storage.save_delivery(delivery)
        return delivery

    try:
        delivery = _run(reconcile())
    except Exception as exc:
        _fail(exc)
    typer.echo(delivery.model_dump_json())


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
