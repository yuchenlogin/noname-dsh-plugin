"""Tool registry: the model-visible surface is separated from host execution.

A tool has two faces that never mix:

- the **model-visible surface** (name, description, input schema) -- what a
  model is allowed to see and request;
- the **host execution surface** (the callable, permission level, approval
  policy, scope) -- what the host will actually do, and under what guard.

The pipeline is::

    validate -> approval -> execute -> log -> return

**Approval is a physical gate backed by the ledger, not a caller-supplied
boolean.**  A gated tool cannot execute until a one-time approval token is
presented.  Tokens are minted by :meth:`ToolRegistry.grant_approval` -- the
*approver's* path, which writes ``tool.approval_granted`` -- and are bound to
``(tool name, canonical hash of arguments, approver)`` and single-use.  The
executor never marks its own homework: it verifies the token against the
in-memory grant set (itself derived from ledger events) and records
``tool.approved`` only as a *reference* to a prior grant.

Two further hard rules make the gate structural rather than advisory:

- **Monotonic shadowing**: a same-name registration may never weaken the
  permission or approval requirement of the tool it shadows, and a narrower
  scope may not be shadowed by a wider one.
- **No subclassing for registration**: only exact :class:`Tool` instances can
  be registered, so the approval gate cannot be overridden away.

This module is the contract and pipeline skeleton.  It executes only the
callables a host explicitly registers and provides no shell, network or file
side effects of its own (those belong to the Execution World layer, validated
separately).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .store import HarnessStore

VALID_TOOL_SCOPES = {"global", "session"}
PERMISSION_LEVELS = {"read", "write", "destructive"}
# Ordered weakest -> strongest so shadowing monotonicity can be enforced.
_PERMISSION_RANK = {"read": 0, "write": 1, "destructive": 2}
VALID_APPROVAL_POLICIES = {"never", "always"}
_SCOPE_RANK = {"global": 0, "session": 1}


class ToolError(Exception):
    """Base class for tool pipeline failures."""


class ToolValidationError(ToolError):
    """Input failed schema validation before approval/execution."""


class ToolApprovalRequired(ToolError):
    """The tool cannot execute until a valid approval token is presented."""


class ToolShadowingError(ToolError):
    """A registration tried to weaken or mis-scope an existing tool."""


@dataclass(frozen=True)
class ToolSchema:
    """The model-visible surface of a tool."""

    name: str
    description: str
    input_schema: dict[str, str]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name cannot be empty")
        if not isinstance(self.input_schema, dict):
            raise ValueError("input_schema must be a mapping of name -> type")


@dataclass(frozen=True)
class Tool:
    """A registered tool: model-visible surface plus host execution surface.

    ``approval`` is binary: ``never`` (read-only by contract, runs freely) or
    ``always`` (must be approved for every call).  ``permission`` describes the
    risk level; a destructive tool must always require approval.
    """

    schema: ToolSchema
    execute: Callable[[dict[str, Any]], Any]
    permission: str = "read"
    approval: str = "never"
    scope: str = "global"
    # Optional session binding for session-scoped tools.
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.permission not in PERMISSION_LEVELS:
            raise ValueError(f"invalid permission level: {self.permission}")
        if self.approval not in VALID_APPROVAL_POLICIES:
            raise ValueError(f"invalid approval policy: {self.approval}")
        if self.scope not in VALID_TOOL_SCOPES:
            raise ValueError(f"invalid tool scope: {self.scope}")
        if not callable(self.execute):
            raise ValueError("execute must be callable")
        if self.permission == "destructive" and self.approval != "always":
            raise ValueError("destructive tools must use approval='always'")
        if self.scope == "session" and not (self.session_id and self.session_id.strip()):
            raise ValueError("session-scoped tools must declare session_id")

    @property
    def requires_approval(self) -> bool:
        return self.approval == "always"

    def visible_surface(self) -> dict[str, Any]:
        """What a model is allowed to see.  Implementation is never exposed."""

        return {
            "name": self.schema.name,
            "description": self.schema.description,
            "input_schema": dict(self.schema.input_schema),
        }


def _canonical_arguments(arguments: dict[str, Any]) -> str:
    """A stable canonical form for binding approvals to exact arguments."""

    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_arguments(arguments).encode("utf-8")).hexdigest()


def _safe_arguments_hash(arguments: Any) -> str:
    """Hash arguments, converting unserialisable input into a validation error.

    The raw ``TypeError`` from ``json.dumps`` would otherwise escape before any
    ledger event is written, breaking the "every outcome is logged" invariant.
    """

    try:
        return _arguments_hash(arguments)
    except (TypeError, ValueError) as exc:
        raise ToolValidationError(f"tool arguments are not JSON-serialisable: {exc}") from exc


@dataclass(frozen=True)
class ApprovalToken:
    """A one-time, call-bound approval grant.  Verified, never self-asserted.

    The grant binds the tool, the exact arguments (by hash), the approver and
    the session it was granted in, so it cannot be replayed for a different
    call, a different tool, or a different session.
    """

    id: str
    tool_name: str
    arguments_hash: str
    approver_id: str
    session_id: str
    granted_at: str
    # The exact tool *generation* this grant authorises.  Re-registering or
    # shadowing a tool bumps its generation, so a token never outlives the
    # precise tool instance it was granted for.
    tool_generation: int = 0


@dataclass
class ToolRegistry:
    """A scoped registry with a ledger-backed approval gate."""

    store: HarnessStore
    _tools: dict[str, Tool] = field(default_factory=dict)
    # Live (unconsumed) approval tokens, keyed by token id.
    _grants: dict[str, ApprovalToken] = field(default_factory=dict)
    # Tombstones: the strongest (permission, approval) a name has ever carried.
    # Unregistering a tool must not let a later, weaker registration slip under
    # the gate -- monotonicity is enforced against the strongest-ever record,
    # not just the currently-registered tool.
    _strongest: dict[str, tuple[int, bool]] = field(default_factory=dict)
    # Monotonic generation per tool name; bumped on every registration.  A
    # grant binds the generation it was issued against, so unloading or
    # replacing a tool invalidates outstanding grants for the old instance.
    _generations: dict[str, int] = field(default_factory=dict)
    # Names explicitly unregistered in this process; re-registering one starts
    # a new instance (new generation).  A name with no live tool merely because
    # the process restarted keeps its generation, so a recovered tool table
    # does not burn outstanding grants.
    _explicitly_unregistered: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        # Rebuild grants, tombstones and generations from the ledger, so the
        # gate is reconstructed from durable evidence -- not process memory.
        #
        # Live grants: a token is *reserved* by tool.approved and *consumed for
        # good* only by a subsequent tool.completed that references it.  A
        # reservation followed by tool.failed releases the token (a transient
        # failure must not burn a human's approval) -- so a reserved-but-failed
        # token is live again after a restart, exactly as it was in memory.
        granted: dict[str, ApprovalToken] = {}
        # token_id -> "reserved" once tool.approved fires
        reserved: dict[str, dict[str, Any]] = {}
        consumed: set[str] = set()
        # list_events returns newest-first; the reservation/consumption pairing
        # must be replayed oldest-first so tool.approved is seen before the
        # tool.completed/tool.failed that resolves it.
        for event in reversed(self.store.list_events(session_id=None, limit=100000)):
            etype = event.event_type
            payload = event.payload if isinstance(event.payload, dict) else {}
            if etype == "tool.approval_granted":
                granted[payload["token_id"]] = ApprovalToken(
                    id=payload["token_id"],
                    tool_name=payload["name"],
                    arguments_hash=payload["arguments_hash"],
                    approver_id=payload["approver_id"],
                    session_id=payload.get("session_id", ""),
                    granted_at=event.occurred_at,
                    tool_generation=payload.get("tool_generation", 0),
                )
            elif etype == "tool.approved":
                token_id = payload.get("approval_token_id") or payload.get("token_id")
                if token_id:
                    reserved[token_id] = payload
            elif etype == "tool.completed":
                token_id = payload.get("approval_token_id")
                if token_id and token_id in reserved:
                    # Reserved and completed: the grant is consumed for good.
                    consumed.add(token_id)
                    reserved.pop(token_id, None)
            elif etype == "tool.failed":
                # A failure releases the reservation for any token reserved on
                # this tool in the same session whose call just failed.  We
                # match by tool name: a failure cannot reference the token id
                # directly (it is recorded before execution), but the most
                # recent reservation on that tool is the one being retried.
                name = payload.get("name")
                for token_id, reservation in list(reserved.items()):
                    if granted.get(token_id) and granted[token_id].tool_name == name:
                        reserved.pop(token_id, None)
            elif etype == "tool.registered":
                # Rebuild generation and tombstone monotonicity per tool name.
                name = payload.get("name")
                if name:
                    generation = int(payload.get("generation", 0))
                    self._generations[name] = max(self._generations.get(name, 0), generation)
                    rank = _PERMISSION_RANK.get(payload.get("permission", "read"), 0)
                    gated = payload.get("approval") == "always"
                    previous = self._strongest.get(name)
                    if previous is None or (rank, gated) > previous:
                        self._strongest[name] = (rank, gated)
        self._grants = {
            token_id: token
            for token_id, token in granted.items()
            if token_id not in consumed
        }

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register(self, tool: Tool, *, _restore: bool = False) -> dict[str, Any]:
        # The gate must not be overridable: only exact Tool instances register.
        if type(tool) is not Tool:
            raise ToolError("only exact Tool instances can be registered")
        name = tool.schema.name
        shadowed = self._tools.get(name)
        if shadowed is not None:
            self._check_shadowing(shadowed, tool)
        # Even with no live tool (e.g. after unregister), a registration may
        # not be weaker than the strongest gate this name has ever had.
        # ``_restore`` bypasses this: putting back a displaced tool is an undo,
        # not a fresh (potentially weakening) registration, and the transient
        # shadow may legitimately have ratcheted the tombstone above it.
        if not _restore:
            self._check_tombstone(name, tool)
        self._tools[name] = tool
        rank = _PERMISSION_RANK[tool.permission]
        gated = tool.requires_approval
        previous = self._strongest.get(name)
        if previous is None or (rank, gated) > previous:
            self._strongest[name] = (rank, gated)
        # Generation semantics: a grant binds the exact tool *instance* it was
        # issued for.  A new instance starts (generation bumps) when a live
        # tool is shadowed, or when a name is re-registered after an explicit
        # unload.  Merely re-registering a name with no live tool -- e.g. a
        # host re-registering its tools after a process restart -- keeps the
        # generation, so recovered tool tables do not invalidate live grants.
        if shadowed is not None or name in self._explicitly_unregistered:
            generation = self._generations.get(name, 0) + 1
        else:
            generation = max(self._generations.get(name, 0), 1)
        self._generations[name] = generation
        self._explicitly_unregistered.discard(name)
        result = {
            "registered": name,
            "scope": tool.scope,
            "generation": generation,
            "shadowed": None,
            # The displaced tool object itself, so a caller (e.g. the plugin
            # runtime) can restore it when rolling back a failed operation.
            "displaced": shadowed,
        }
        if shadowed is not None:
            result["shadowed"] = {"scope": shadowed.scope, "permission": shadowed.permission}
            self.store.append_event(
                "system",
                "tool.shadowed",
                {
                    "name": name,
                    "new_scope": tool.scope,
                    "new_permission": tool.permission,
                    "previous_scope": shadowed.scope,
                    "previous_permission": shadowed.permission,
                },
            )
        self.store.append_event(
            "system",
            "tool.registered",
            {
                "name": name,
                "scope": tool.scope,
                "permission": tool.permission,
                "approval": tool.approval,
                "session_id": tool.session_id,
                "generation": generation,
                "shadowed": result["shadowed"],
            },
        )
        return result

    @staticmethod
    def _check_shadowing(old: Tool, new: Tool) -> None:
        """A shadowing registration may never weaken the gate or mis-scope.

        - scope monotonicity: a wider scope may not shadow a narrower one;
        - permission monotonicity: the new tool's risk may not be lower;
        - approval monotonicity: the new tool may not drop a required approval.
        """

        if _SCOPE_RANK[new.scope] < _SCOPE_RANK[old.scope]:
            raise ToolShadowingError(
                f"a {new.scope}-scoped tool cannot shadow a {old.scope}-scoped tool"
            )
        if _PERMISSION_RANK[new.permission] < _PERMISSION_RANK[old.permission]:
            raise ToolShadowingError(
                f"cannot shadow {old.permission} tool with lower-risk {new.permission} tool"
            )
        if old.requires_approval and not new.requires_approval:
            raise ToolShadowingError(
                "cannot shadow an approval-gated tool with one that needs no approval"
            )

    def _check_tombstone(self, name: str, new: Tool) -> None:
        """A re-registration may not be weaker than this name's strongest gate."""

        strongest = self._strongest.get(name)
        if strongest is None:
            return
        rank, gated = strongest
        if _PERMISSION_RANK[new.permission] < rank:
            raise ToolShadowingError(
                f"cannot re-register '{name}' below its strongest-ever permission"
            )
        if gated and not new.requires_approval:
            raise ToolShadowingError(
                f"cannot re-register '{name}' without the approval it once required"
            )

    def unregister(self, name: str) -> bool:
        tool = self._tools.pop(name, None)
        if tool is None:
            return False
        # Mark the explicit unload: a later re-registration of this name is a
        # new instance, so outstanding grants for the old instance stay invalid.
        self._explicitly_unregistered.add(name)
        self.store.append_event(
            "system", "tool.unregistered", {"name": name, "scope": tool.scope}
        )
        return True

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def visible_tools(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """The model-visible surface a given session is allowed to see.

        Session-scoped tools are visible only to their own session; wider
        scopes are visible to all.  This is what makes scope real rather than
        decorative.
        """

        visible = []
        for tool in self._tools.values():
            if tool.scope == "session" and tool.session_id != session_id:
                continue
            visible.append(tool.visible_surface())
        return visible

    # ------------------------------------------------------------------
    # approval (the approver's path)
    # ------------------------------------------------------------------
    def grant_approval(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        approver_id: str,
        session_id: str,
    ) -> ApprovalToken:
        """Mint a one-time approval token for an exact call.  Writes the grant.

        This is the only path that may authorise a gated call.  The grant is
        recorded in the ledger by the approver, never by the executor.
        """

        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        if not approver_id.strip():
            raise ValueError("approver_id cannot be empty")
        token = ApprovalToken(
            id=self._new_token_id(arguments),
            tool_name=name,
            arguments_hash=_safe_arguments_hash(arguments),
            approver_id=approver_id,
            session_id=session_id,
            granted_at=self._now(),
            tool_generation=self._generations.get(name, 0),
        )
        # The ledger is the source of truth: record the grant *first*, and only
        # add the token to the live set once the write succeeded.  A failed
        # append must never leave a live token with no ledger trace.
        self.store.append_event(
            session_id,
            "tool.approval_granted",
            {
                "token_id": token.id,
                "name": name,
                "arguments_hash": token.arguments_hash,
                "approver_id": approver_id,
                "session_id": session_id,
                "tool_generation": token.tool_generation,
            },
        )
        self._grants[token.id] = token
        return token

    # ------------------------------------------------------------------
    # execution pipeline
    # ------------------------------------------------------------------
    def _validate_input(self, tool: Tool, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolValidationError("tool arguments must be an object")
        schema = tool.schema.input_schema
        unknown = set(arguments) - set(schema)
        if unknown:
            raise ToolValidationError(f"unexpected tool arguments: {sorted(unknown)}")
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for param, expected in schema.items():
            if param not in arguments:
                raise ToolValidationError(f"missing required argument: {param}")
            value = arguments[param]
            py_type = type_map.get(expected)
            if py_type is None:
                raise ToolValidationError(f"unknown schema type for {param}: {expected}")
            if expected in {"integer", "number"} and isinstance(value, bool):
                raise ToolValidationError(f"argument {param} must be {expected}, got boolean")
            if not isinstance(value, py_type):
                raise ToolValidationError(
                    f"argument {param} must be {expected}, got {type(value).__name__}"
                )

    def request(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        approval_token: ApprovalToken | None = None,
        actor_id: str = "model",
    ) -> dict[str, Any]:
        """Run the pipeline for a tool call.  Every outcome is logged.

        A gated tool executes only when presented a valid, unconsumed approval
        token bound to this exact call's arguments.  There is no boolean to
        self-assert; the token must have been minted by ``grant_approval``.
        """

        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        # Scope enforcement: a session-scoped tool runs only for its session.
        if tool.scope == "session" and tool.session_id != session_id:
            raise ToolError(f"tool '{name}' is not available in this session")

        # Record the request with an arguments *hash*, not the raw content: a
        # not-yet-approved gated call must not persist model-controlled content
        # into the append-only ledger.
        try:
            arguments_hash = _safe_arguments_hash(arguments)
        except ToolValidationError as exc:
            self.store.append_event(
                session_id,
                "tool.validation_failed",
                {"name": name, "error": str(exc), "actor_id": actor_id},
            )
            raise
        self.store.append_event(
            session_id,
            "tool.requested",
            {
                "name": name,
                "arguments_hash": arguments_hash,
                "actor_id": actor_id,
                "scope": tool.scope,
                "permission": tool.permission,
                "gated": tool.requires_approval,
            },
        )

        # validate
        try:
            self._validate_input(tool, arguments)
        except ToolValidationError as exc:
            self.store.append_event(
                session_id,
                "tool.validation_failed",
                {"name": name, "error": str(exc), "actor_id": actor_id},
            )
            raise

        # approval: verify a ledger-backed, call-bound, single-use token.
        if tool.requires_approval:
            token = self._verify_approval(tool, arguments, approval_token, session_id, actor_id)
        else:
            token = None

        # execute -> log
        import time

        started = time.monotonic()
        try:
            raw = tool.execute(arguments)
        except Exception as exc:  # noqa: BLE001 - every failure is logged
            # Return the reserved token so a failed attempt does not consume the
            # human's approval; the call may be retried with the same grant.
            if token is not None:
                self._grants[token.id] = token
            self.store.append_event(
                session_id,
                "tool.failed",
                {"name": name, "error": str(exc), "actor_id": actor_id},
            )
            raise ToolError(f"tool '{name}' failed: {exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        result = {"name": name, "output": raw}
        self.store.append_event(
            session_id,
            "tool.completed",
            {
                "name": name,
                "actor_id": actor_id,
                "elapsed_ms": elapsed_ms,
                "approval_token_id": token.id if token else None,
            },
        )
        return result

    def requires_approval_for(self, name: str, approval_token: ApprovalToken | None) -> bool:
        """Return whether a call would need approval WITHOUT executing it.

        This is a non-mutating pre-flight check used by the agent loop to scan
        a batch of tool calls before executing any of them, so a gated call
        stops the whole turn *before* any side effect happens (true "no partial
        execution").  A call needs approval iff the tool is gated and no valid
        token bound to this call is presented.
        """

        tool = self._tools.get(name)
        if tool is None:
            return False  # unknown tools fail at request time, not here
        if not tool.requires_approval:
            return False
        if approval_token is None:
            return True
        live = self._grants.get(approval_token.id)
        return live is None or live.tool_name != name

    def get_live_token(self, token_id: str) -> ApprovalToken | None:
        """Return a live (unconsumed) approval token by id, or None.

        A token crosses the model boundary as a JSON id string; the caller that
        re-presents it must rehydrate it into the exact ApprovalToken object
        before the registry will honour it.  This is that lookup.
        """

        return self._grants.get(token_id)

    def _verify_approval(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        token: ApprovalToken | None,
        session_id: str,
        actor_id: str,
    ) -> ApprovalToken:
        name = tool.schema.name
        if token is None:
            self.store.append_event(
                session_id,
                "tool.approval_required",
                {"name": name, "permission": tool.permission, "actor_id": actor_id},
            )
            raise ToolApprovalRequired(
                f"tool '{name}' (permission={tool.permission}) requires an approval token"
            )
        live = self._grants.get(token.id)
        arguments_hash = _safe_arguments_hash(arguments)
        current_generation = self._generations.get(name, 0)
        if (
            live is None
            or live.tool_name != name
            or live.arguments_hash != arguments_hash
            # A grant never outlives the exact tool instance it was issued for:
            # re-registering or shadowing bumps the generation, invalidating it.
            or live.tool_generation != current_generation
        ):
            self.store.append_event(
                session_id,
                "tool.approval_rejected",
                {
                    "name": name,
                    "token_id": token.id,
                    "reason": "invalid, consumed, or argument-mismatched token",
                    "actor_id": actor_id,
                },
            )
            raise ToolApprovalRequired(
                f"approval token for '{name}' is invalid, consumed, or bound to different arguments"
            )
        # A token granted in one session must not authorise a call in another.
        if live.session_id and live.session_id != session_id:
            self.store.append_event(
                session_id,
                "tool.approval_rejected",
                {
                    "name": name,
                    "token_id": token.id,
                    "reason": "token bound to a different session",
                    "actor_id": actor_id,
                },
            )
            raise ToolApprovalRequired(
                f"approval token for '{name}' was granted in a different session"
            )
        # Reserve the token (two-phase): it leaves the live set now but is
        # returned if execution fails, so a transient error does not burn a
        # human's approval.  It is only consumed for good on success.
        del self._grants[token.id]
        self.store.append_event(
            session_id,
            "tool.approved",
            {
                "name": name,
                "token_id": token.id,
                "approver_id": live.approver_id,
                "actor_id": actor_id,
            },
        )
        return live

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _new_token_id(self, arguments: dict[str, Any]) -> str:
        """A unique, unguessable token id.

        It embeds the argument hash (so a token is self-describing) plus a
        random component, so ids never collide across restarts and cannot be
        predicted from the arguments alone.
        """

        import uuid

        digest = hashlib.sha256(_canonical_arguments(arguments).encode("utf-8")).hexdigest()[:12]
        return f"apr_{digest}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
