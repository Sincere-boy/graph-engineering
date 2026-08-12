from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from pydantic import ValidationError

from graph_engineering.models import Event


class EventLogCorrupt(RuntimeError):
    pass


class EventLogConflict(RuntimeError):
    pass


class EventLog:
    def __init__(self, path: Path):
        self.path = path

    def append(self, event: Event, *, expected_cursor: int | None = None) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            existing, current_cursor = self._read_locked(stream, 0)
            if expected_cursor is not None and current_cursor != expected_cursor:
                raise EventLogConflict(
                    f"event log changed after validation: expected cursor "
                    f"{expected_cursor}, found {current_cursor}"
                )
            if any(item.event_id == event.event_id for item in existing):
                raise ValueError(f"duplicate event_id: {event.event_id}")
            stream.seek(0, os.SEEK_END)
            payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False).encode() + b"\n"
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            return stream.tell()

    def read_from(self, cursor: int) -> tuple[list[Event], int]:
        entries = self.read_entries_from(cursor)
        return [event for event, _ in entries], entries[-1][1] if entries else cursor

    def file_identity(self) -> str | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return f"{stat.st_dev}:{stat.st_ino}"

    def read_entries_from(
        self, cursor: int, *, expected_identity: str | None = None
    ) -> list[tuple[Event, int]]:
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if not self.path.exists():
            if cursor:
                raise EventLogCorrupt("event log disappeared behind cursor")
            return []
        with self.path.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            stat = os.fstat(stream.fileno())
            actual_identity = f"{stat.st_dev}:{stat.st_ino}"
            if expected_identity is not None and actual_identity != expected_identity:
                raise EventLogCorrupt("event log identity changed while opening the file")
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if cursor > size:
                raise EventLogCorrupt(f"cursor {cursor} exceeds event log size {size}")
            stream.seek(cursor)
            entries: list[tuple[Event, int]] = []
            line_number = 0
            while line := stream.readline():
                line_number += 1
                if not line.endswith(b"\n"):
                    raise EventLogCorrupt("event log has a truncated final record")
                try:
                    entries.append((Event.model_validate_json(line), stream.tell()))
                except (ValidationError, ValueError) as exc:
                    raise EventLogCorrupt(
                        f"invalid event record {line_number} after cursor {cursor}"
                    ) from exc
            return entries

    def _read_locked(self, stream: object, cursor: int) -> tuple[list[Event], int]:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if cursor > size:
            raise EventLogCorrupt(f"cursor {cursor} exceeds event log size {size}")
        stream.seek(cursor)
        payload = stream.read()
        if payload and not payload.endswith(b"\n"):
            raise EventLogCorrupt("event log has a truncated final record")
        records: list[Event] = []
        for offset, line in enumerate(payload.splitlines(), start=1):
            try:
                records.append(Event.model_validate_json(line))
            except (ValidationError, ValueError) as exc:
                raise EventLogCorrupt(
                    f"invalid event record {offset} after cursor {cursor}"
                ) from exc
        return records, size
