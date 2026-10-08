"""Router: an explicit, reasoned choice about what happens to the context.

The vision's first principle: a session faces a choice -- continue the current
context, or be reborn into a new one carrying key memory.  That choice belongs
to an explicit **router**, decided jointly by phase boundary, saturation,
model switch and the user's instruction.  The router:

- **outputs a decision with a reason, and never modifies long-term memory.**
  It reads projections (saturation, pending approvals, task state) and emits a
  :class:`RouteDecision`; it does not fork, compact or rewrite anything
  itself -- execution is the caller's job (e.g. the AgentLoop or the user).
- **is deterministic and explainable.**  Every decision carries the signals
  that produced it, so it can be replayed and audited, and the routing rules
  can become explainable over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import HarnessStore

VALID_ROUTES = {"continue", "fork", "rebirth", "switch_recipe", "spawn_subagent"}


@dataclass(frozen=True)
class RouteDecision:
    """The router's output: a route plus the reason and evidence behind it."""

    route: str
    reason: str
    signals: dict[str, Any]
    suggested_recipe: str | None = None

    def __post_init__(self) -> None:
        if self.route not in VALID_ROUTES:
            raise ValueError(f"invalid route: {self.route}")
        if not self.reason.strip():
            raise ValueError("a route decision must carry a reason")

    def describe(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "signals": self.signals,
            "suggested_recipe": self.suggested_recipe,
        }


@dataclass
class Router:
    """Decide how a session's context should proceed, with explicit reasons."""

    store: HarnessStore
    # Saturation is measured in work events in the current session.  Above
    # ``saturate_at`` the router recommends a rebirth (compact + new context);
    # above ``fork_at`` it recommends a fork.  These are heuristics a user can
    # override, recorded with every decision.
    saturate_at: int = 40
    fork_at: int = 80

    def decide(
        self,
        *,
        session_id: str,
        task_type: str | None = None,
        user_instruction: str | None = None,
        record: bool = True,
    ) -> RouteDecision:
        """Decide the route for a session and record it (with its reason).

        The user's explicit instruction always wins; otherwise the decision is
        driven by saturation and pending approvals.  The router reads
        projections only and never writes long-term memory.
        """

        signals = self._gather_signals(session_id)

        # 1. The user's explicit instruction is decisive.
        if user_instruction:
            route = self._route_from_instruction(user_instruction)
            decision = RouteDecision(
                route=route,
                # The reason is a fixed template; the raw instruction is data,
                # recorded separately below, never prose the ledger presents as
                # authoritative explanation.
                reason="explicit user instruction",
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        # 2. A pending approval blocks continuation: the session is waiting on
        #    a human, so forking/rebirthing now would lose that thread.
        elif signals["pending_approvals"] > 0:
            decision = RouteDecision(
                route="continue",
                reason="an approval is pending; the session is waiting on a human decision",
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        # 3. Saturation drives fork / rebirth.
        elif signals["work_events"] >= self.fork_at:
            decision = RouteDecision(
                route="fork",
                reason=(
                    f"context is saturated ({signals['work_events']} work events "
                    f">= fork threshold {self.fork_at}); fork to a child agent "
                    "carrying the current state"
                ),
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        elif signals["work_events"] >= self.saturate_at:
            decision = RouteDecision(
                route="rebirth",
                reason=(
                    f"context is filling up ({signals['work_events']} work events "
                    f">= saturation threshold {self.saturate_at}); assemble a handoff "
                    "package and be reborn into a fresh context"
                ),
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )
        else:
            decision = RouteDecision(
                route="continue",
                reason=f"context has room ({signals['work_events']} work events); continue",
                signals=signals,
                suggested_recipe=self._suggest_recipe(task_type),
            )

        if record:
            self.store.append_event(
                session_id,
                "route.selected",
                {
                    "route": decision.route,
                    "reason": decision.reason,
                    "task_type": task_type,
                    "user_instruction": user_instruction,
                    "signals": decision.signals,
                    "suggested_recipe": decision.suggested_recipe,
                    "thresholds": {"saturate_at": self.saturate_at, "fork_at": self.fork_at},
                },
            )
        return decision

    # ------------------------------------------------------------------
    # signals (read-only projections)
    # ------------------------------------------------------------------
    def _gather_signals(self, session_id: str) -> dict[str, Any]:
        return {
            "work_events": self._work_events_since_boundary(session_id),
            "pending_approvals": self._open_approvals(session_id),
            "saturate_at": self.saturate_at,
            "fork_at": self.fork_at,
        }

    def _work_events_since_boundary(self, session_id: str) -> int:
        """Count work events since the last rebirth/handoff boundary.

        Saturation must measure the *current* context, not the session's whole
        history -- otherwise a rebirth could never relieve the router (the
        count would never reset).  The anchor is the most recent
        ``context.assembled`` event (a handoff/rebirth marker); events after it
        are the live context.  Counted via a targeted read-only query, not a
        capped newest-first window that would truncate exactly the events that
        matter in a long session.
        """

        anchor = self.store.query_one(
            "SELECT seq FROM session_events WHERE session_id = ? "
            "AND event_type = 'context.assembled' ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        anchor_seq = anchor["seq"] if anchor else 0
        row = self.store.query_one(
            "SELECT COUNT(*) AS n FROM session_events WHERE session_id = ? AND seq > ? "
            "AND event_type NOT LIKE 'memory.%' AND event_type != 'context.assembled' "
            "AND event_type NOT LIKE 'loop.%' AND event_type NOT LIKE 'tool.%' "
            "AND event_type NOT LIKE 'plugin.%' AND event_type NOT LIKE 'taste.%' "
            "AND event_type != 'workspace.snapshot'",
            (session_id, anchor_seq),
        )
        return int(row["n"])

    def _open_approvals(self, session_id: str) -> int:
        """Count approval requests that are still genuinely open.

        A request is open until a grant with a *matching* arguments_hash
        resolves it -- matching by call, not by a global count, so a retry
        storm or an unrelated grant cannot miscount it.  Computed with a
        targeted read-only query (no truncation window).
        """

        requests = self.store.query(
            "SELECT payload_json FROM session_events WHERE session_id = ? "
            "AND event_type = 'tool.requested'",
            (session_id,),
        )
        granted_hashes = {
            row["arguments_hash"]
            for row in self.store.query(
                "SELECT json_extract(payload_json, '$.arguments_hash') AS arguments_hash "
                "FROM session_events WHERE session_id = ? "
                "AND event_type = 'tool.approval_granted'",
                (session_id,),
            )
            if row["arguments_hash"]
        }
        open_count = 0
        for row in requests:
            import json as _json

            payload = _json.loads(row["payload_json"])
            # Only gated requests (those that needed approval) can be "open".
            if not payload.get("gated"):
                continue
            if payload.get("arguments_hash") not in granted_hashes:
                open_count += 1
        return open_count

    @staticmethod
    def _route_from_instruction(instruction: str) -> str:
        """Map an explicit instruction word to a route, conservatively."""

        text = instruction.strip().lower()
        if text in {"fork", "派生", "分支"}:
            return "fork"
        if text in {"rebirth", "重生", "compact", "压缩"}:
            return "rebirth"
        if text in {"switch", "switch_recipe", "切换配方", "换模型"}:
            return "switch_recipe"
        if text in {"subagent", "spawn", "子agent", "子代理"}:
            return "spawn_subagent"
        # An unrecognised instruction means "keep going" -- never guess a
        # destructive route from ambiguous input.
        return "continue"

    @staticmethod
    def _suggest_recipe(task_type: str | None) -> str | None:
        if task_type is None:
            return None
        try:
            from .recipes import resolve_recipe

            return resolve_recipe(task_type).id
        except ValueError:
            return None
