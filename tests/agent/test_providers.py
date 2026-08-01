"""Unit coverage for authoring providers (keyless; no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from envmaker.agent.providers import (
    OpenAIProvider,
    ProviderError,
    ProviderTurn,
    ScriptedProvider,
    _parse_assistant_message,
    _strip_python_fence,
)
from envmaker.core.program import ProviderInfo


def test_provider_turn_requires_exactly_one_channel() -> None:
    ProviderTurn(code="x = 1\n", tool=None, args={}, text=None)
    ProviderTurn(code=None, tool="read_program", args={}, text=None)
    ProviderTurn(code=None, tool=None, args={}, text="done")
    with pytest.raises(ValidationError):
        ProviderTurn(code="x", tool="read_program", args={}, text=None)
    with pytest.raises(ValidationError):
        ProviderTurn(code=None, tool=None, args={}, text=None)


def test_scripted_provider_replays_and_exhausts(tmp_path: Path) -> None:
    descriptor = ProviderInfo(
        provider="scripted",
        model_name="fixture",
        prompt_version="1",
    )
    turns = (
        ProviderTurn(code="print(1)\n"),
        ProviderTurn(tool="compile_environment", args={}),
        ProviderTurn(text="done"),
    )
    provider = ScriptedProvider(turns, descriptor=descriptor)
    assert provider.descriptor == descriptor
    assert provider.next_turn([]).code == "print(1)\n"
    assert provider.next_turn([{"role": "user"}]).tool == "compile_environment"
    assert provider.next_turn([]).text == "done"
    with pytest.raises(ProviderError, match="exhausted"):
        provider.next_turn([])


def test_scripted_provider_from_fixture(tmp_path: Path) -> None:
    (tmp_path / "rev1.py").write_text("SOURCE = 1\n", encoding="utf-8")
    (tmp_path / "patch1.txt").write_text(
        "<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n",
        encoding="utf-8",
    )
    transcript = {
        "turns": [
            {"code_file": "rev1.py"},
            {"tool": "patch_program", "args": {}, "patch_file": "patch1.txt"},
            {"text": "finished"},
        ]
    }
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    provider = ScriptedProvider.from_fixture(path)
    first = provider.next_turn([])
    assert first.code == "SOURCE = 1\n"
    second = provider.next_turn([])
    assert second.tool == "patch_program"
    assert "SEARCH" in str(second.args["patch"])
    assert provider.next_turn([]).text == "finished"


def test_strip_python_fence() -> None:
    fenced = "here\n```python\nx = 1\n```\n"
    assert _strip_python_fence(fenced) == "x = 1\n"
    assert _strip_python_fence("no fence") is None


def test_parse_assistant_message_tool_and_code_and_text() -> None:
    tool_msg = {
        "tool_calls": [
            {
                "function": {
                    "name": "compile_environment",
                    "arguments": "{}",
                }
            }
        ],
        "content": None,
    }
    turn = _parse_assistant_message(tool_msg)
    assert turn.tool == "compile_environment"
    assert turn.args == {}

    code_msg = {"content": "ok\n```python\na = 1\n```\n", "tool_calls": None}
    assert _parse_assistant_message(code_msg).code == "a = 1\n"

    text_msg = {"content": "looking good", "tool_calls": None}
    assert _parse_assistant_message(text_msg).text == "looking good"


def test_openai_provider_reads_key_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-testkey12345678\n", encoding="utf-8")
    provider = OpenAIProvider(api_key=None, env_file=env_path)
    assert provider._api_key == "sk-testkey12345678"
    assert provider.descriptor.provider == "openai"


def test_openai_request_path_offline(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from envmaker.agent.providers import OpenAIProvider

    captured: dict[str, object] = {}

    class _FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = SimpleNamespace(
                content="```python\nx = 1\n```", tool_calls=None
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class _FakeClient:
        def __init__(self, api_key):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setitem(
        sys.modules, "openai", SimpleNamespace(OpenAI=_FakeClient)
    )

    provider = OpenAIProvider(api_key="sk-test-abcdef123456")
    sent_messages = [
        {"role": "system", "content": "SYSPROMPT"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "TOOL_CALL compile_environment {}"},
        {"role": "user", "content": "TOOL_RESULT compile_environment {}"},
    ]
    turn = provider.next_turn(sent_messages)
    assert turn.code is not None and "x = 1" in turn.code

    sent = captured["messages"]
    assert all(m["role"] in {"system", "user", "assistant"} for m in sent)
    assert [m["role"] for m in sent].count("system") == 1
    assert sent[0]["content"] == "SYSPROMPT"
    tool_names = {t["function"]["name"] for t in captured["tools"]}
    assert tool_names == {
        "read_program",
        "patch_program",
        "compile_environment",
        "probe_environment",
        "render_environment",
        "simulate_navigation",
    }
    assert captured["timeout"] == 120.0


def test_multiple_tool_calls_rejected_to_corrective_text() -> None:
    from envmaker.agent.providers import _parse_assistant_message

    message = {
        "content": None,
        "tool_calls": [
            {"function": {"name": "compile_environment", "arguments": "{}"}},
            {"function": {"name": "simulate_navigation", "arguments": "{}"}},
        ],
    }
    turn = _parse_assistant_message(message)
    assert turn.tool is None
    assert turn.text is not None and "only one tool call" in turn.text


def test_dotenv_export_prefix_supported(tmp_path) -> None:
    from envmaker.agent.providers import _read_dotenv_key

    env_file = tmp_path / ".env"
    env_file.write_text('export OPENAI_API_KEY="sk-live-abc12345"\n')
    assert _read_dotenv_key(env_file, "OPENAI_API_KEY") == "sk-live-abc12345"


def test_stderr_tail_redacts_before_truncating() -> None:
    from envmaker.agent.worker import _stderr_tail

    secret = "OPENAI_API_KEY=sk-" + "a" * 600
    blob = ("noise\n" + secret).encode("utf-8")
    tail = _stderr_tail(blob, chars=400)
    assert "sk-" + "a" * 20 not in tail
    assert "[redacted]" in tail or "aaaa" not in tail
