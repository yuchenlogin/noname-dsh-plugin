"""Model adapters: vendor differences converged into a stable capability.

Business logic chooses models by **capability**, never by a provider's API
field.  An adapter maps a vendor's messages, tool calls, streaming, errors and
cost onto one contract, and preserves a reference to the vendor's raw response
so every call is auditable.

This module defines the contract and a deterministic, network-free adapter
used to verify it.  Real vendor adapters (OpenAI, Anthropic, local models)
plug in behind the same protocol -- as *plugins*, which is exactly the
vision's "capabilities crystallise into plugins" bet.  The kernel never
depends on a specific vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

from .models import ModelCapability

# Unified error classification.  ``retryable`` tells the caller whether
# retrying the same request is sensible; it is a property of the error class,
# not of a vendor's status code.
ERROR_CLASSES = {
    "rate_limit": True,
    "timeout": True,
    "overloaded": True,
    "auth": False,
    "invalid_request": False,
    "cancelled": False,
    "unknown": False,
}


class ModelAdapterError(Exception):
    """A vendor-normalised model error with a stable classification."""

    def __init__(self, error_class: str, message: str, *, vendor_ref: Any = None):
        if error_class not in ERROR_CLASSES:
            raise ValueError(f"invalid error class: {error_class}")
        super().__init__(message)
        self.error_class = error_class
        self.retryable = ERROR_CLASSES[error_class]
        self.vendor_ref = vendor_ref


@dataclass(frozen=True)
class TextBlock:
    """A text content block."""

    text: str

    @property
    def kind(self) -> str:
        return "text"


@dataclass(frozen=True)
class ImageBlock:
    """An image content block (for vision-capable models).

    ``data`` is base64-encoded image bytes and ``url`` an optional remote
    reference; exactly one source is used.  ``media_type`` is e.g. image/png.
    Image payloads are content, never credentials, and are never recorded into
    a vendor_ref.
    """

    media_type: str
    data: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        # Exactly one source: data XOR url.  Both-or-neither is ambiguous
        # caller intent and must not be silently resolved one way.
        if (self.data is None) == (self.url is None):
            raise ValueError("an image block needs exactly one of data or url")
        if not self.media_type.startswith("image/"):
            raise ValueError(f"invalid image media_type: {self.media_type}")
        # A conservative payload bound (real APIs cap images at a few MB).
        if self.data is not None and len(self.data) > 20_000_000:
            raise ValueError("image data exceeds the 20MB payload limit")

    @property
    def kind(self) -> str:
        return "image"


# A content block is a text or image part of a multimodal message.
ContentBlock = TextBlock | ImageBlock


@dataclass(frozen=True)
class ModelMessage:
    """A single message in a vendor-neutral conversation.

    ``content`` is either a plain string (text-only, the common case) or a list
    of content blocks for multimodal messages (text + images).  Plain strings
    stay fully backwards compatible.
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | list[ContentBlock]
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"invalid message role: {self.role}")
        if not isinstance(self.content, (str, list)):
            raise ValueError("content must be a string or a list of content blocks")
        if isinstance(self.content, list):
            if not self.content:
                raise ValueError("content block list cannot be empty")
            for block in self.content:
                if not isinstance(block, (TextBlock, ImageBlock)):
                    raise ValueError(f"invalid content block type: {type(block).__name__}")

    def is_multimodal(self) -> bool:
        """Return whether this message carries non-text content."""

        return isinstance(self.content, list) and any(
            isinstance(block, ImageBlock) for block in self.content
        )

    def text(self) -> str:
        """The text content of this message (blocks joined, or the string)."""

        if isinstance(self.content, str):
            return self.content
        # Join text blocks with a newline (not concatenated), so token
        # estimates and tool-result flattening don't fuse words together.
        return "\n".join(block.text for block in self.content if isinstance(block, TextBlock))


@dataclass(frozen=True)
class ModelRequest:
    """A vendor-neutral completion request."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("a request must have at least one message")


@dataclass(frozen=True)
class ModelResponse:
    """A vendor-neutral completion response.

    ``vendor_ref`` preserves a reference to the raw vendor response (or the
    response itself for small payloads) so the call remains auditable without
    the kernel depending on the vendor's shape.
    """

    text: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    model_id: str = ""
    finish_reason: str = "stop"
    input_tokens: int | None = None
    output_tokens: int | None = None
    vendor_ref: Any = None


@dataclass(frozen=True)
class StreamEvent:
    """One chunk of a streaming response."""

    kind: str  # "text_delta" | "tool_call" | "completed" | "failed"
    text: str = ""
    payload: Any = None


class ModelAdapter(Protocol):
    """The stable capability every vendor adapter must provide."""

    def id(self) -> str:
        ...

    def capability(self) -> ModelCapability:
        ...

    def complete(self, request: ModelRequest) -> ModelResponse:
        ...

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        ...

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        ...


# ---------------------------------------------------------------------------
# A deterministic, network-free adapter for verifying the contract and for
# driving the AgentLoop without any external dependency.
# ---------------------------------------------------------------------------


@dataclass
class LocalEchoAdapter:
    """A deterministic adapter that answers from a scripted rule.

    It is *not* a mock of a vendor; it is the reference implementation of the
    contract: it honours capability reporting, error classification, tool-call
    structuring and vendor_ref preservation, all without network access.  The
    ``responder`` maps a request to either text or a tool call, so tests and
    the AgentLoop can exercise the full pipeline deterministically.
    """

    model_id: str = "local-echo"
    context_window: int | None = 32_000
    # responder(request) -> str | {"tool_call": {...}}
    responder: Any = None

    def id(self) -> str:
        return self.model_id

    def capability(self) -> ModelCapability:
        return ModelCapability(
            reasoning=True,
            vision=False,
            tool_calling=True,
            streaming=True,
            context_window=self.context_window,
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        # The reference implementation honours the same invariant: vision=False
        # must reject image content, never silently drop it.
        for message in request.messages:
            if message.is_multimodal():
                raise ModelAdapterError(
                    "invalid_request",
                    f"model {self.model_id} does not support image content (vision=False)",
                )
        reply = self._respond(request)
        if isinstance(reply, dict) and "tool_call" in reply:
            return ModelResponse(
                text="",
                tool_calls=(reply["tool_call"],),
                model_id=self.model_id,
                finish_reason="tool_calls",
                input_tokens=self._count_tokens(request),
                output_tokens=0,
                vendor_ref={"adapter": "local-echo", "reply": reply},
            )
        text = str(reply)
        return ModelResponse(
            text=text,
            model_id=self.model_id,
            finish_reason="stop",
            input_tokens=self._count_tokens(request),
            output_tokens=len(text.split()),
            vendor_ref={"adapter": "local-echo", "reply": text},
        )

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        response = self.complete(request)
        if response.tool_calls:
            yield StreamEvent(kind="tool_call", payload=response.tool_calls[0])
        else:
            for word in response.text.split(" "):
                yield StreamEvent(kind="text_delta", text=word + " ")
        yield StreamEvent(kind="completed", payload=response)

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "input_tokens": self._count_tokens(request),
            "currency": "none",
            "note": "deterministic local adapter; no real cost",
        }

    def _respond(self, request: ModelRequest) -> Any:
        if self.responder is not None:
            return self.responder(request)
        # Default: echo the last user message back, deterministically.
        last_user = next(
            (m for m in reversed(request.messages) if m.role == "user"), None
        )
        return f"echo: {last_user.text() if last_user else ''}"

    @staticmethod
    def _count_tokens(request: ModelRequest) -> int:
        # A coarse, deterministic token estimate (words), not a vendor count.
        return sum(len(message.text().split()) for message in request.messages)


# ---------------------------------------------------------------------------
# Bridging an adapter into the AgentLoop's SessionDriver protocol.
# ---------------------------------------------------------------------------


class AdapterDriver:
    """Drive an AgentLoop from a ModelAdapter.

    The driver's job is to translate between the loop's turn-taking and the
    adapter's request/response: it builds a ModelRequest from the assembled
    context package, calls the adapter, and maps the response to a
    :class:`LoopResult` (completing, or requesting a tool call that the loop
    routes through the approval gate).  It never decides routing, memory or
    permissions itself.
    """

    def __init__(
        self,
        adapter: ModelAdapter,
        *,
        system_prompt: str | None = None,
        tool_registry: Any = None,
        store: Any = None,
        session_id: str | None = None,
    ):
        self.adapter = adapter
        self.system_prompt = system_prompt
        # Optional registry used to rehydrate approval token ids coming back
        # from the model into the exact ApprovalToken objects the gate honours.
        self.tool_registry = tool_registry
        # Optional store + session for recording model.* ledger events.  When
        # present, every model call is audited (model.requested/completed/failed),
        # closing the "model-visible content is reconstructible from the log"
        # coverage gap for the most core behaviour of all.
        self.store = store
        self.session_id = session_id
        # The ids of the tool calls the model last requested, so each result
        # can be correlated back to its call on the next turn (required by APIs
        # like Anthropic's tool_result block, and [OI] tool messages).
        self._pending_tool_call_ids: list[str | None] = []
        # The raw tool calls from the model's last turn, kept so the next
        # request can rebuild the assistant message that carries them.  Strict
        # OpenAI-compatible gateways reject a tool message that does not answer
        # an assistant message with matching ``tool_calls`` (400): the history
        # [user, tool] is protocol-invalid without the assistant turn in
        # between.  Only the vendor-facing shape (id / function name /
        # serialized arguments) is retained, never the executed result.
        self._pending_assistant_tool_calls: list[dict[str, Any]] = []

    def act(self, context: dict[str, Any], last_tool_result: Any = None) -> Any:
        from .agent_loop import LoopResult

        request = self._build_request(context, last_tool_result)
        response = self._complete_with_audit(request)
        if response.tool_calls:
            # Map every tool call (single or parallel).  Each is validated and
            # gets its own id for result correlation; the loop executes them all
            # through the approval gate (each gated call needs its own token).
            # Assign this turn's pending ids up front (clears any stale ids
            # from a previous approval-stop or failed turn), so correlation
            # never leaks across turns.
            self._pending_tool_call_ids = [call.get("id") for call in response.tool_calls]
            self._pending_assistant_tool_calls = [
                self._assistant_tool_call_payload(call) for call in response.tool_calls
            ]
            mapped = [self._map_tool_call(call, context) for call in response.tool_calls]
            if len(mapped) == 1:
                return LoopResult(tool_call=mapped[0])
            return LoopResult(tool_calls=mapped)
        return LoopResult(output=response.text, task_complete=True)

    @staticmethod
    def wrap_parallel_results(results: list[Any]) -> dict[str, Any]:
        """Wrap parallel tool results in the explicit correlation marker.

        The loop passes this back as ``last_tool_result`` so the driver can
        distinguish "several parallel results" from "one tool that returned a
        list" when correlating results to their call ids.
        """

        return {"_parallel": list(results)}

    @staticmethod
    def _assistant_tool_call_payload(call: dict[str, Any]) -> dict[str, Any]:
        """Re-serialise one tool call into the vendor-facing assistant shape.

        The loop's normalised call carries ``name`` / ``arguments`` (a dict);
        the vendor assistant message needs ``id`` + ``type: function`` +
        ``function: {name, arguments: <json string>}``.  Arguments are
        re-serialised (never the raw model string) so the shape is stable
        regardless of how the vendor framed them.
        """

        import json as _json

        return {
            "id": call.get("id"),
            "type": "function",
            "function": {
                "name": call.get("name"),
                "arguments": _json.dumps(
                    call.get("arguments", {}), ensure_ascii=False, default=str
                ),
            },
        }

    def _map_tool_call(self, call: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalise a vendor tool call for the loop's gate.

        A malformed call (no name, arguments=null) is a driver-contract error,
        not something the registry should choke on with a confusing message.
        An approval token crosses the model boundary as an id string and must
        be rehydrated into the exact ApprovalToken object via the registry.
        """

        from .agent_loop import AgentLoopError

        name = call.get("name")
        if not name or not isinstance(name, str) or not name.strip():
            raise AgentLoopError(
                f"adapter returned a tool call with no valid name: {call!r}"
            )
        arguments = call.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise AgentLoopError(
                f"adapter returned non-object arguments for tool '{name}': {arguments!r}"
            )
        # Rehydrate an approval token id (string) into the ApprovalToken the
        # registry honours.  A bare string would crash the gate's verification.
        approval_token = call.get("approval_token")
        if isinstance(approval_token, str):
            registry = self.tool_registry
            if registry is None:
                raise AgentLoopError(
                    "adapter supplied an approval_token id but no registry is "
                    "available to rehydrate it"
                )
            approval_token = registry.get_live_token(approval_token)
            if approval_token is None:
                raise AgentLoopError(
                    "adapter supplied an unknown or consumed approval token id"
                )
        return {
            "name": name,
            "arguments": arguments,
            "approval_token": approval_token,
        }

    def _complete_with_audit(self, request: ModelRequest) -> ModelResponse:
        """Call the adapter, recording model.* ledger events when a store is set.

        This is the audit coverage for the most core behaviour: every model
        call produces model.requested (with the vendor-neutral request shape,
        never credentials) and either model.completed (with vendor_ref) or
        model.failed (with error classification).  Without a store the driver
        stays audit-free, as before.
        """

        if self.store is None or self.session_id is None:
            return self.adapter.complete(request)

        from .adapters import ModelAdapterError as _MAE

        model_id = self.adapter.id()
        self.store.append_event(
            self.session_id,
            "model.requested",
            {
                "model_id": model_id,
                "message_count": len(request.messages),
                "tool_count": len(request.tools),
                "max_output_tokens": request.max_output_tokens,
            },
        )
        try:
            response = self.adapter.complete(request)
        except _MAE as exc:
            self.store.append_event(
                self.session_id,
                "model.failed",
                {
                    "model_id": model_id,
                    "error_class": exc.error_class,
                    "retryable": exc.retryable,
                    "vendor_ref": exc.vendor_ref,
                },
            )
            raise
        self.store.append_event(
            self.session_id,
            "model.completed",
            {
                "model_id": response.model_id,
                "finish_reason": response.finish_reason,
                "tool_call_count": len(response.tool_calls),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "vendor_ref": response.vendor_ref,
            },
        )
        return response

    def _build_request(
        self, context: dict[str, Any], last_tool_result: Any
    ) -> ModelRequest:
        messages: list[ModelMessage] = []
        if self.system_prompt:
            messages.append(ModelMessage(role="system", content=self.system_prompt))
        # The assembled package is the brief for the model.  It carries the
        # facts the model needs to act: stable canon, current task state, the
        # recent evidence window (low), the workspace guardrails (so the model
        # sees the boundary it must respect), advisory next steps, and taste as
        # soft influence.  NOTE: the package is the secrecy boundary -- only
        # content that is safe to send to a vendor belongs here.
        import json

        layers = context.get("layers", {})
        brief = {
            "task": context.get("task"),
            "high": layers.get("high"),
            "mid": layers.get("mid"),
            "low": layers.get("low"),
            "guardrails": context.get("guardrails"),
            "next_step_candidates": context.get("next_step_candidates"),
            "preference": context.get("preference"),
        }
        messages.append(
            ModelMessage(role="user", content=json.dumps(brief, ensure_ascii=False))
        )
        if last_tool_result is not None:
            # Correlate results to their tool calls.  Parallel results arrive as
            # {"_parallel": [...]} (an explicit marker), so a tool that happens
            # to RETURN a list is never mistaken for parallel results.  A single
            # result pairs with the single pending id.
            if isinstance(last_tool_result, dict) and "_parallel" in last_tool_result:
                results = last_tool_result["_parallel"]
            else:
                results = [last_tool_result]
            # Rebuild the assistant turn that issued these calls, immediately
            # before their results: strict gateways require every tool message
            # to answer an assistant message carrying the matching tool_calls.
            if self._pending_assistant_tool_calls:
                messages.append(
                    ModelMessage(
                        role="assistant",
                        content="",
                        name=json.dumps(
                            {"tool_calls": self._pending_assistant_tool_calls},
                            ensure_ascii=False,
                        ),
                    )
                )
            for index, result in enumerate(results):
                tool_use_id = (
                    self._pending_tool_call_ids[index]
                    if index < len(self._pending_tool_call_ids)
                    else None
                )
                # Positional fallback when a call has no id: a constant
                # "last_tool_result" name would falsely claim a correlation that
                # does not exist (and breaks Anthropic's tool_use_id lookup).
                name = tool_use_id if tool_use_id is not None else f"tool_result_{index}"
                messages.append(
                    ModelMessage(
                        role="tool",
                        content=json.dumps(result, ensure_ascii=False, default=str),
                        name=name,
                    )
                )
            # Pending ids are consumed once the results are fed back.
            self._pending_tool_call_ids = []
            self._pending_assistant_tool_calls = []
        # Surface the model-visible tool contracts (never the implementations).
        tools = tuple(
            context.get("visible_tools", ())
        )
        return ModelRequest(messages=tuple(messages), tools=tools)
