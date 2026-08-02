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
from envmaker.core.artifacts import canonical_json as _canonical_json
from envmaker.core.definition import seal_definition as _seal_definition
from envmaker.core.episode import NavigationProbe as _NavigationProbe
from envmaker.core.episode import TerminalReason as _TerminalReason
from envmaker.core.program import EnvironmentProgram as _EnvironmentProgram
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.requirements import PromptRequirementSet as _PromptRequirementSet
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
        "audit_render",
        "simulate_navigation",
    }
)
_DRIVER_TOOLS = frozenset(
    {"render_environment", "audit_render", "simulate_navigation"}
)


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
    definition_path: str | None = None
    definition_fingerprint: str | None = None


def _bound_json(payload: object) -> str:
    text = _json.dumps(payload, sort_keys=True, default=str)
    if len(text.encode("utf-8")) <= _RESULT_BYTES:
        return text
    return text.encode("utf-8")[:_RESULT_BYTES].decode("utf-8", errors="ignore")


def _is_image_message(message: dict[str, object]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in content
    )


def _is_tool_exchange(message: dict[str, object]) -> bool:
    content = message.get("content")
    if isinstance(content, list):
        # Multimodal audit TOOL_RESULT observations count as tool exchanges.
        return message.get("role") == "user"
    if not isinstance(content, str):
        return False
    if message.get("role") == "assistant" and content.startswith("TOOL_CALL "):
        return True
    if message.get("role") == "user" and (
        content.startswith("TOOL_RESULT ")
        or content.startswith("NO PROGRAM EXISTS YET")
        or content.startswith("Your last reply was empty")
    ):
        return True
    return False


def _evict_prior_image_messages(messages: list[dict[str, object]]) -> None:
    """Keep at most one image-bearing message in the transcript."""

    image_indices = [
        index for index, message in enumerate(messages) if _is_image_message(message)
    ]
    for index in reversed(image_indices[:-1]):
        messages.pop(index)


def _trim_messages(messages: list[dict[str, object]]) -> None:
    """Drop oldest tool exchanges first; never the system or task prompts.

    Rationale: dropping only TOOL_RESULT entries would starve the context of
    feedback while info-free TOOL_CALL stubs accumulated, and the fallback
    would eventually evict the task prompt itself. Content-array audit
    observations count as tool exchanges; at most one image message is kept.
    """

    _evict_prior_image_messages(messages)
    while len(messages) > _MESSAGE_CAP:
        drop_at = None
        # Indices 0 (system prompt) and 1 (task prompt) are pinned.
        for index, message in enumerate(messages):
            if index < 2:
                continue
            if _is_tool_exchange(message):
                drop_at = index
                break
        if drop_at is None:
            # Nothing tool-shaped left: drop the oldest entry after the two
            # pinned prompts.
            drop_at = 2 if len(messages) > 2 else None
        if drop_at is None:
            break
        messages.pop(drop_at)
        _evict_prior_image_messages(messages)


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


def select_landmark_probe(
    model: object,
    candidate: object,
) -> _NavigationProbe | None:
    """Resolve the first declared landmark to the canonical navigation probe.

    The ONLY probe-selection logic in the system: the first component in
    declaration order with ``payload["component"] == "landmark"``, resolved to
    its compiled ``{name}.0`` kit-part node. Returns None when no landmark
    resolves. cli.py and evaluation.py must use this helper, never a local
    heuristic.
    """

    node_ids = {
        node.semantic_id
        for node in getattr(getattr(candidate, "scene", None), "nodes", ())
    }
    for component in getattr(model, "components", ()):
        payload = getattr(component, "payload", {}) or {}
        if payload.get("component") != "landmark":
            continue
        candidate_id = f"{component.semantic_id}.0"
        if candidate_id in node_ids:
            return _NavigationProbe(
                target_landmark_id=candidate_id,
                success_radius_m=1.5,
                max_ticks=2400,
                action_repeat=1,
                allowed_connector_types=(),
                stuck_timeout_ticks=180,
                terminal_reasons=(
                    _TerminalReason.ARRIVED,
                    _TerminalReason.TIMEOUT,
                ),
            )
    return None


def _ensure_probe(context: _ToolContext) -> None:
    """Bind a navigation probe to the first declared landmark kit part."""

    static = context.static
    if static is None or static.model is None or static.candidate is None:
        return
    context.probe = select_landmark_probe(static.model, static.candidate)


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
    if name == "audit_render":
        return _result_payload(method())
    if name == "simulate_navigation":
        return _result_payload(method())
    return {"ok": False, "reason": f"unknown tool: {name}", "error": "tool_error"}


def _try_accept(
    context: _ToolContext, *, prompt: str, provider: _Provider
) -> tuple[str, str] | None:
    """Seal and persist the definition when all hard stages passed.

    Returns ``(definition_path, definition_fingerprint)`` relative to
    ``run_dir``, or ``None`` when acceptance preconditions are unmet.
    Persistence failures raise so the caller can downgrade to harness_error
    without ever reporting ``bundle_sealed=True``.
    """

    static = context.static
    if static is None or static.model is None or static.candidate is None:
        return None
    if not static.reports or not all(report.passed for report in static.reports):
        return None
    if not context.runtime_reports or not all(
        report.passed for report in context.runtime_reports
    ):
        return None
    bundle = _full_bundle(static, context.runtime_reports)
    if not bundle.all_passed():
        return None

    # Prompt compliance is scored as a human-audited evaluation dimension
    # (the eval YAML checklists), not a hard stage.
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
    definition = _seal_definition(
        static.candidate,
        bundle,
        requirements=requirements,
        program=program,
        model=static.model,
        navmesh_fingerprint=_canonical_fingerprint(
            {"candidate": static.candidate.candidate_fingerprint}
        ),
    )
    rel_path = "environment-definition.json"
    abs_path = context.run_dir / rel_path
    try:
        abs_path.write_text(_canonical_json(definition), encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(
            f"failed to persist sealed definition: {exc}"
        ) from exc
    context.runlog.append(
        "outcome",
        {
            "terminal_state": "accepted",
            "bundle_sealed": True,
            "definition_path": rel_path,
            "definition_fingerprint": definition.definition_fingerprint,
        },
    )
    return rel_path, definition.definition_fingerprint


class _CallbackRunLog:
    """RunLog wrapper that mirrors each append to an optional callback."""

    def __init__(
        self,
        path: _Path,
        on_event: _Callable[[str, dict], None] | None,
    ) -> None:
        self._inner = _RunLog(path)
        self._on_event = on_event

    def append(self, kind: str, payload: dict) -> None:
        self._inner.append(kind, payload)
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:
            pass

    def events(self) -> list[dict]:
        return self._inner.events()


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
    on_event: _Callable[[str, dict], None] | None = None,
) -> AuthoringOutcome:
    """Run the bounded authoring loop until acceptance or a terminal budget."""

    run_dir = _Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    revision_dir = run_dir / "revisions"
    runlog = _CallbackRunLog(run_dir / "runlog.jsonl", on_event)
    resource_limits = limits or _DEFAULT_LIMITS
    factory = driver_factory or _default_driver_factory

    context = _ToolContext(
        source="",
        limits=resource_limits,
        run_dir=run_dir,
        runlog=runlog,  # type: ignore[arg-type]
        min_walkable_fraction=min_walkable_fraction,
    )
    surface = _ToolSurface(context)
    driver: object | None = None
    turns_used = 0
    consecutive_failed_patches = 0
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
                else:
                    # Text instead of action never advances the run; tell the
                    # model exactly what does, based on pipeline state.
                    static = context.static
                    static_clean = (
                        static is not None
                        and static.model is not None
                        and bool(static.reports)
                        and all(report.passed for report in static.reports)
                    )
                    runtime_clean = bool(context.runtime_reports) and all(
                        report.passed for report in context.runtime_reports
                    )
                    if static_clean and not runtime_clean:
                        action_nudge = (
                            "Do not narrate. Call exactly one tool. If you "
                            "believe the program is ready, call "
                            "simulate_navigation now."
                        )
                        reason = "narration"
                    else:
                        action_nudge = (
                            "That reply was plain text, not an action. Typed "
                            "TOOL_CALL lines do nothing. Either make a real "
                            "tool call, or send the complete corrected "
                            "program in one ```python fenced block."
                        )
                        reason = "text while static failing"
                    messages.append({"role": "user", "content": action_nudge})
                    runlog.append(
                        "nudge",
                        {"turn": turns_used, "reason": reason},
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
                consecutive_failed_patches = 0
            elif tool_name == "patch_program":
                consecutive_failed_patches += 1
                if consecutive_failed_patches == 2:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Two patches in a row were rejected. Stop "
                                "patching: resend the COMPLETE corrected "
                                "program in one ```python fenced block."
                            ),
                        }
                    )
                    runlog.append(
                        "nudge",
                        {"turn": turns_used, "reason": "patch thrashing"},
                    )
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

            log_result: dict[str, object] = result
            if tool_name == "audit_render":
                # Never put image bytes / base64 through the runlog.
                images = result.get("images_b64") or ()
                log_result = {
                    "ok": result.get("ok"),
                    "refs": list(result.get("refs") or ()),
                    "byte_sizes": [
                        len(str(item).encode("utf-8")) for item in images
                    ],
                    "aesthetics": result.get("aesthetics") or {},
                    "reason": result.get("reason", ""),
                }
            tool_payload = {
                "name": tool_name,
                "args": tool_args,
                "turn": turns_used,
                "ok": result.get("ok"),
                "stage_outcomes": result.get("stage_outcomes", {}),
                "reason": result.get("reason", ""),
                "signal_codes": codes,
                "signal_messages": messages_text[:2000],
                "result": log_result,
            }
            # The full result already rides on tool_call.payload.result; a
            # separate tool_result event would duplicate it with no consumer.
            runlog.append("tool_call", tool_payload)

            messages.append(
                {
                    "role": "assistant",
                    "content": f"TOOL_CALL {tool_name} {_bound_json(tool_args)}",
                }
            )
            # Rendered as a user-side observation: OpenAI reserves role "tool"
            # for native tool_calls pairing, which this transcript does not use.
            # Successful audits become multimodal content arrays (images + text).
            if tool_name == "audit_render" and result.get("ok"):
                aesthetics_text = _bound_json(result.get("aesthetics") or {})
                content_parts: list[dict[str, object]] = [
                    {
                        "type": "text",
                        "text": (
                            "AUDIT_RENDER result (isometric, topdown) + "
                            f"aesthetics: {aesthetics_text}"
                        ),
                    }
                ]
                for image_b64 in result.get("images_b64") or ():
                    content_parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                            },
                        }
                    )
                messages.append({"role": "user", "content": content_parts})
                _evict_prior_image_messages(messages)
            else:
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
                accepted = _try_accept(
                    context, prompt=prompt, provider=provider
                )
                if accepted is not None:
                    definition_path, definition_fingerprint = accepted
                    terminal_state = "accepted"
                    bundle_sealed = True
                    return AuthoringOutcome(
                        terminal_state=terminal_state,
                        turns_used=turns_used,
                        final_source=context.source or None,
                        bundle_sealed=bundle_sealed,
                        run_dir=run_dir,
                        failure_summary=None,
                        definition_path=definition_path,
                        definition_fingerprint=definition_fingerprint,
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
