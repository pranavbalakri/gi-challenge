"""Append-only JSONL run traces with secret redaction."""

from __future__ import annotations

import json as _json
import re as _re
import threading as _threading
import time as _time
from pathlib import Path as _Path

__all__ = ["RunLog"]

_REDACTED = "[redacted]"
_SECRET_PATTERNS = (
    _re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    _re.compile(r"OPENAI_API_KEY=\S+"),
    _re.compile(r"(?i)bearer\s+\S+"),
)


def _redact_string(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            return _REDACTED
    return value


def _redact(value: object) -> object:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {
            _redact_string(str(key)): _redact(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


class RunLog:
    """Thread-safe append-only JSONL event log."""

    def __init__(self, path: _Path | str) -> None:
        self._path = _Path(path)
        self._lock = _threading.Lock()
        self._seq = 0
        if self._path.exists():
            events = self.events()
            if events:
                self._seq = max(int(event.get("seq", 0)) for event in events)

    def append(self, kind: str, payload: dict) -> None:
        """Append one redacted event with monotonic seq and wall timestamp."""

        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")

        with self._lock:
            event = {
                "seq": self._seq + 1,
                "ts": _time.time(),
                "kind": kind,
                "payload": _redact(payload),
            }
            try:
                line = _json.dumps(
                    event,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (ValueError, TypeError) as exc:
                raise ValueError("payload is not json-serializable") from exc

            self._seq += 1
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def events(self) -> list[dict]:
        """Return all events currently on disk in append order."""

        if not self._path.exists():
            return []
        events: list[dict] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                events.append(_json.loads(text))
        return events
