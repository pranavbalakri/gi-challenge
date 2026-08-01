"""Agent harness surfaces for EnvMaker generated programs.

Re-exports are lazy (PEP 562): ``envmaker.validation`` imports
``envmaker.agent.worker`` while ``envmaker.agent.tools`` imports
``envmaker.validation``, so an eager package ``__init__`` would create an
import cycle for whichever module loads first.
"""

from __future__ import annotations

import importlib as _importlib
from typing import Any as _Any

__all__ = [
    "AuthoringOutcome",
    "CompileResult",
    "NavigationResult",
    "OpenAIProvider",
    "PROMPT_VERSION",
    "PatchResult",
    "ProbeResult",
    "Provider",
    "ProviderError",
    "ProviderTurn",
    "RenderResult",
    "SYSTEM_PROMPT",
    "ScriptedProvider",
    "ToolContext",
    "ToolSurface",
    "build_user_prompt",
    "run_authoring",
    "run_generated_program",
]

_EXPORT_MODULES: dict[str, str] = {
    "AuthoringOutcome": "envmaker.agent.loop",
    "CompileResult": "envmaker.agent.tools",
    "NavigationResult": "envmaker.agent.tools",
    "OpenAIProvider": "envmaker.agent.providers",
    "PROMPT_VERSION": "envmaker.agent.prompts",
    "PatchResult": "envmaker.agent.tools",
    "ProbeResult": "envmaker.agent.tools",
    "Provider": "envmaker.agent.providers",
    "ProviderError": "envmaker.agent.providers",
    "ProviderTurn": "envmaker.agent.providers",
    "RenderResult": "envmaker.agent.tools",
    "SYSTEM_PROMPT": "envmaker.agent.prompts",
    "ScriptedProvider": "envmaker.agent.providers",
    "ToolContext": "envmaker.agent.tools",
    "ToolSurface": "envmaker.agent.tools",
    "build_user_prompt": "envmaker.agent.prompts",
    "run_authoring": "envmaker.agent.loop",
    "run_generated_program": "envmaker.agent.worker",
}


def __getattr__(name: str) -> _Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
