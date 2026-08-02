"""Immutable prompt requirements linked to their source text."""

from __future__ import annotations

from enum import StrEnum as _StrEnum
from typing import Any as _Any

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint

__all__ = ["RequirementKind", "Requirement", "PromptRequirementSet"]


class RequirementKind(_StrEnum):
    """Kind of environment requirement expressed by a prompt."""

    CONTENT = "content"
    RELATION = "relation"
    MATERIAL = "material"
    STYLE = "style"
    EXTENT = "extent"
    LANDMARK = "landmark"
    UNSUPPORTED = "unsupported"


class Requirement(_BaseModel):
    """One prompt requirement and its source-character span."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    req_id: str = _Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    kind: RequirementKind
    text: str = _Field(min_length=1, max_length=2000)
    source_span: tuple[int, int]

    @_model_validator(mode="after")
    def _validate_source_span(self) -> Requirement:
        start, end = self.source_span
        if not 0 <= start < end:
            raise ValueError("source_span must satisfy 0 <= start < end")
        return self


class PromptRequirementSet(_BaseModel):
    """The immutable requirement inventory for one source prompt.

    Hard stages validate the component graph and geometry; prompt compliance
    against ``requirements`` is a human-audited evaluation dimension (eval YAML
    checklists), not a seal gate.
    """

    model_config = _ConfigDict(frozen=True, extra="forbid")

    prompt: str = _Field(min_length=1)
    requirements: tuple[Requirement, ...]
    prompt_fingerprint: str = _Field(default="", pattern=r"^[0-9a-f]{64}$")

    @_model_validator(mode="before")
    @classmethod
    def _fill_or_verify_fingerprint(cls, data: _Any) -> _Any:
        if not isinstance(data, dict) or "prompt" not in data:
            return data

        values = dict(data)
        canonical = _canonical_fingerprint({"prompt": values["prompt"]})
        supplied = values.get("prompt_fingerprint", "")
        if supplied == "":
            values["prompt_fingerprint"] = canonical
        elif supplied != canonical:
            raise ValueError("prompt_fingerprint mismatch")
        return values

    @_model_validator(mode="after")
    def _validate_requirements(self) -> PromptRequirementSet:
        req_ids = [requirement.req_id for requirement in self.requirements]
        if len(set(req_ids)) != len(req_ids):
            raise ValueError("requirement req_ids must be unique")

        prompt_length = len(self.prompt)
        for requirement in self.requirements:
            start, end = requirement.source_span
            if not 0 <= start < end <= prompt_length:
                raise ValueError("requirement source_span outside prompt bounds")
        return self
