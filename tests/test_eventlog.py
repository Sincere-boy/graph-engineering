import json
from pathlib import Path

import pytest

from graph_engineering.eventlog import EventLog, EventLogCorrupt
from graph_engineering.models import Event


def event(event_id: str, state: str = "go", actor: str = "a") -> Event:
    return Event(
        event_id=event_id,
        workspace_id="ws",
        config_version=1,
        actor_id=actor,
        state_id=state,
        message=f"message-{event_id}",
    )


def test_eventlog_appends_and_reads_from_durable_byte_cursor(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "eventlog.jsonl")
    first_cursor = log.append(event("01"))
    end_cursor = log.append(event("02"))

    records, cursor = log.read_from(0)
    tail, tail_cursor = log.read_from(first_cursor)

    assert [record.event_id for record in records] == ["01", "02"]
    assert [record.event_id for record in tail] == ["02"]
    assert cursor == end_cursor == tail_cursor


def test_eventlog_rejects_duplicate_event_ids(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "eventlog.jsonl")
    log.append(event("same"))

    with pytest.raises(ValueError, match="duplicate"):
        log.append(event("same"))


def test_eventlog_fails_closed_on_truncated_or_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "eventlog.jsonl"
    path.write_text(json.dumps(event("01").model_dump(mode="json")) + "\n{", encoding="utf-8")

    with pytest.raises(EventLogCorrupt):
        EventLog(path).read_from(0)


def test_eventlog_fails_when_file_shrinks_behind_cursor(tmp_path: Path) -> None:
    path = tmp_path / "eventlog.jsonl"
    log = EventLog(path)
    cursor = log.append(event("01"))
    path.write_text("", encoding="utf-8")

    with pytest.raises(EventLogCorrupt, match="cursor"):
        log.read_from(cursor)
