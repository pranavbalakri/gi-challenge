"""Bounded authoring loop: provider turns → tools → seal or budget stop."""

from __future__ import annotations

import json as _json
import traceback as _traceback
import uuid as _uuid
from collections.abc import Callable as _Callable
from pathlib import Path as _Path
from time import monotonic as _monotonic
from typing import Literal as _Literal

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict

from envmaker.agent.prompts import PROMPT_VERSION as _PROMPT_VERSION
from envmaker.agent.prompts import SYSTEM_PROMPT as _SYSTEM_PROMPT
from envmaker.agent.prompts import build_user_prompt as _build_user_prompt
from envmaker.agent.providers import Provider as _Provider
from envmaker.agent.providers import ProviderError as _ProviderError
from envmaker.agent.tools import ToolContext as _ToolContext
from envmaker.agent.tools import ToolSurface as _ToolSurface
from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.definition import seal_definition as _seal_definition
from envmaker.core.episode import NavigationProbe as _NavigationProbe
from envmaker.core.episode import TerminalReason as _TerminalReason
from envmaker.core.program import EnvironmentProgram as _EnvironmentProgram
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.requirements import PromptRequirementSet as _PromptRequirementSet
from envmaker.core.scene_spec import PlaneVisual as _PlaneVisual
from envmaker.runlog import RunLog as _RunLog
from envmaker.sdk import SDK_VERSION as _SDK_VERSION
from envmaker.validation import full_bundle as _full_bundle

__all__ = ["AuthoringOutcome", "run_authoring"]

_MESSAGE_CAP = 40
_RESULT_BYTES = 4 * 1024
_DEFAULT_LIMITS = _ResourceLimits(
    cpu_seconds=30.0,
    memory_mb=512,
    output_bytes=1_048_576,
    wall_seconds=60.0,
)
_TOOL_METHODS = frozenset(
    {
        "read_program",
        "patch_program",
        "compile_environment",
        "probe_environment",
        "render_environment",
        "simulate_navigation",
    }
)
_DRIVER_TOOLS = frozenset({"render_environment", "simulate_navigation"})


class AuthoringOutcome(_BaseModel):
    """Terminal outcome of one bounded authoring run."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    terminal_state: _Literal[
        "accepted",
        "rejected_after_budget",
        "provider_error",
        "harness_error",
    ]
    turns_used: int
    final_source: str | None
    bundle_sealed: bool
    run_dir: _Path
    failure_summary: str | None = None


def _bound_json(payload: object) -> str:
    text = _json.dumps(payload, sort_keys=True, default=str)
    if len(text.encode("utf-8")) <= _RESULT_BYTES:
        return text
    return text.encode("utf-8")[:_RESULT_BYTES].decode("utf-8", errors="ignore")


def _trim_messages(messages: list[dict[str, object]]) -> None:
    while len(messages) > _MESSAGE_CAP:
        drop_at = None
        for index, message in enumerate(messages):
            if index == 0:
                continue
            content = message.get("content")
            if (
                message.get("role") == "user"
                and isinstance(content, str)
                and content.startswith("TOOL_RESULT ")
            ):
                drop_at = index
                break
        if drop_at is None:
            # Never drop the system prompt; drop the oldest non-system entry.
            drop_at = 1 if len(messages) > 1 else None
        if drop_at is None:
            break
        messages.pop(drop_at)


def _default_driver_factory(run_dir: _Path) -> object:
    from envmaker.runtime import RuntimeDriver

    # Session ids are contract-bound to ^[a-z0-9][a-z0-9-]{0,63}$; run_dir
    # names (e.g. pytest tmp dirs with underscores) are not, so mint one.
    driver = RuntimeDriver(
        run_dir=run_dir / "runtime",
        session_id="run-" + _uuid.uuid4().hex[:12],
        windowed=True,
    )
    driver.start()
    return driver


def _ensure_probe(context: _ToolContext) -> None:
    static = context.static
    if static is None or static.candidate is None:
        return
    landmark_id = None
    for node in static.candidate.scene.nodes:
        if isinstance(node.visual, _PlaneVisual):
            continue
        if node.collider is None and "." in node.semantic_id:
            landmark_id = node.semantic_id
            break
        if node.collider is None:
            landmark_id = node.semantic_id
            break
    if landmark_id is None:
        for component in static.model.components if static.model else ():
            if component.payload.get("component") == "landmark":
                landmark_id = f"{component.semantic_id}.0"
                break
    if landmark_id is None:
        return
    context.probe = _NavigationProbe(
        target_landmark_id=landmark_id,
        success_radius_m=1.5,
        max_ticks=2400,
        action_repeat=1,
        allowed_connector_types=(),
        stuck_timeout_ticks=180,
        terminal_reasons=(_TerminalReason.ARRIVED, _TerminalReason.TIMEOUT),
    )


def _save_revision(context: _ToolContext, revision_dir: _Path) -> int:
    revision_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(revision_dir.glob("rev-*.py"))
    next_index = len(existing) + 1
    path = revision_dir / f"rev-{next_index}.py"
    path.write_text(context.source, encoding="utf-8")
    context.runlog.append(
        "revision",
        {
            "index": next_index,
            "path": str(path.relative_to(context.run_dir)),
            "bytes": len(context.source.encode("utf-8")),
        },
    )
    return next_index


def _result_payload(result: object) -> dict[str, object]:
    if hasattr(result, "model_dump"):
        dumped = result.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(result, dict):
        return dict(result)
    if isinstance(result, str):
        return {"ok": True, "source": result}
    return {"ok": True, "value": str(result)}


def _dispatch_tool(
    surface: _ToolSurface,
    name: str,
    args: dict[str, object],
) -> dict[str, object]:
    if name not in _TOOL_METHODS:
        return {"ok": False, "reason": f"unknown tool: {name}", "error": "tool_error"}
    method = getattr(surface, name)
    if name == "read_program":
        return _result_payload(method())
    if name == "patch_program":
        return _result_payload(method(str(args.get("patch", ""))))
    if name == "compile_environment":
        return _result_payload(method())
    if name == "probe_environment":
        return _result_payload(method(str(args.get("query", ""))))
    if name == "render_environment":
        return _result_payload(method(str(args.get("view", ""))))
    if name == "simulate_navigation":
        return _result_payload(method())
    return {"ok": False, "reason": f"unknown tool: {name}", "error": "tool_error"}


def _try_accept(context: _ToolContext, *, prompt: str, provider: _Provider) -> bool:
    static = context.static
    if static is None or static.model is None or static.candidate is None:
        return False
    if not static.reports or not all(report.passed for report in static.reports):
        return False
    if not context.runtime_reports or not all(
        report.passed for report in context.runtime_reports
    ):
        return False
    bundle = _full_bundle(static, context.runtime_reports)
    if not bundle.all_passed():
        return False

    requirements = _PromptRequirementSet(prompt=prompt, requirements=())
    descriptor = provider.descriptor
    if descriptor.prompt_version != _PROMPT_VERSION:
        descriptor = descriptor.model_copy(
            update={"prompt_version": _PROMPT_VERSION}
        )
    program = _EnvironmentProgram(
        source=context.source,
        sdk_version=_SDK_VERSION,
        prompt_fingerprint=requirements.prompt_fingerprint,
        provider=descriptor,
    )
    _seal_definition(
        static.candidate,
        bundle,
        requirements=requirements,
        program=program,
        model=static.model,
        navmesh_fingerprint=_canonical_fingerprint(
            {"candidate": static.candidate.candidate_fingerprint}
        ),
    )
    context.runlog.append(
        "outcome",
        {"terminal_state": "accepted", "bundle_sealed": True},
    )
    return True


def run_authoring(
    prompt: str,
    *,
    provider: _Provider,
    seed: int,
    max_turns: int = 8,
    wall_seconds: float,
    run_dir: _Path,
    driver_factory: _Callable[[_Path], object] | None = None,
    limits: _ResourceLimits | None = None,
    min_walkable_fraction: float = 0.5,
) -> AuthoringOutcome:
    """Run the bounded authoring loop until acceptance or a terminal budget."""

    run_dir = _Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    revision_dir = run_dir / "revisions"
    runlog = _RunLog(run_dir / "runlog.jsonl")
    resource_limits = limits or _DEFAULT_LIMITS
    factory = driver_factory or _default_driver_factory

    context = _ToolContext(
        source="",
        limits=resource_limits,
        run_dir=run_dir,
        runlog=runlog,
        min_walkable_fraction=min_walkable_fraction,
    )
    surface = _ToolSurface(context)
    driver: object | None = None
    turns_used = 0
    started = _monotonic()

    user_prompt = _build_user_prompt(prompt, seed)
    messages: list[dict[str, object]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    runlog.append("system_prompt", {"version": _PROMPT_VERSION})
    runlog.append("user_prompt", {"prompt": prompt, "seed": seed})

    terminal_state: _Literal[
        "accepted",
        "rejected_after_budget",
        "provider_error",
        "harness_error",
    ] = "rejected_after_budget"
    failure_summary: str | None = None
    bundle_sealed = False

    try:
        while turns_used < max_turns:
            if _monotonic() - started >= wall_seconds:
                terminal_state = "rejected_after_budget"
                failure_summary = "wall clock budget exhausted"
                break

            try:
                turn = provider.next_turn(messages)
            except _ProviderError as exc:
                terminal_state = "provider_error"
                failure_summary = str(exc)
                runlog.append(
                    "outcome",
                    {
                        "terminal_state": terminal_state,
                        "failure_summary": failure_summary,
                    },
                )
                return AuthoringOutcome(
                    terminal_state=terminal_state,
                    turns_used=turns_used,
                    final_source=context.source or None,
                    bundle_sealed=False,
                    run_dir=run_dir,
                    failure_summary=failure_summary,
                )

            turns_used += 1
            runlog.append(
                "provider_turn",
                {
                    "turn": turns_used,
                    "has_code": turn.code is not None,
                    "tool": turn.tool,
                    "has_text": turn.text is not None,
                },
            )

            if turn.code is not None:
                context.source = turn.code
                context.static = None
                context.runtime_reports = []
                _save_revision(context, revision_dir)
                messages.append({"role": "assistant", "content": turn.code})
                _trim_messages(messages)
                continue

            if turn.text is not None:
                runlog.append("signals", {"text": turn.text[:2000]})
                messages.append({"role": "assistant", "content": turn.text})
                if not str(turn.text).strip():
                    empty_nudge = (
                        "Your last reply was empty. Call exactly one tool, or "
                        "send the complete program in one ```python fenced "
                        "block."
                    )
                    messages.append({"role": "user", "content": empty_nudge})
                    runlog.append(
                        "nudge", {"turn": turns_used, "reason": "empty reply"}
                    )
                _trim_messages(messages)
                continue

            assert turn.tool is not None
            tool_name = turn.tool
            tool_args = dict(turn.args)

            if tool_name in _DRIVER_TOOLS and context.driver is None:
                driver = factory(run_dir)
                context.driver = driver

            try:
                result = _dispatch_tool(surface, tool_name, tool_args)
            except Exception as exc:
                result = {
                    "ok": False,
                    "reason": f"tool raised: {exc}",
                    "error": "tool_error",
                }

            if tool_name == "patch_program" and result.get("ok"):
                _save_revision(context, revision_dir)
            if tool_name == "compile_environment":
                _ensure_probe(context)

            codes: list[str] = []
            messages_text = ""
            for signal in result.get("signals") or []:
                if isinstance(signal, dict):
                    code = signal.get("code")
                    messages_text += str(signal.get("message", "")) + "\n"
                else:
                    code = getattr(signal, "code", None)
                    messages_text += str(getattr(signal, "message", "")) + "\n"
                if code:
                    codes.append(str(code))

            if codes or tool_name in {"compile_environment", "simulate_navigation"}:
                runlog.append(
                    "signals",
                    {
                        "tool": tool_name,
                        "ok": result.get("ok"),
                        "signal_codes": codes,
                        "messages": messages_text[:2000],
                    },
                )

            tool_payload = {
                "name": tool_name,
                "args": tool_args,
                "turn": turns_used,
                "ok": result.get("ok"),
                "stage_outcomes": result.get("stage_outcomes", {}),
                "reason": result.get("reason", ""),
                "signal_codes": codes,
                "signal_messages": messages_text[:2000],
                "result": result,
            }
            runlog.append("tool_call", tool_payload)
            runlog.append(
                "tool_result",
                {
                    "name": tool_name,
                    "ok": result.get("ok"),
                    "result": result,
                },
            )

            messages.append(
                {
                    "role": "assistant",
                    "content": f"TOOL_CALL {tool_name} {_bound_json(tool_args)}",
                }
            )
            # Rendered as a user-side observation: OpenAI reserves role "tool"
            # for native tool_calls pairing, which this transcript does not use.
            messages.append(
                {
                    "role": "user",
                    "content": f"TOOL_RESULT {tool_name} {_bound_json(result)}",
                }
            )
            if not context.source.strip():
                nudge = (
                    "NO PROGRAM EXISTS YET. Do not call tools. Reply with the "
                    "complete program in one ```python fenced block."
                )
                messages.append({"role": "user", "content": nudge})
                runlog.append("nudge", {"turn": turns_used, "reason": "empty source"})
            _trim_messages(messages)

            if tool_name == "simulate_navigation" and result.get("ok"):
                if _try_accept(context, prompt=prompt, provider=provider):
                    terminal_state = "accepted"
                    bundle_sealed = True
                    return AuthoringOutcome(
                        terminal_state=terminal_state,
                        turns_used=turns_used,
                        final_source=context.source or None,
                        bundle_sealed=bundle_sealed,
                        run_dir=run_dir,
                        failure_summary=None,
                    )

        if terminal_state != "accepted":
            terminal_state = "rejected_after_budget"
            if failure_summary is None:
                failure_summary = "turn or wall budget exhausted"
            runlog.append(
                "outcome",
                {
                    "terminal_state": terminal_state,
                    "failure_summary": failure_summary,
                },
            )
    except _ProviderError as exc:
        terminal_state = "provider_error"
        failure_summary = str(exc)
        runlog.append(
            "outcome",
            {"terminal_state": terminal_state, "failure_summary": failure_summary},
        )
    except Exception as exc:
        terminal_state = "harness_error"
        failure_summary = str(exc)
        runlog.append(
            "outcome",
            {
                "terminal_state": terminal_state,
                "failure_summary": failure_summary,
                "traceback": _traceback.format_exc()[-4000:],
            },
        )
    finally:
        if driver is not None and hasattr(driver, "close"):
            try:
                driver.close()
            except Exception:
                pass
        elif context.driver is not None and hasattr(context.driver, "close"):
            try:
                context.driver.close()
            except Exception:
                pass

    return AuthoringOutcome(
        terminal_state=terminal_state,
        turns_used=turns_used,
        final_source=context.source or None,
        bundle_sealed=bundle_sealed,
        run_dir=run_dir,
        failure_summary=failure_summary,
    )
