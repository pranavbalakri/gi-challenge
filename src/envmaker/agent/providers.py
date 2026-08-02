"""Provider adapters for the EnvMaker authoring loop."""

from __future__ import annotations

import json as _json
import os as _os
import re as _re
from collections.abc import Sequence as _Sequence
from pathlib import Path as _Path
from typing import Protocol as _Protocol
from typing import runtime_checkable as _runtime_checkable

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.program import ProviderInfo as _ProviderInfo

__all__ = [
    "ProviderTurn",
    "ProviderError",
    "Provider",
    "ScriptedProvider",
    "OpenAIProvider",
    "_parse_assistant_message",
    "_strip_python_fence",
]

_PYTHON_FENCE = _re.compile(
    r"```(?:python)?\s*\n(.*?)```",
    _re.DOTALL | _re.IGNORECASE,
)
_TOOL_CALL_MIMICRY = _re.compile(
    r"^\s*TOOL_CALL\s+([a-z_]+)\s*(\{.*\})?\s*$",
    _re.DOTALL,
)
_REPO_ROOT = _Path(__file__).resolve().parents[3]

_TOOL_SCHEMAS: dict[str, dict[str, object]] = {
    "read_program": {
        "name": "read_program",
        "description": "Return the current environment.py source.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "patch_program": {
        "name": "patch_program",
        "description": (
            "Apply a unified diff or search/replace patch to the program source."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "Unified diff or SEARCH/REPLACE block",
                }
            },
            "required": ["patch"],
            "additionalProperties": False,
        },
    },
    "compile_environment": {
        "name": "compile_environment",
        "description": (
            "Run static validation stages program through scene (V1–V5)."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "probe_environment": {
        "name": "probe_environment",
        "description": (
            "Read-only measurements over the latest compiled candidate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "component <id> | bounds | blockers | spawn | "
                        "aesthetics | route x1 z1 x2 z2"
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "render_environment": {
        "name": "render_environment",
        "description": "Capture an isometric or topdown render artifact ref.",
        "parameters": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["isometric", "topdown"],
                }
            },
            "required": ["view"],
            "additionalProperties": False,
        },
    },
    "audit_render": {
        "name": "audit_render",
        "description": (
            "Capture isometric+topdown screenshots with bounded JPEG feedback "
            "and aesthetics measurements."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "simulate_navigation": {
        "name": "simulate_navigation",
        "description": (
            "Run live-runtime stages materialization through camera (V6–V7)."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


class ProviderTurn(_BaseModel):
    """One provider response: exactly one of code, tool, or text."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    code: str | None = None
    tool: str | None = None
    args: dict[str, object] = _Field(default_factory=dict)
    text: str | None = None

    @_model_validator(mode="after")
    def _exactly_one_channel(self) -> ProviderTurn:
        populated = sum(
            1
            for value in (self.code, self.tool, self.text)
            if value is not None
        )
        if populated != 1:
            raise ValueError(
                "exactly one of code, tool, or text must be populated"
            )
        return self


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce the next turn."""


@_runtime_checkable
class Provider(_Protocol):
    """Provider seam used by the authoring loop."""

    def next_turn(self, messages: list[dict[str, object]]) -> ProviderTurn: ...

    @property
    def descriptor(self) -> _ProviderInfo: ...


def _strip_python_fence(content: str) -> str | None:
    match = _PYTHON_FENCE.search(content)
    if match is None:
        return None
    return match.group(1)


def _parse_assistant_message(message: dict[str, object]) -> ProviderTurn:
    """Map a chat-completion-shaped assistant message to a ProviderTurn."""

    tool_calls = message.get("tool_calls")
    if tool_calls and len(tool_calls) > 1:
        # One tool per reply is the loop contract; a silent first-only pick
        # would leave the model believing every action ran.
        return ProviderTurn(
            text=(
                "MULTIPLE TOOL CALLS SENT; only one tool call is allowed per "
                "reply. Resend exactly one tool call."
            )
        )
    if tool_calls:
        first = tool_calls[0]
        if isinstance(first, dict):
            function = first.get("function", {})
        else:
            function = getattr(first, "function", None)
            if function is not None and not isinstance(function, dict):
                function = {
                    "name": getattr(function, "name", ""),
                    "arguments": getattr(function, "arguments", "{}"),
                }
        if not isinstance(function, dict):
            raise ProviderError("tool call missing function payload")
        name = str(function.get("name", ""))
        raw_args = function.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                parsed = _json.loads(raw_args) if raw_args else {}
            except _json.JSONDecodeError as exc:
                raise ProviderError("tool arguments are not valid JSON") from exc
        elif isinstance(raw_args, dict):
            parsed = raw_args
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            raise ProviderError("tool arguments must decode to an object")
        return ProviderTurn(tool=name, args=dict(parsed))

    content = message.get("content")
    text = "" if content is None else str(content)
    fenced = _strip_python_fence(text)
    if fenced is not None:
        return ProviderTurn(code=fenced)
    if "def build_environment" in text and "environment = build_environment()" in text:
        # Bare-code recovery: weak models sometimes drop the fence around an
        # otherwise complete program; losing it to a text turn wastes budget.
        return ProviderTurn(code=text)
    mimicry = _TOOL_CALL_MIMICRY.match(text)
    if mimicry is not None and mimicry.group(1) in _TOOL_SCHEMAS:
        # Weak models imitate the rendered transcript ("TOOL_CALL x {}") as
        # plain text instead of emitting native tool calls; the intent is
        # unambiguous, so execute it rather than burning a turn on a nudge.
        raw_args = mimicry.group(2) or "{}"
        try:
            parsed_args = _json.loads(raw_args)
        except _json.JSONDecodeError:
            parsed_args = {}
        if not isinstance(parsed_args, dict):
            parsed_args = {}
        return ProviderTurn(tool=mimicry.group(1), args=parsed_args)
    return ProviderTurn(text=text)


def _read_dotenv_key(path: _Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


class ScriptedProvider:
    """Replay a fixed sequence of provider turns (keyless fixtures)."""

    def __init__(
        self,
        turns: _Sequence[ProviderTurn],
        *,
        descriptor: _ProviderInfo,
    ) -> None:
        self._turns = list(turns)
        self._index = 0
        self._descriptor = descriptor

    @property
    def descriptor(self) -> _ProviderInfo:
        return self._descriptor

    def next_turn(self, messages: list[dict[str, object]]) -> ProviderTurn:
        del messages
        if self._index >= len(self._turns):
            raise ProviderError("scripted provider transcript exhausted")
        turn = self._turns[self._index]
        self._index += 1
        return turn

    @classmethod
    def from_fixture(cls, path: _Path | str) -> ScriptedProvider:
        fixture_path = _Path(path)
        payload = _json.loads(fixture_path.read_text(encoding="utf-8"))
        raw_turns = payload.get("turns", [])
        if not isinstance(raw_turns, list):
            raise ProviderError("fixture turns must be a list")
        turns: list[ProviderTurn] = []
        for entry in raw_turns:
            if not isinstance(entry, dict):
                raise ProviderError("fixture turn must be an object")
            if "code_file" in entry:
                code_path = fixture_path.parent / str(entry["code_file"])
                turns.append(
                    ProviderTurn(code=code_path.read_text(encoding="utf-8"))
                )
                continue
            if "tool" in entry:
                args = dict(entry.get("args") or {})
                if "patch_file" in entry:
                    patch_path = fixture_path.parent / str(entry["patch_file"])
                    args["patch"] = patch_path.read_text(encoding="utf-8")
                turns.append(ProviderTurn(tool=str(entry["tool"]), args=args))
                continue
            if "text" in entry:
                turns.append(ProviderTurn(text=str(entry["text"])))
                continue
            raise ProviderError(
                "fixture turn must include code_file, tool, or text"
            )
        descriptor = _ProviderInfo(
            provider=str(payload.get("provider", "scripted")),
            model_name=str(payload.get("model_name", "fixture")),
            prompt_version=str(payload.get("prompt_version", "1")),
        )
        return cls(turns, descriptor=descriptor)


class OpenAIProvider:
    """Live OpenAI chat-completions provider (lazy import; no network in tests)."""

    def __init__(
        self,
        model_name: str = "gpt-4o-mini",
        *,
        api_key: str | None = None,
        env_file: _Path | str | None = None,
        request_timeout: float = 120.0,
    ) -> None:
        self._model_name = model_name
        self._request_timeout = request_timeout
        resolved = api_key
        if resolved is None:
            resolved = _os.environ.get("OPENAI_API_KEY")
        if resolved is None:
            dotenv = _Path(env_file) if env_file is not None else _REPO_ROOT / ".env"
            resolved = _read_dotenv_key(dotenv, "OPENAI_API_KEY")
        if not resolved:
            raise ProviderError("OPENAI_API_KEY is not configured")
        self._api_key = resolved
        self._descriptor = _ProviderInfo(
            provider="openai",
            model_name=model_name,
            prompt_version="1",
        )

    @property
    def descriptor(self) -> _ProviderInfo:
        return self._descriptor

    def next_turn(self, messages: list[dict[str, object]]) -> ProviderTurn:
        try:
            import openai as _openai
        except ImportError as exc:
            raise ProviderError("openai package is not installed") from exc

        tools = [
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
            }
            for schema in _TOOL_SCHEMAS.values()
        ]
        # No extra system message: the loop's SYSTEM_PROMPT (messages[0])
        # already carries the FIRST MOVE contract; duplicating it here would
        # invite drift between the two copies.
        try:
            client = _openai.OpenAI(api_key=self._api_key)
            response = client.chat.completions.create(
                model=self._model_name,
                messages=list(messages),
                tools=tools,
                timeout=self._request_timeout,
            )
        except Exception as exc:
            raise ProviderError(f"openai request failed: {exc}") from exc

        try:
            choice = response.choices[0].message
            message = {
                "content": choice.content,
                "tool_calls": choice.tool_calls,
            }
            return _parse_assistant_message(message)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"failed to parse openai response: {exc}") from exc
