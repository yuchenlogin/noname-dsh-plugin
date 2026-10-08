"""An OpenAI-compatible model adapter, packaged as a capability plugin.

This is the reference for how a *real vendor* plugs into the harness: behind
the same :class:`~noname_harness.adapters.ModelAdapter` protocol as the
deterministic ``LocalEchoAdapter``, so business logic never notices the
difference.  It honours the vision's bet -- a vendor capability crystallises
into a plugin, and the kernel never depends on the vendor.

Two deliberate design choices keep it testable and honest:

- **The transport is injectable.**  Request-building and response-parsing are
  separated from the actual HTTP call (a callable).  Tests inject a
  deterministic replay transport, so request construction, response mapping,
  error classification and vendor_ref preservation are all verified without
  network or an API key.  The default transport is a real ``urllib`` POST.
- **The API key is never stored or logged.**  It is read from the environment
  and used only in the request header; ``vendor_ref`` carries a *reference* to
  the raw response, never credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

from .adapters import (
    ModelAdapterError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from .models import ModelCapability
from .vendor_http import (
    StreamTransport,
    classify_transport_error,
    iter_sse,
    safe_usage_ref,
    secure_stream_transport,
)
from .vendor_http import _NoRedirectHandler  # re-exported for backwards-compatible imports
from .vendor_http import (
    Transport,
    classify_http_status,
    classify_transport_error,
    json_schema_type,
    safe_usage_ref,
    secure_transport,
    validate_base_url,
    word_count_cost,
)

# A transport maps (url, headers, body_bytes, timeout) -> (status, response_bytes).

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ENV_KEY = "OPENAI_API_KEY"
_ENV_BASE_URL = "OPENAI_BASE_URL"


def _parse_tool_arguments(arguments_json: str) -> Any:
    """Parse accumulated tool-call argument fragments, with a size cap.

    Real streams deliver arguments as a JSON string in fragments; an
    unparseable or oversized result falls back to a marked raw form (never a
    crash, never unbounded).
    """

    import json as _json

    if len(arguments_json) > 1_000_000:
        return {"_error": "arguments exceed the 1MB limit"}
    try:
        return _json.loads(arguments_json) if arguments_json else {}
    except ValueError:
        return {"_raw": arguments_json}


@dataclass
class OpenAIAdapter:
    """An OpenAI-compatible chat-completions adapter.

    Works with the OpenAI API and with compatible endpoints (set
    ``OPENAI_BASE_URL``).  The model id and capability come from configuration,
    not hard-coded vendor specifics, so the same class serves many endpoints.
    """

    model_id: str = "gpt-4o-mini"
    context_window: int | None = 128_000
    timeout: float = 60.0
    transport: Transport = secure_transport
    base_url: str | None = None
    # repr=False: the credential must never appear in a repr/log/traceback.
    api_key: str | None = field(default=None, repr=False)
    # Opt-in escape hatch for plaintext HTTP (e.g. a local model server).
    allow_insecure: bool = False
    # Whether this model accepts image content (multimodal).  Declared in
    # config like model_id/context_window, so a text-only model can be
    # declared vision=False and image blocks are rejected locally.
    vision: bool = True
    # Optional SSE stream transport for true incremental streaming.  When
    # set, stream() consumes the vendor's SSE endpoint incrementally; when
    # None, stream() falls back to a complete-then-re-emit replay.
    # Defaults to the real secure SSE transport; pass None to use the
    # complete-then-re-emit replay (tests / offline).
    stream_transport: StreamTransport | None = secure_stream_transport

    def id(self) -> str:
        return self.model_id

    def capability(self) -> ModelCapability:
        return ModelCapability(
            reasoning=True,
            vision=self.vision,
            tool_calling=True,
            streaming=True,
            context_window=self.context_window,
        )

    # -- request/response mapping ------------------------------------------

    def _endpoint(self) -> str:
        base = validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(_ENV_KEY)
        if not key:
            raise ModelAdapterError("auth", f"{_ENV_KEY} is not set")
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }

    def _map_message(self, message: ModelMessage) -> dict[str, Any]:
        """Map a ModelMessage to the [OI] message shape.

        Plain-text content stays a plain string; multimodal content becomes a
        content-part array ({"type": "text"} / {"type": "image_url"}).  A
        vision-capable adapter must not silently drop an image; a text-only
        message never produces the part array (backwards compatible).
        """

        if isinstance(message.content, str):
            mapped: dict[str, Any] = {"role": message.role, "content": message.content}
        elif message.role == "tool":
            # [OI] tool messages carry string content, not a part array; image
            # content is rejected upstream by the unified vision check when
            # vision=False, and flattened to text otherwise.
            mapped = {"role": message.role, "content": message.text()}
        else:
            parts: list[dict[str, Any]] = []
            for block in message.content:
                if block.kind == "text":
                    parts.append({"type": "text", "text": block.text})
                elif block.kind == "image":
                    source = (
                        f"data:{block.media_type};base64,{block.data}"
                        if block.data is not None
                        else block.url
                    )
                    parts.append({"type": "image_url", "image_url": {"url": source}})
            mapped = {"role": message.role, "content": parts}
        if message.role == "assistant" and message.name is not None:
            # The driver threads the assistant turn's tool_calls through
            # ``name`` as a JSON envelope (the vendor-neutral ModelMessage has
            # no dedicated tool_calls field).  Strict gateways require the
            # assistant message to carry them top-level so the following tool
            # message answers a real call.  Malformed envelopes fall back to a
            # plain name rather than corrupting the request.
            try:
                envelope = json.loads(message.name)
            except (ValueError, TypeError):
                envelope = None
            if isinstance(envelope, dict) and "tool_calls" in envelope:
                mapped["tool_calls"] = envelope["tool_calls"]
                if not mapped.get("content"):
                    # OpenAI frames a tool-calling assistant turn with null
                    # content, not an empty string.
                    mapped["content"] = None
                return mapped
            mapped["name"] = message.name
            return mapped
        if message.name is not None:
            if message.role == "tool":
                # The driver threads the vendor tool-call id through
                # ``ModelMessage.name``; the [OI] protocol requires it as a
                # top-level ``tool_call_id`` on tool messages.  A bare ``name``
                # is not a substitute: strict OpenAI-compatible gateways
                # reject a tool message without tool_call_id (400), and a
                # mis-labelled correlation is worse than none.  Anthropic maps
                # the same field to its ``tool_use_id`` block key.
                mapped["tool_call_id"] = message.name
            else:
                mapped["name"] = message.name
        return mapped

    def _build_body(self, request: ModelRequest) -> bytes:
        # Unified invariant: a vision=False model must never receive image
        # content, on ANY role or path (not just the user-message mapper).
        if not self.vision:
            for message in request.messages:
                if message.is_multimodal():
                    raise ModelAdapterError(
                        "invalid_request",
                        f"model {self.model_id} does not support image content (vision=False)",
                    )
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [self._map_message(m) for m in request.messages],
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                name: {"type": json_schema_type(t)}
                                for name, t in tool.get("input_schema", {}).items()
                            },
                            "required": list(tool.get("input_schema", {}).keys()),
                        },
                    },
                }
                for tool in request.tools
            ]
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return json.dumps(payload).encode("utf-8")

    def complete(self, request: ModelRequest) -> ModelResponse:
        body = self._build_body(request)
        headers = self._headers()
        try:
            status, raw = self.transport(self._endpoint(), headers, body, self.timeout)
        except ModelAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised at the vendor seam
            raise classify_transport_error(exc, self.timeout) from exc

        if status != 200:
            raise self._classify_http_error(status, raw)

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelAdapterError(
                "unknown",
                f"unparseable vendor response: {exc}",
                # Never raw bytes (not JSON-serialisable, and could carry
                # content) -- a length is enough to audit.
                vendor_ref={"status": 200, "bytes": len(raw)},
            ) from exc
        return self._map_response(data, raw)

    def _map_response(self, data: dict[str, Any], raw: bytes) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ModelAdapterError(
                "unknown",
                "vendor returned no choices",
                vendor_ref={"id": data.get("id")} if isinstance(data, dict) else None,
            )
        message = choices[0].get("message", {})
        usage = data.get("usage", {})
        tool_calls = tuple(
            self._map_tool_call(call, data) for call in message.get("tool_calls", []) or []
        )
        return ModelResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            model_id=data.get("model", self.model_id),
            finish_reason=choices[0].get("finish_reason", "stop"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            # Preserve a reference for audit -- an allowlist, never the raw
            # body: only the vendor id and the three numeric usage fields are
            # kept, so hostile extra keys can never smuggle content into the
            # ledger, and no credentials are ever stored.
            vendor_ref={
                "status": 200,
                "id": data.get("id"),
                "usage": safe_usage_ref(usage),
            },
        )

    def _map_tool_call(self, call: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        """Map one vendor tool call, normalising malformed arguments.

        A vendor (or OpenAI-compatible server) may return ``arguments`` as a
        dict instead of a string, or as truncated/invalid JSON mid-generation.
        Neither may escape as a raw TypeError/JSONDecodeError outside the
        ModelAdapterError contract.
        """

        function = call.get("function", {})
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, dict):
            parsed = arguments
        else:
            try:
                parsed = json.loads(arguments or "{}")
            except (ValueError, TypeError) as exc:
                raise ModelAdapterError(
                    "unknown",
                    "vendor returned malformed tool-call arguments",
                    vendor_ref={"id": data.get("id")},
                ) from exc
        # A dict (or parsed dict) of unbounded size is a memory-amplification
        # DoS into the ledger; cap the serialized size.
        try:
            serialized = json.dumps(parsed)
        except (ValueError, TypeError) as exc:
            raise ModelAdapterError(
                "unknown",
                "vendor tool-call arguments are not JSON-serialisable",
                vendor_ref={"id": data.get("id")},
            ) from exc
        if len(serialized) > 1_000_000:
            raise ModelAdapterError(
                "unknown",
                "vendor tool-call arguments exceed the 1MB limit",
                vendor_ref={"id": data.get("id")},
            )
        return {
            "name": function.get("name"),
            "arguments": parsed,
            "id": call.get("id"),
        }

    def _classify_http_error(self, status: int, raw: bytes) -> ModelAdapterError:
        return classify_http_status(status, raw)

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        # True SSE streaming when a stream transport is configured; otherwise a
        # deterministic complete-then-re-emit replay (still auditable).
        if self.stream_transport is not None:
            yield from self._stream_sse(request)
            return
        response = self.complete(request)
        for call in response.tool_calls:
            yield StreamEvent(kind="tool_call", payload=call)
        if not response.tool_calls and response.text:
            yield StreamEvent(kind="text_delta", text=response.text)
        yield StreamEvent(kind="completed", payload=response)

    def _stream_sse(self, request: ModelRequest) -> Iterator[StreamEvent]:
        """Consume the vendor's SSE endpoint, emitting incremental events."""

        body = self._build_body(request)
        # Ask the vendor for a stream.
        payload = json.loads(body.decode("utf-8"))
        payload["stream"] = True
        body = json.dumps(payload).encode("utf-8")
        try:
            lines = self.stream_transport(self._endpoint(), self._headers(), body, self.timeout)
        except ModelAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised at the vendor seam
            raise classify_transport_error(exc, self.timeout) from exc
        text_parts: list[str] = []
        # Real OpenAI streams send a tool call's name/id ONCE (first delta at an
        # index) and then stream function.arguments as fragments in LATER deltas
        # that carry no name -- so fragments must be accumulated BY INDEX, not
        # only when a name is present.
        tool_blocks: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        for data in iter_sse(lines):
            # usage may ride the final chunk's top-level field (OpenAI streams it
            # separately from the choices array, often with stream_options).
            if data.get("usage"):
                usage = data["usage"]
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            chunk_text = delta.get("content")
            if chunk_text:
                text_parts.append(chunk_text)
                yield StreamEvent(kind="text_delta", text=chunk_text)
            for call in delta.get("tool_calls", []) or []:
                index = call.get("index", 0)
                function = call.get("function", {})
                block = tool_blocks.setdefault(index, {"name": None, "id": None, "arguments_json": ""})
                if function.get("name"):
                    block["name"] = function["name"]
                if call.get("id"):
                    block["id"] = call["id"]
                # arguments arrive as string fragments; concatenate them.
                block["arguments_json"] += function.get("arguments", "") or ""
            if choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]
        # Parse accumulated argument fragments (with the shared cap + fallback).
        mapped_calls = []
        for index in sorted(tool_blocks):
            block = tool_blocks[index]
            arguments = _parse_tool_arguments(block["arguments_json"])
            call = {"name": block["name"], "arguments": arguments, "id": block["id"]}
            mapped_calls.append(call)
            yield StreamEvent(kind="tool_call", payload=call)
        response = ModelResponse(
            text="".join(text_parts),
            tool_calls=tuple(mapped_calls),
            model_id=self.model_id,
            finish_reason=finish_reason,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            vendor_ref={"status": 200, "stream": True, "usage": safe_usage_ref(usage)},
        )
        yield StreamEvent(kind="completed", payload=response)

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        return word_count_cost(self.model_id, request)


# ---------------------------------------------------------------------------
# Plugin packaging: the adapter crystallises into a loadable plugin.
# ---------------------------------------------------------------------------


def load_openai_adapter(runtime: Any, **adapter_kwargs: Any) -> OpenAIAdapter:
    """Load the OpenAI adapter through a PluginRuntime and return it.

    A vendor capability crystallises into a plugin: the manifest is validated
    and the load audited (``plugin.loaded``) before the adapter is handed back.
    The manifest honestly declares the plugin's real side effects --
    ``network-egress`` (outbound HTTPS) and ``billing`` (metered) -- because
    ``max_permission`` covers only contributed tools, and this plugin
    contributes none.  ``build()`` returns the adapter itself so the caller can
    drive an AgentLoop with it.
    """

    adapter = OpenAIAdapter(**adapter_kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"model-openai-{adapter.model_id}",
            version="1.0.0",
            capabilities=(f"model:{adapter.model_id}", "model-adapter"),
            max_permission="read",
            side_effects=("network-egress", "billing"),
        ),
        build=lambda: [],
    )
    runtime.load(plugin)
    return adapter
