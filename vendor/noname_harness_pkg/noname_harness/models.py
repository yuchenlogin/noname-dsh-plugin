"""Small data objects shared by the prototype services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvidenceInput:
    """A piece of source material attached to an event."""

    content: str
    artifact_uri: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None


@dataclass(frozen=True)
class Event:
    """An immutable event returned from the event store."""

    id: str
    session_id: str
    seq: int
    event_type: str
    payload: Any
    occurred_at: str
    content_hash: str


# ---------------------------------------------------------------------------
# Model capability & recipe contracts
#
# These are *contracts*, not clients.  The prototype never calls an external
# model; it only describes what a target model can do so that a context
# package can be projected to fit it.  A future Model Adapter will satisfy the
# same capability contract without the projection logic changing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCapability:
    """What a target model can do, in vendor-neutral terms.

    Business logic must choose models by capability, never by a provider
    field.  ``context_window`` is the only attribute the projection uses
    today; the rest are declared so recipes and routing can be recorded
    honestly before a real adapter exists.
    """

    reasoning: bool = False
    vision: bool = False
    tool_calling: bool = True
    streaming: bool = True
    # The usable context window in tokens.  None means "unbounded for our
    # purposes" -- the projection then keeps its default evidence window.
    context_window: int | None = None


@dataclass(frozen=True)
class ModelProfile:
    """A named target model: an id plus its capability and cost posture.

    ``budget`` is a coarse posture (``low``/``medium``/``high``) used to bias
    how much low-layer evidence the projection spends.  It never trims reviewed
    canon, task state, taste, or provenance -- only the low evidence window.
    """

    id: str
    capability: ModelCapability
    budget: str = "medium"  # low | medium | high

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("model profile id cannot be empty")
        if self.budget not in {"low", "medium", "high"}:
            raise ValueError(f"invalid budget posture: {self.budget}")
        if self.capability.context_window is not None and self.capability.context_window <= 0:
            raise ValueError("context_window must be positive when set")
