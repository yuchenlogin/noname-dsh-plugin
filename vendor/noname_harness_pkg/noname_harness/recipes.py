"""Model recipes: auditable role chains, not a scattering of model names.

A recipe describes *roles* for a task type (planner / worker / critic / ...),
not a hard-coded model list.  In this prototype a recipe is advisory only: it
is resolved, recorded in the ledger, and surfaced in the context package's
assembly metadata, but it does not yet drive real model routing -- there is no
external model to route to.  Recording recommendations now keeps the ledger
honest so routing rules can become explainable later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Task types the harness recognises.  A recipe is looked up by task type.
VALID_TASK_TYPES = {
    "question",
    "research",
    "code-change",
    "memory-write",
    "taste-card",
    "high-risk",
}


@dataclass(frozen=True)
class RoleSpec:
    """One role in a recipe chain."""

    role: str
    capability: str
    budget: str = "medium"
    different_family_from: str | None = None


@dataclass(frozen=True)
class Recipe:
    """A role chain for a task type, plus an optional fallback recipe id."""

    id: str
    task_type: str
    roles: tuple[RoleSpec, ...]
    fallback: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("recipe id cannot be empty")
        if self.task_type not in VALID_TASK_TYPES:
            raise ValueError(f"invalid task type: {self.task_type}")
        if not self.roles:
            raise ValueError("a recipe must define at least one role")

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "roles": [
                {
                    "role": r.role,
                    "capability": r.capability,
                    "budget": r.budget,
                    "different_family_from": r.different_family_from,
                }
                for r in self.roles
            ],
            "fallback": self.fallback,
            "notes": self.notes,
        }


# Default recipes, mirroring docs/runtime-architecture.md.  These are defaults
# a user can override; the registry never forces a choice.
DEFAULT_RECIPES: dict[str, Recipe] = {
    "question": Recipe(
        id="question-simple",
        task_type="question",
        roles=(RoleSpec("responder", "reasoning", "low"),),
    ),
    "research": Recipe(
        id="research-deep",
        task_type="research",
        roles=(
            RoleSpec("planner", "reasoning", "medium"),
            RoleSpec("researcher", "retrieval", "high"),
            RoleSpec("synthesizer", "reasoning", "medium"),
        ),
        fallback="question-simple",
    ),
    "code-change": Recipe(
        id="code-change-balanced",
        task_type="code-change",
        roles=(
            RoleSpec("planner", "reasoning", "medium"),
            RoleSpec("worker", "coding-tools", "high"),
            RoleSpec("critic", "independent-review", "medium", different_family_from="worker"),
        ),
        fallback="question-simple",
    ),
    "memory-write": Recipe(
        id="memory-write-reviewed",
        task_type="memory-write",
        roles=(
            RoleSpec("extractor", "extraction", "medium"),
            RoleSpec("conflict-checker", "independent-review", "low", different_family_from="extractor"),
            RoleSpec("human-review", "human", "high"),
        ),
        notes="extractor and checker are never the same model role",
    ),
    "taste-card": Recipe(
        id="taste-card-reviewed",
        task_type="taste-card",
        roles=(
            RoleSpec("clusterer", "clustering", "medium"),
            RoleSpec("visualizer", "image-generation", "medium"),
            RoleSpec("human-review", "human", "high"),
        ),
    ),
    "high-risk": Recipe(
        id="high-risk-gated",
        task_type="high-risk",
        roles=(
            RoleSpec("planner", "reasoning", "medium"),
            RoleSpec("policy-checker", "policy", "medium"),
            RoleSpec("human-approval", "human", "high"),
            RoleSpec("executor", "coding-tools", "high"),
        ),
    ),
}


def resolve_recipe(task_type: str, registry: dict[str, Recipe] | None = None) -> Recipe:
    """Return the default recipe for a task type.

    The registry is injectable so a project can override defaults without
    changing this module.  Resolution is deterministic and side-effect free.
    """

    if task_type not in VALID_TASK_TYPES:
        raise ValueError(f"unknown task type: {task_type}")
    recipes = registry if registry is not None else DEFAULT_RECIPES
    return recipes[task_type]
