"""Agent loop: an explicit, recoverable state machine that only drives.

The loop is deliberately thin.  It does **not** route models, write memory or
judge permissions -- those belong to the services it already has
(``assemble_context_package``, ``resolve_recipe``, ``ToolRegistry``).  It only
drives a host-provided :class:`SessionDriver` through a fixed state machine and
records every transition as a session event, so a run can be replayed and
recovered from the event stream alone.

State machine (from docs/runtime-architecture.md)::

    IDLE
      -> ASSEMBLING_CONTEXT
      -> SELECTING_MODEL
      -> CALLING_MODEL
      -> WAITING_TOOL
      -> APPLYING_RESULT
      -> CHECKING_STOP
      -> COMPLETED / FAILED / CANCELLED

Stop conditions are explicit: task complete, user pause, round limit, budget
limit, unrecoverable error, waiting for approval.  Because no real model is
called in the prototype, ``CALLING_MODEL`` delegates to the injected driver;
a production driver would invoke a Model Adapter behind the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .store import HarnessStore

# Explicit states.  Terminal states: COMPLETED / FAILED / CANCELLED.
STATES = {
    "IDLE",
    "ASSEMBLING_CONTEXT",
    "SELECTING_MODEL",
    "CALLING_MODEL",
    "WAITING_TOOL",
    "APPLYING_RESULT",
    "CHECKING_STOP",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}

# Explicit stop reasons.
# Sentinel returned when a gated tool needs approval before it can run.
_WAITING_APPROVAL = object()

STOP_REASONS = {
    "task_complete",
    "user_pause",
    "round_limit",
    "budget_limit",
    "unrecoverable_error",
    "waiting_approval",
}

# The legal transitions.  Anything else is a bug in the driver, not a choice.
_TRANSITIONS: dict[str, set[str]] = {
    "IDLE": {"ASSEMBLING_CONTEXT", "CANCELLED"},
    "ASSEMBLING_CONTEXT": {"SELECTING_MODEL", "FAILED", "CANCELLED"},
    "SELECTING_MODEL": {"CALLING_MODEL", "FAILED", "CANCELLED"},
    "CALLING_MODEL": {"WAITING_TOOL", "APPLYING_RESULT", "FAILED", "CANCELLED"},
    "WAITING_TOOL": {"APPLYING_RESULT", "FAILED", "CANCELLED"},
    "APPLYING_RESULT": {"CHECKING_STOP", "FAILED", "CANCELLED"},
    "CHECKING_STOP": {"CALLING_MODEL", "COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    # CANCELLED -> ASSEMBLING_CONTEXT is reachable ONLY via resume(): a run
    # that stopped on waiting_approval re-enters the machine at the assembly
    # stage once approval is granted.  It is declared here explicitly so the
    # legal-transition table stays the single source of truth; a plain
    # transition into CANCELLED still cannot be followed by anything else.
    "CANCELLED": {"ASSEMBLING_CONTEXT"},
}


class AgentLoopError(Exception):
    """Raised on illegal transitions or driver contract violations."""


@dataclass(frozen=True)
class LoopResult:
    """The outcome of a single driver turn.

    The driver reports what happened; the loop decides the next state.  A
    driver may request a tool call (``tool_call``), signal completion
    (``task_complete``), or ask to stop for a budget/round reason.
    """

    output: Any = None
    tool_call: dict[str, Any] | None = None
    # Parallel tool calls (the model requested several tools in one turn).
    # ``tool_call`` (single) and ``tool_calls`` (plural) are mutually exclusive.
    tool_calls: list[dict[str, Any]] | None = None
    task_complete: bool = False
    stop_reason: str | None = None


class SessionDriver(Protocol):
    """The host-provided behaviour the loop drives.

    Implementations must be deterministic given the same context for the
    prototype to stay reproducible.  ``act`` receives the assembled context
    package (and any tool result from the previous turn) and returns a
    :class:`LoopResult`.
    """

    def act(self, context: dict[str, Any], last_tool_result: Any = None) -> LoopResult:
        ...


@dataclass
class AgentLoop:
    """Drive a session through the state machine, logging every transition."""

    store: HarnessStore
    session_id: str
    driver: SessionDriver
    max_rounds: int = 10
    budget_rounds: int | None = None
    # Optional tool registry.  When present, every driver tool_call is routed
    # through the approval gate; when absent, the loop is a pure state-machine
    # driver with no execution (tool_calls become a contract violation).
    tool_registry: Any = None
    _state: str = field(default="IDLE", init=False)
    _rounds: int = field(default=0, init=False)
    # Resume scratch state: set by resume() before re-entering the machine,
    # consumed by _drive() at the assembly stage.
    _resume_context: dict[str, Any] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id cannot be empty")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if self.budget_rounds is not None and self.budget_rounds < 1:
            raise ValueError("budget_rounds must be at least 1 when set")

    @property
    def state(self) -> str:
        return self._state

    @property
    def rounds(self) -> int:
        return self._rounds

    def _transition(self, to_state: str, detail: dict[str, Any] | None = None) -> None:
        if to_state not in STATES:
            raise AgentLoopError(f"unknown state: {to_state}")
        allowed = _TRANSITIONS.get(self._state, set())
        if to_state not in allowed:
            raise AgentLoopError(f"illegal transition {self._state} -> {to_state}")
        payload = {"from": self._state, "to": to_state, "round": self._rounds}
        if detail:
            payload.update(detail)
        self.store.append_event(self.session_id, "loop.transition", payload)
        self._state = to_state

    def _force_fail(self, error: str) -> None:
        """Record a real transition to FAILED from any non-terminal state.

        The normal machine only reaches FAILED from specific states, but a
        fatal error can occur anywhere.  Rather than bypass the event stream
        (which would make reconstruct() resurrect a dead run as mid-flight), a
        forced transition is logged with ``forced: true`` so recovery sees the
        terminal state and an auditor sees that the machine was aborted, not
        driven, into FAILED.
        """

        self.store.append_event(
            self.session_id,
            "loop.transition",
            {
                "from": self._state,
                "to": "FAILED",
                "round": self._rounds,
                "forced": True,
                "error": error,
            },
        )
        self._state = "FAILED"

    def run(self, task: str, *, task_type: str | None = None) -> dict[str, Any]:
        """Drive the loop to a terminal state and return a run summary."""

        if self._state != "IDLE":
            raise AgentLoopError("run() may only be called from IDLE")
        # If the driver records model.* events (e.g. an AdapterDriver) but was
        # not given a store/session, inject the loop's own so every model call
        # is audited without the caller wiring it twice.
        if getattr(self.driver, "store", None) is None and hasattr(self.driver, "store"):
            self.driver.store = self.store
        if getattr(self.driver, "session_id", None) is None and hasattr(self.driver, "session_id"):
            self.driver.session_id = self.session_id
        # Record the stop-condition configuration so a reconstructed loop can
        # read it back from the stream rather than relying on process state.
        self.store.append_event(
            self.session_id,
            "loop.started",
            {
                "task": task,
                "task_type": task_type,
                "max_rounds": self.max_rounds,
                "budget_rounds": self.budget_rounds,
            },
        )
        return self._drive(task, task_type=task_type)

    def _drive(
        self,
        task: str,
        *,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Assemble context and drive the machine to a terminal state.

        Shared by run() (fresh run) and resume() (continuation of a
        waiting_approval run): the driver loop below is identical for both.
        A resume scratch (``_resume_context``) is consumed at the assembly
        stage, so a continuation round is recorded and driven exactly like a
        normal one.

        Defence in depth (NOT a security boundary): the private entry accepts
        only a fresh loop (IDLE, entered by run()) or a resume loop that was
        explicitly parked at CANCELLED with its scratch context set by
        resume().  This catches in-process callers that try to re-drive a
        finished/cancelled loop around the public gates; Python's privacy is
        by convention, so the real gate stays in resume().
        """

        if self._state != "IDLE" and not (
            self._state == "CANCELLED" and self._resume_context is not None
        ):
            raise AgentLoopError(
                f"_drive() requires a fresh (IDLE) loop or a resume()-prepared "
                f"(CANCELLED + resume context) loop, not state {self._state!r}"
            )

        context: dict[str, Any] = {}
        last_tool_result: Any = None
        try:
            self._transition("ASSEMBLING_CONTEXT")
            context = self.store.assemble_context_package(
                task, session_id=self.session_id, task_type=task_type
            )
            if self._resume_context is not None:
                # A resumed run carries the approval evidence of the paused
                # turn into the assembled package: the driver sees WHICH gated
                # call was waiting and its one-time token id, and decides how
                # to continue (typically: re-issue that exact call carrying
                # the token).  The token itself is never serialised -- only
                # its id, which is useless without the registry's live grants.
                context["resume"] = self._resume_context
                self._resume_context = None
            # Inject the model-visible tool contracts so the driver can tell
            # the model which tools exist (contracts only, never
            # implementations).  Without this the model could only hallucinate
            # tool names.
            if self.tool_registry is not None:
                context["visible_tools"] = self.tool_registry.visible_tools(
                    session_id=self.session_id
                )
            self._transition("SELECTING_MODEL")
            # Model selection is recorded by the package's recipe; the loop
            # does not choose a model itself.
            self._transition("CALLING_MODEL")

            while True:
                result = self.driver.act(context, last_tool_result)
                self._rounds += 1
                self._validate_result(result)

                if result.tool_call is not None or result.tool_calls is not None:
                    calls = (
                        [result.tool_call]
                        if result.tool_call is not None
                        else list(result.tool_calls)
                    )
                    # Pre-flight: scan the whole batch for any gated call that
                    # lacks a valid token BEFORE executing anything.  A gated
                    # call stops the turn here -- true "no partial execution":
                    # no earlier call runs and no side effect is recorded.
                    pending = self._first_gated_call(calls)
                    if pending is not None:
                        # Record the approval requirement so the ledger shows
                        # WHICH gated call is waiting (the same event type an
                        # in-execution gate would produce, for a coherent audit).
                        self.store.append_event(
                            self.session_id,
                            "tool.approval_required",
                            {
                                "name": pending.get("name"),
                                "reason": "pre-flight: gated tool call lacks a valid approval token",
                            },
                        )
                        # The pending call is identified by name + index +
                        # arguments *hash* (never the raw model-controlled
                        # arguments) so resume() can verify a presented token
                        # is bound to THIS exact call before re-entering.
                        from .tools import _safe_arguments_hash

                        pending_hash = _safe_arguments_hash(pending.get("arguments", {}))
                        pending_detail = {
                            "reason": "waiting_approval",
                            "pending_tool": pending.get("name"),
                            "pending_index": calls.index(pending),
                            "pending_arguments_hash": pending_hash,
                        }
                        self._transition("CANCELLED", pending_detail)
                        return self._summary(
                            "waiting_approval",
                            context,
                            output={
                                "pending_tool": pending.get("name"),
                                "pending_index": calls.index(pending),
                                "pending_arguments_hash": pending_hash,
                            },
                        )
                    self._transition("WAITING_TOOL", {"tools": [c.get("name") for c in calls]})
                    results = []
                    approval_needed = False
                    for call in calls:
                        tool_result = self._execute_tool_call(call)
                        if tool_result is _WAITING_APPROVAL:
                            # A token became invalid mid-batch (e.g. consumed by
                            # an earlier parallel call): stop conservatively.
                            approval_needed = True
                            break
                        results.append(tool_result)
                    if approval_needed:
                        self._transition("CANCELLED", {"reason": "waiting_approval"})
                        return self._summary("waiting_approval", context)
                    # Each result is correlated back to its call by id, so the
                    # driver can build the right tool_result/tool message.  A
                    # parallel batch is wrapped in an explicit marker so a tool
                    # that legitimately RETURNS a list is never mistaken for
                    # parallel results.
                    if result.tool_call is not None:
                        last_tool_result = results[0]
                    else:
                        last_tool_result = {"_parallel": results}
                    self._transition("APPLYING_RESULT")
                else:
                    self._transition("APPLYING_RESULT")

                self._transition("CHECKING_STOP")
                stop = self._check_stop(result)
                if stop is not None:
                    state, reason = stop
                    self._transition(state, {"reason": reason})
                    return self._summary(reason, context, output=result.output)
                # Otherwise loop back for another model turn.
                self._transition("CALLING_MODEL")
        except Exception as exc:  # noqa: BLE001 - normalised into FAILED
            # Both unexpected exceptions and driver-contract violations
            # (AgentLoopError) become a FAILED run -- never a raised brick that
            # leaves the machine stuck in a non-terminal state.  The failure is
            # recorded as a *real* loop.transition so the event stream remains
            # the source of truth and reconstruct() sees the terminal state.
            error_text = str(exc)
            # Preserve structured error classification (e.g. ModelAdapterError's
            # error_class/retryable/vendor_ref) so the ledger keeps the
            # actionable detail and the raw vendor reference, not just a string.
            error_detail: dict[str, Any] = {}
            error_class = getattr(exc, "error_class", None)
            if error_class is not None:
                error_detail["error_class"] = error_class
                error_detail["retryable"] = getattr(exc, "retryable", False)
                error_detail["vendor_ref"] = getattr(exc, "vendor_ref", None)
            if self._state not in TERMINAL_STATES:
                self.store.append_event(
                    self.session_id,
                    "loop.error",
                    {
                        "state": self._state,
                        "error": error_text,
                        "round": self._rounds,
                        **error_detail,
                    },
                )
                self._force_fail(error_text)
            summary = self._summary("unrecoverable_error", context, error=error_text)
            summary.update(error_detail)
            return summary

    # ------------------------------------------------------------------
    # resume: continue a run that paused on waiting_approval
    # ------------------------------------------------------------------
    @classmethod
    def resume(
        cls,
        store: HarnessStore,
        session_id: str,
        driver: SessionDriver,
        *,
        approval_token: Any,
        tool_registry: Any = None,
        actor_id: str = "system",
    ) -> dict[str, Any]:
        """Resume a run whose last terminal state is waiting_approval.

        Recovery is a READ-ONLY rebuild of the paused run's truth from the
        event stream (task, limits, round count, pending tool call), followed
        by re-entering the state machine with rounds inherited.  It is not a
        new run: no new ``loop.started`` is written, the round counter
        continues where the current run's stream left off, and ``max_rounds``
        keeps binding across the pause -- resume can never be a backdoor
        around the budget.

        Gate rules (all violations raise :class:`AgentLoopError` loudly, no
        events written):

        - only a run whose most recent ``loop.finished`` has
          ``stop_reason == "waiting_approval"`` may resume;
        - one resume PER PAUSE (seq-scoped): a ``loop.resumed`` event vetoes
          resuming the pause it belongs to (i.e. the pause whose
          ``loop.finished`` precedes it); a LATER pause (a newer
          waiting_approval finish) gets its own fresh resume;
        - the presented approval token must be a LIVE token bound to the
          pending call (tool name + exact-arguments hash, when the stream
          recorded it) -- checked against the registry without consuming it,
          so a wrong or consumed token fails before any transition is
          recorded.

        Gates 1 and 2 plus the ``loop.resumed`` ledger write run inside a
        single ``BEGIN IMMEDIATE`` transaction (the store's standard write
        primitive), so two concurrent actors cannot both pass the check:
        the second blocks on the write lock, then re-reads the ledger and
        sees the first actor's ``loop.resumed``.  Gate 3 (token lookup) is
        intentionally OUTSIDE that transaction: the registry's live grants
        are process-local, and the token's one-time consumption is arbitrated
        by the registry at execution time.

        Round inheritance is scoped to the CURRENT run: only transitions
        recorded after the most recent ``loop.started`` count.  Earlier runs
        in the same session never spend this run's budget.

        Trust boundary: the event stream has no author concept, so resume()
        trusts the recorded ``loop.finished`` / ``loop.started`` contents.
        An actor able to write this session's event stream is already
        trusted; a forged finish can unlock the resume gate but CANNOT mint
        an approval token or defeat the registry's execution-time
        name/arguments/session binding -- the physical approval gate is gate
        3 plus execution-time verification, not the event predicates.

        Token semantics are unchanged: the token is still one-time,
        argument-bound and session-bound, and is consumed only when the
        resumed run actually executes the gated call through the registry.
        """

        if not actor_id.strip():
            raise ValueError("actor_id cannot be empty")

        import json as _json

        # Gates 1+2 and the resume bookkeeping share ONE BEGIN IMMEDIATE
        # transaction: the check (SELECT) and the claim (INSERT loop.resumed)
        # are atomic, closing the check-then-act window that allowed two
        # racing actors to both resume the same pause.  The write lock is
        # held for microseconds -- the actual driving happens after COMMIT.
        with store.transaction() as connection:
            def read_one(sql, args):
                # Identical read queries as store.query_one, but on the
                # transaction's connection so they see (and serialise with)
                # the claim write below.
                return connection.execute(sql, tuple(args)).fetchone()

            # --- gate 1: the most recent finish must be a waiting_approval pause.
            finished = read_one(
                "SELECT payload_json, seq FROM session_events "
                "WHERE session_id = ? AND event_type = 'loop.finished' "
                "ORDER BY seq DESC LIMIT 1",
                (session_id,),
            )
            if finished is None:
                raise AgentLoopError(
                    f"cannot resume session '{session_id}': no finished run in the event stream"
                )

            finished_payload = _json.loads(finished["payload_json"])
            stop_reason = finished_payload.get("stop_reason")
            if stop_reason != "waiting_approval":
                raise AgentLoopError(
                    f"cannot resume session '{session_id}': last run ended with "
                    f"stop_reason={stop_reason!r}; only a waiting_approval run may resume"
                )

            # --- gate 2: one resume per pause (seq-scoped, like
            # _cancellation_requested): a loop.resumed vetoes only the pause
            # it answered.  A pause whose finish seq is NEWER than every
            # loop.resumed has never been resumed and may resume once.
            resumed = read_one(
                "SELECT seq FROM session_events "
                "WHERE session_id = ? AND event_type = 'loop.resumed' "
                "AND seq > ? ORDER BY seq DESC LIMIT 1",
                (session_id, finished["seq"]),
            )
            if resumed is not None:
                raise AgentLoopError(
                    f"cannot resume session '{session_id}': this pause was already resumed "
                    "(loop.resumed is in the ledger); refusing to double-run it"
                )

            # --- read-only rebuild of the run's truth from the stream.
            started = read_one(
                "SELECT payload_json, seq FROM session_events "
                "WHERE session_id = ? AND event_type = 'loop.started' "
                "ORDER BY seq DESC LIMIT 1",
                (session_id,),
            )
            if started is None:
                raise AgentLoopError(
                    f"cannot resume session '{session_id}': loop.started is missing from the stream"
                )
            started_payload = _json.loads(started["payload_json"])
            task = started_payload.get("task")
            task_type = started_payload.get("task_type")
            max_rounds = started_payload.get("max_rounds", 10)
            budget_rounds = started_payload.get("budget_rounds")
            # Rounds are derived from the recorded transitions of THIS run
            # (seq > latest loop.started) -- never from process memory, and
            # never charged against earlier runs in the same session.
            rounds_row = read_one(
                "SELECT MAX(CAST(json_extract(payload_json, '$.round') AS INTEGER)) AS rounds "
                "FROM session_events WHERE session_id = ? AND event_type = 'loop.transition' "
                "AND seq > ?",
                (session_id, started["seq"]),
            )
            inherited_rounds = int(rounds_row["rounds"]) if rounds_row and rounds_row["rounds"] is not None else 0

            # The pending tool call is identified by the recorded finish summary.
            output = finished_payload.get("output") or {}
            pending_tool = output.get("pending_tool")
            pending_index = output.get("pending_index", 0)
            pending_arguments_hash = output.get("pending_arguments_hash")

            # --- gate 3: the token must be live and bound to the pending tool.
            # Checked WITHOUT consuming it: consumption happens only when the
            # resumed run re-issues the call through the registry.
            token_id = getattr(approval_token, "id", approval_token)
            if tool_registry is None:
                raise AgentLoopError(
                    "cannot resume: a tool_registry is required to verify the approval token"
                )
            live = tool_registry.get_live_token(token_id) if isinstance(token_id, str) else None
            if (
                live is None
                or live.tool_name != pending_tool
                # A grant is bound to exact arguments: when the stream recorded the
                # pending call's hash, the token must match it -- a token minted
                # for a different call of the same tool is not a grant for this
                # pause.  (Older streams without the hash fall back to the
                # name-only check; execution-time verification still applies.)
                or (pending_arguments_hash is not None
                    and live.arguments_hash != pending_arguments_hash)
                # A grant is also SESSION-bound: a live token minted for
                # session s1 must not unlock the resume gate of s2's pause
                # (same tool + identical arguments hash would otherwise match).
                # The registry already enforces this at execution time, but the
                # gate must not burn s2's one-resume-per-pause on a grant that
                # was never issued for s2.
                or getattr(live, "session_id", None) != session_id
            ):
                raise AgentLoopError(
                    f"cannot resume session '{session_id}': approval token is not a live "
                    f"grant bound to this session's pending call {pending_tool!r}"
                )

            # The resume itself is an append-only, replayable ledger fact: who
            # resumed, which grant authorised it, from which pause, at which
            # round.  Recorded INSIDE the gate transaction so the claim is
            # committed before any transition: a crash mid-resume leaves an
            # honest trail (and vetoes a retry of the same pause via gate 2).
            store.record_event(
                connection,
                session_id,
                "loop.resumed",
                {
                    "actor_id": actor_id,
                    "resumed_from": "waiting_approval",
                    "pending_tool": pending_tool,
                    "pending_index": pending_index,
                    "approval_token_id": token_id,
                    "inherited_rounds": inherited_rounds,
                },
            )

        loop = cls(
            store=store,
            session_id=session_id,
            driver=driver,
            max_rounds=max_rounds,
            budget_rounds=budget_rounds,
            tool_registry=tool_registry,
        )
        loop._state = "CANCELLED"  # re-entering the machine from the pause
        loop._rounds = inherited_rounds
        loop._resume_context = {
            "pending_tool": pending_tool,
            "pending_index": pending_index,
            "approval_token_id": token_id,
        }
        return loop._drive(task, task_type=task_type)

    def cancel(self, reason: str = "user_cancelled") -> None:
        """Request cancellation of this loop, as an append-only ledger event.

        Cancellation is *event-driven*, not a process-internal flag: the
        request is recorded as ``loop.cancel_requested`` so any actor (a person
        via the CLI, another agent, or the loop's own driver) can cancel a
        running loop from outside its thread/process, and the request is
        replayable -- a reconstructed loop can see that cancellation was
        requested.  The loop honours the request cooperatively at the next
        round boundary (_check_stop); a synchronous loop cannot preempt a
        blocked driver mid-call, but it will not start another round.
        """

        if not reason.strip():
            raise ValueError("cancel reason cannot be empty")
        self.store.append_event(
            self.session_id,
            "loop.cancel_requested",
            {"reason": reason},
        )

    def _cancellation_requested(self) -> str | None:
        """Return the reason of a live cancellation request for the current run.

        The rule is precise and cheap: a cancel is live iff its ``seq`` is newer
        than the most recent ``loop.started`` in this session.  This scopes a
        request to the run it belongs to -- a cancel from an earlier run (or a
        stray one with no running loop) does not poison a later run, and a
        finished run's cancels are correctly seen as belonging to that finished
        run, not the next one.  One indexed query, O(1), no per-round O(history)
        scans.
        """

        # A cancel is live iff it is newer than the most recent FINISHED run in
        # this session: it then belongs to the current (or next) run.  A cancel
        # older than the last finish belongs to that finished run and is dead.
        # This is one indexed MAX(seq) query per condition, O(1) per round, no
        # O(history) scans -- and unlike "any finished vetoes all cancels", a
        # cancel written after a finished run correctly cancels the NEXT run.
        finished = self.store.query_one(
            "SELECT MAX(seq) AS seq FROM session_events "
            "WHERE session_id = ? AND event_type = 'loop.finished'",
            (self.session_id,),
        )
        finished_seq = finished["seq"] if finished and finished["seq"] is not None else -1
        cancel = self.store.query_one(
            "SELECT payload_json, seq FROM session_events "
            "WHERE session_id = ? AND event_type = 'loop.cancel_requested' "
            "AND seq > ? ORDER BY seq DESC LIMIT 1",
            (self.session_id, finished_seq),
        )
        if cancel is None:
            return None
        import json as _json

        return _json.loads(cancel["payload_json"]).get("reason", "user_cancelled")

    def _check_stop(self, result: LoopResult) -> tuple[str, str] | None:
        # A cancellation request (event-driven, from any actor) is honoured
        # first at the round boundary: the loop stops cleanly instead of
        # starting another round.
        cancel_reason = self._cancellation_requested()
        if cancel_reason is not None:
            return ("CANCELLED", cancel_reason)
        if result.stop_reason is not None:
            if result.stop_reason not in STOP_REASONS:
                raise AgentLoopError(f"unknown stop reason: {result.stop_reason}")
            if result.stop_reason == "task_complete" or result.task_complete:
                return ("COMPLETED", "task_complete")
            if result.stop_reason == "waiting_approval":
                return ("CANCELLED", "waiting_approval")
            if result.stop_reason == "user_pause":
                return ("CANCELLED", "user_pause")
            return ("FAILED", result.stop_reason)
        if result.task_complete:
            return ("COMPLETED", "task_complete")
        if self._rounds >= self.max_rounds:
            return ("FAILED", "round_limit")
        if self.budget_rounds is not None and self._rounds >= self.budget_rounds:
            return ("FAILED", "budget_limit")
        return None

    def _first_gated_call(self, calls: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return the first gated call lacking a valid token, without executing.

        Used by the pre-flight scan so a gated call stops the turn before any
        tool runs.  Returns None when there is no registry (no gate) or when
        every gated call has a valid token.
        """

        if self.tool_registry is None:
            return None
        for call in calls:
            token = call.get("approval_token")
            if self.tool_registry.requires_approval_for(call.get("name"), token):
                return call
        return None

    def _execute_tool_call(self, tool_call: dict[str, Any]) -> Any:
        """Route a driver's tool call through the approval gate.

        The loop never trusts a result the driver cooked up itself: a tool
        call is executed via the injected :class:`ToolRegistry`, so validation,
        approval and ledger logging all apply.  Without a registry the loop
        cannot execute tools at all -- a tool_call is then a contract
        violation, surfaced as a FAILED run rather than silently trusted.
        """

        if self.tool_registry is None:
            raise AgentLoopError(
                "driver requested a tool call but no tool_registry is configured; "
                "the loop will not execute unverified tools"
            )
        from .tools import ToolApprovalRequired

        name = tool_call.get("name")
        arguments = tool_call.get("arguments", {})
        approval_token = tool_call.get("approval_token")
        try:
            result = self.tool_registry.request(
                name,
                arguments,
                session_id=self.session_id,
                approval_token=approval_token,
            )
        except ToolApprovalRequired:
            return _WAITING_APPROVAL
        return result.get("output")

    @staticmethod
    def _validate_result(result: LoopResult) -> None:
        """Reject contradictory driver reports instead of silently picking one.

        A driver that reports a tool call *and* a terminal intent, or a stop
        reason that disagrees with ``task_complete``, has a bug; the loop must
        fail loudly (normalised to FAILED by run()) rather than resolve the
        conflict by accidental branch order.
        """

        if result.tool_call is not None and result.tool_calls is not None:
            raise AgentLoopError(
                "contradictory LoopResult: tool_call and tool_calls are mutually exclusive"
            )
        if (result.tool_call is not None or result.tool_calls is not None) and (
            result.task_complete or result.stop_reason is not None
        ):
            raise AgentLoopError(
                "contradictory LoopResult: tool call(s) cannot coexist with task_complete/stop_reason"
            )
        if result.tool_calls is not None and not result.tool_calls:
            raise AgentLoopError("tool_calls cannot be an empty list")
        if result.task_complete and result.stop_reason not in (None, "task_complete"):
            raise AgentLoopError(
                f"contradictory LoopResult: task_complete with stop_reason={result.stop_reason}"
            )
        if result.stop_reason is not None and result.stop_reason not in STOP_REASONS:
            raise AgentLoopError(f"unknown stop reason: {result.stop_reason}")

    def _summary(
        self,
        reason: str,
        context: dict[str, Any],
        error: str | None = None,
        output: Any = None,
    ) -> dict[str, Any]:
        summary = {
            "session_id": self.session_id,
            "final_state": self._state,
            "stop_reason": reason,
            "rounds": self._rounds,
            "package_id": context.get("package_id"),
        }
        if output is not None:
            summary["output"] = output
        if error is not None:
            summary["error"] = error
        self.store.append_event(self.session_id, "loop.finished", summary)
        return summary

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    @classmethod
    def reconstruct(
        cls,
        store: HarnessStore,
        session_id: str,
        driver: SessionDriver,
        **kwargs: Any,
    ) -> "AgentLoop":
        """Rebuild loop state from the event stream, not process memory.

        The current state and round count are derived purely from the recorded
        ``loop.transition`` events, so a recovered loop continues exactly where
        the event stream left off.
        """

        # Read back the recorded configuration (loop.started) unless the caller
        # overrides it, so a recovered loop keeps its stop-condition limits
        # without relying on process memory.
        started = next(
            (
                e for e in store.list_events(session_id=session_id, limit=1000)
                if e.event_type == "loop.started"
            ),
            None,
        )
        if started is not None:
            kwargs.setdefault("max_rounds", started.payload.get("max_rounds", 10))
            kwargs.setdefault("budget_rounds", started.payload.get("budget_rounds"))
        loop = cls(store=store, session_id=session_id, driver=driver, **kwargs)

        # Only the latest transition and the max round are needed.  list_events
        # returns newest-first, so the first transition is the current state and
        # the running max over the replayed window gives the round count.  A
        # generous limit keeps this correct for very long sessions without
        # silently truncating (loop sessions are bounded by max_rounds anyway).
        state = "IDLE"
        rounds = 0
        seen_transition = False
        for event in store.list_events(session_id=session_id, limit=100000):
            if event.event_type != "loop.transition":
                continue
            if not seen_transition:
                state = event.payload.get("to", state)  # newest transition first
                seen_transition = True
            rounds = max(rounds, int(event.payload.get("round", 0)))
        loop._state = state
        loop._rounds = rounds
        return loop
