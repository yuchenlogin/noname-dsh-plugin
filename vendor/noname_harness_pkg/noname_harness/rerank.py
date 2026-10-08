"""Rerank: the second stage of three-stage retrieval (docs/memory-model §6).

Recall (FTS5 + vector similarity + scope/time filtering) produces candidates;
**rerank** orders them by the contract's dimensions -- task relevance, source
quality, review status, freshness and conflict.  Reranking:

- **is a projection, never a new fact.**  It only re-orders candidates and
  annotates *why* (``rerank_reasons``), never alters the underlying
  events/evidence.  The original recall order is always recoverable.
- **is explainable.**  Every ranked result carries the reasons that placed it,
  so a person can see *why* something surfaced, not just that it did.
- **is injectable.**  A real reranker model plugs in behind the same
  ``RerankFn`` protocol; the default is a deterministic multi-dimensional
  scorer so the pipeline works and is verifiable offline.

Rerank applies only to the factual layer (events/evidence); taste retrieval is
separate and never reranked here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

# A rerank function maps (query, candidates) to re-scored, re-ordered candidates.
RerankFn = Callable[[str, list[dict[str, Any]]], list["RankedCandidate"]]


@dataclass(frozen=True)
class RankedCandidate:
    """A recall candidate after reranking, with its score and reasons."""

    event: Any
    similarity: float
    ref_id: str
    score: float
    rerank_reasons: tuple[str, ...]


# Signal weights for the default scorer.  These are coarse, documented
# heuristics -- a real reranker model replaces them behind the same protocol.
_W_RELEVANCE = 1.0      # task relevance (vector similarity)
_W_SOURCE = 0.15        # source quality (evidence count, event type)
_W_REVIEW = 0.20        # review status (promoted to canon/task state)
_W_FRESH = 0.10         # freshness (recency)

# Event types that are higher-signal sources for a work handoff.
_HIGH_SIGNAL_TYPES = {"test.failed", "artifact.changed", "decision.accepted", "task.blocked"}


def _freshness_score(occurred_at: str, reference: str) -> float:
    """A simple recency score in [0, 1]: newer is higher, ~30-day half-life."""

    def parse(text: str) -> datetime:
        if not isinstance(text, str):
            raise TypeError("timestamp must be a string")
        return datetime.fromisoformat(text.replace("Z", "+00:00"))

    try:
        age_seconds = max(0.0, (parse(reference) - parse(occurred_at)).total_seconds())
    except (ValueError, TypeError, AttributeError):
        return 0.5
    half_life = 30 * 24 * 3600
    # A true half-life decay: the score is 0.5 at exactly one half-life.
    return 2.0 ** (-age_seconds / half_life)


def default_rerank(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    promoted_event_ids: frozenset[str] = frozenset(),
    reference_time: str | None = None,
    now_fn: Callable[[], str] | None = None,
    weights: dict[str, float] | None = None,
) -> list[RankedCandidate]:
    """A deterministic multi-dimensional reranker (the contract's dimensions).

    Scores each candidate on task relevance (similarity), source quality
    (evidence count + event type), review status (promoted to durable state)
    and freshness (recency), then orders by the weighted total.  Every result
    carries the reasons behind its placement.  This is the deterministic
    stand-in for a reranker model; it is fully explainable.
    """

    if now_fn is None:
        from datetime import timezone

        now_fn = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    reference = reference_time or now_fn()
    w = {
        "relevance": _W_RELEVANCE,
        "source": _W_SOURCE,
        "review": _W_REVIEW,
        "fresh": _W_FRESH,
    }
    if weights:
        w.update(weights)

    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        event = candidate["event"]
        similarity = float(candidate.get("similarity", 0.0))
        reasons: list[str] = []

        # Task relevance (dominant signal).
        relevance = w["relevance"] * similarity
        reasons.append(f"相关性 {similarity:.2f}")

        # Source quality: evidence count + high-signal event type.
        evidence_count = len(candidate.get("evidence", []))
        source_score = 0.0
        if evidence_count:
            source_score += 0.5
            reasons.append(f"{evidence_count} 条证据")
        if event.event_type in _HIGH_SIGNAL_TYPES:
            source_score += 0.5
            reasons.append(f"高信号类型 {event.event_type}")

        # Review status: promoted to durable canon/task state.  Read from the
        # candidate's own ``promoted`` flag (set by the store) so the signal is
        # available no matter how this reranker was invoked or wrapped.
        review_score = 0.0
        if candidate.get("promoted") or event.id in promoted_event_ids:
            review_score = 1.0
            reasons.append("已提升为法典/任务态")

        # Freshness.
        fresh = _freshness_score(event.occurred_at, reference)
        reasons.append(f"新鲜度 {fresh:.2f}")

        score = (
            relevance
            + w["source"] * source_score
            + w["review"] * review_score
            + w["fresh"] * fresh
        )
        ranked.append(
            RankedCandidate(
                event=event,
                similarity=similarity,
                ref_id=candidate.get("ref_id", event.id[:12]),
                score=score,
                rerank_reasons=tuple(reasons),
            )
        )
    ranked.sort(key=lambda item: (item.score, item.similarity, item.event.id), reverse=True)
    return ranked
