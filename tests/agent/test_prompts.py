"""Coverage for the authoring system prompt."""

from __future__ import annotations

from envmaker.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt


_BUILDER_METHODS = (
    "ground",
    "path",
    "water",
    "wall",
    "obstacle",
    "structure",
    "landmark",
    "prop",
    "scatter",
    "spawn",
    "camera",
    "freeze",
)
_TOOL_NAMES = (
    "read_program",
    "patch_program",
    "compile_environment",
    "probe_environment",
    "render_environment",
    "audit_render",
    "simulate_navigation",
)


def test_system_prompt_contract_and_budget() -> None:
    assert PROMPT_VERSION == "1"
    assert "def build_environment() -> EnvironmentModel" in SYSTEM_PROMPT
    assert "environment = build_environment()" in SYSTEM_PROMPT
    assert "from envmaker.sdk import EnvironmentBuilder, Polygon2D" in SYSTEM_PROMPT
    assert "import math" in SYSTEM_PROMPT
    for name in _BUILDER_METHODS:
        assert name in SYSTEM_PROMPT
    for name in _TOOL_NAMES:
        assert name in SYSTEM_PROMPT
    assert 'probe_environment("aesthetics")' in SYSTEM_PROMPT or '"aesthetics"' in SYSTEM_PROMPT
    assert "COMPOSITION" in SYSTEM_PROMPT
    assert "audit_render" in SYSTEM_PROMPT
    assert "IMMEDIATELY call simulate_navigation" in SYSTEM_PROMPT
    assert "yaw_jitter" in SYSTEM_PROMPT
    assert "scale_range" in SYSTEM_PROMPT
    assert "pine/vegetation/true (conical tree)" in SYSTEM_PROMPT
    assert "oak/vegetation/true (round canopy tree)" in SYSTEM_PROMPT
    assert "boulder/vegetation/true (rock cluster)" in SYSTEM_PROMPT
    assert len(SYSTEM_PROMPT) <= 6000


def test_build_user_prompt_includes_seed() -> None:
    text = build_user_prompt("a frozen village", seed=7)
    assert "a frozen village" in text
    assert "use seed 7 in EnvironmentBuilder" in text
