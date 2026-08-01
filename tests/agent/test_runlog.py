"""Append-only JSONL runlog with secret redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from envmaker.runlog import RunLog


def test_runlog_orders_events_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = RunLog(path)
    log.append("start", {"ok": True})
    log.append("step", {"n": 2})
    events = log.events()
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["kind"] == "start"
    assert events[1]["payload"]["n"] == 2
    assert "ts" in events[0]
    reread = RunLog(path).events()
    assert reread == events


def test_runlog_redacts_nested_secrets(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "trace.jsonl"
    log = RunLog(path)
    log.append(
        "provider",
        {
            "headers": {
                "authorization": "Bearer sk-abcdef1234567890",
                "note": "OPENAI_API_KEY=sk-zzzzzzzzzzzz",
            },
            "token": "sk-ABCDEFGH1234",
        },
    )
    payload = log.events()[0]["payload"]
    assert payload["token"] == "[redacted]"
    assert payload["headers"]["authorization"] == "[redacted]"
    assert payload["headers"]["note"] == "[redacted]"
    assert path.parent.is_dir()


def test_runlog_redacts_sk_proj_keys_and_rejects_nan(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = RunLog(path)
    log.append(
        "secrets",
        {
            "sk-proj-abcdefghijklmnop": "visible",
            "nested": {"api_key": "sk-proj-zzzzzzzzzzzzzzzz"},
        },
    )
    payload = log.events()[0]["payload"]
    assert "[redacted]" in payload
    assert payload["[redacted]"] == "visible"
    assert payload["nested"]["api_key"] == "[redacted]"

    with pytest.raises(ValueError, match="json-serializable"):
        log.append("bad", {"score": float("nan")})

    log.append("after", {"ok": True})
    seqs = [event["seq"] for event in log.events()]
    assert seqs == [1, 2]
    assert log.events()[-1]["kind"] == "after"
