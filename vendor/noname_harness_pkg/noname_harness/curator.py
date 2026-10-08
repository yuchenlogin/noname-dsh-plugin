"""The first, deliberately conservative, state-curator service.

This module does not pretend to be an LLM.  It defines the contract a future
model-powered curator must satisfy: durable proposals need an explicit layer,
logical key, source event and confidence.  For now, structured hints in event
payloads are enough to exercise the full review and handoff path.
"""

from __future__ import annotations

from typing import Any

from .store import HarnessStore


class CuratorService:
    """Turn structured event hints into reviewable high/mid-layer proposals."""

    EVENT_LAYER_HINTS = {
        "project.goal": "high",
        "project.constraint": "high",
        "decision.accepted": "high",
        "canon.candidate": "high",
        "phase.updated": "mid",
        "task.updated": "mid",
        "task.blocked": "mid",
        "task.progress": "mid",
    }

    def __init__(self, store: HarnessStore):
        self.store = store

    def propose_from_event(self, event_id: str) -> dict[str, Any] | None:
        """Create one proposal if the event contains a durable-state hint.

        A producer may attach a ``candidate`` object to an event::

            {
              "candidate": {
                "layer": "mid",
                "kind": "task-state",
                "key": "current_task",
                "content": {"goal": "..."},
                "confidence": 0.92
              }
            }

        Recognised event types can omit ``candidate`` but still need ``key``
        and ``content``.  Low-level events are intentionally left as evidence.
        """

        event = self.store.get_event(event_id)
        payload = event.payload if isinstance(event.payload, dict) else {}
        candidate = payload.get("candidate")
        if candidate is not None:
            if not isinstance(candidate, dict):
                raise ValueError("event candidate must be an object")
            layer = candidate.get("layer")
            key = candidate.get("key")
            content = candidate.get("content")
            kind = candidate.get("kind", "state")
            confidence = candidate.get("confidence")
            reason = candidate.get("reason")
        else:
            layer = self.EVENT_LAYER_HINTS.get(event.event_type)
            key = payload.get("key")
            content = payload.get("content")
            kind = payload.get("kind", "state")
            confidence = payload.get("confidence")
            reason = payload.get("reason")

        if layer is None or key is None or content is None:
            return None
        if self.store.proposal_exists_for_event(event_id, logical_key=str(key)):
            return None
        return self.store.create_proposal(
            str(layer),
            str(key),
            content,
            [event_id],
            kind=str(kind),
            proposed_by="curator",
            confidence=confidence,
            reason=reason,
        )

    def scan(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Scan recent events and return newly created proposals."""

        events = self.store.list_events(session_id=session_id, limit=limit)
        proposals: list[dict[str, Any]] = []
        for event in reversed(events):
            proposal = self.propose_from_event(event.id)
            if proposal is not None:
                proposals.append(proposal)
        return proposals
