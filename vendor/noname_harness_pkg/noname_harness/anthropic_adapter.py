"""An Anthropic Messages API adapter, packaged as a capability plugin.

A second vendor behind the same :class:`~noname_harness.adapters.ModelAdapter`
protocol -- the proof that the contract is vendor-neutral.  Business logic
drives it through the same ``AdapterDriver`` and AgentLoop with zero changes;
only the request/response mapping differs.  Credential safety (no redirects,
HTTPS-only, safe vendor_ref, cause-based errors) is shared via
:mod:`noname_harness.vendor_http`, so it cannot drift from the OpenAI adapter.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

from .adapters import (
    ModelAdapterError,
    ModelRequest,
    ModelResponse,
    StreamEvent,
)
from .openai_adapter import _parse_tool_arguments
from .models import ModelCapability
from .vendor_http import (
    StreamTransport,
    Transport,
    classify_http_status,
    classify_transport_error,
    iter_sse,
    safe_usage_ref,
    secure_stream_transport,
    secure_transport,
    validate_base_url,
)

_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_ENV_KEY = "ANTHROPIC_API_KEY"
_ENV_BASE_URL = "ANTHROPIC_BASE_URL"
_API_VERSION = "2023-06-01"

# Anthropic stop_reason -> vendor-neutral finish_reason.
_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


@dataclass
class AnthropicAdapter:
    """An Anthropic Messages API adapter."""

    model_id: str = "claude-sonnet-4-5"
    context_window: int | None = 200_000
    timeout: float = 60.0
    transport: Transport = secure_transport
    base_url: str | None = None
    # repr=False: the credential must never appear in a repr/log/traceback.
    api_key: str | None = field(default=None, repr=False)
    allow_insecure: bool = False
    max_output_tokens: int = 4096
    # Whether this model accepts image content (multimodal).
    vision: bool = True
    # Optional SSE stream transport for true incremental streaming.
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

    def _endpoint(self) -> str:
        base = validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        return f"{base}/messages"

    def _headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(_ENV_KEY)
        if not key:
            raise ModelAdapterError("auth", f"{_ENV_KEY} is not set")
        return {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": _API_VERSION,
        }

    def _map_content(self, content: Any) -> Any:
        """Map ModelMessage content to Anthropic content blocks.

        Plain-text stays a plain string; multimodal content becomes Anthropic's
        block array ({"type": "text"} / {"type": "image"} with a base64 source).
        A vision=False adapter must not silently drop an image.
        """

        if isinstance(content, str):
            return content
        from .adapters import ModelAdapterError, TextBlock

        blocks = []
        for block in content:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            else:
                if not self.capability().vision:
                    raise ModelAdapterError(
                        "invalid_request",
                        f"model {self.model_id} does not support image content (vision=False)",
                    )
                if block.data is not None:
                    source = {
                        "type": "base64",
                        "media_type": block.media_type,
                        "data": block.data,
                    }
                else:
                    source = {"type": "url", "url": block.url}
                blocks.append({"type": "image", "source": source})
        return blocks

    def _build_body(self, request: ModelRequest) -> bytes:
        # Anthropic: system is a top-level field, not a message; content is a
        # list of blocks; tools have an input_schema object.
        # Unified invariant: a vision=False model must never receive image
        # content, on ANY role or path (user, tool, or system).
        if not self.vision:
            for message in request.messages:
                if message.is_multimodal():
                    raise ModelAdapterError(
                        "invalid_request",
                        f"model {self.model_id} does not support image content (vision=False)",
                    )
        # System content must be plain text: a multimodal system message is a
        # caller error, classified (not a raw TypeError from a newline-join on a list).
        for message in request.messages:
            if message.role == "system" and not isinstance(message.content, str):
                raise ModelAdapterError(
                    "invalid_request",
                    "system message content must be a plain string, not content blocks",
                )
        system_parts = [m.content for m in request.messages if m.role == "system"]
        messages = []
        for message in request.messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                # A tool result must be a tool_result content block inside a user
                # message, carrying the tool_use_id it answers -- a bare user
                # message is rejected by the real API (and breaks role
                # alternation).  The id is threaded via message.name.
                tool_content = (
                    message.content
                    if isinstance(message.content, str)
                    else message.text()
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.name or "unknown",
                                "content": tool_content,
                            }
                        ],
                    }
                )
            else:
                messages.append(
                    {"role": message.role, "content": self._map_content(message.content)}
                )
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": (request.max_output_tokens if request.max_output_tokens is not None else self.max_output_tokens),
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            name: {"type": _json_schema_type(t)}
                            for name, t in tool.get("input_schema", {}).items()
                        },
                        "required": list(tool.get("input_schema", {}).keys()),
                    },
                }
                for tool in request.tools
            ]
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
            raise classify_http_status(status, raw)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelAdapterError(
                "unknown",
                f"unparseable vendor response: {exc}",
                vendor_ref={"status": 200, "bytes": len(raw)},
            ) from exc
        return self._map_response(data, raw)

    def _map_response(self, data: dict[str, Any], raw: bytes) -> ModelResponse:
        # Malformed (but valid-JSON) responses must fail inside the
        # ModelAdapterError contract, not crash with AttributeError.
        if not isinstance(data, dict):
            raise ModelAdapterError(
                "unknown",
                "vendor returned a non-object response",
                vendor_ref={"status": 200, "bytes": len(raw)},
            )
        # Anthropic returns content as a list of blocks (text + tool_use).
        blocks = data.get("content") or []
        if not isinstance(blocks, list):
            raise ModelAdapterError(
                "unknown", "vendor returned malformed content blocks", vendor_ref={"id": data.get("id")}
            )
        text_parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        tool_calls = tuple(
            self._map_tool_use(b, data)
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_use"
        )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ModelResponse(
            text="\n".join(part for part in text_parts if part),
            tool_calls=tool_calls,
            model_id=data.get("model", self.model_id),
            finish_reason=_FINISH_REASON_MAP.get(data.get("stop_reason"), "stop"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            vendor_ref={
                "status": 200,
                "id": data.get("id"),
                "usage": safe_usage_ref(
                    {
                        "prompt_tokens": usage.get("input_tokens"),
                        "completion_tokens": usage.get("output_tokens"),
                    }
                ),
            },
        )

    def _map_tool_use(self, block: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        """Map one tool_use block, capping the input size (DoS guard)."""

        input_value = block.get("input") if isinstance(block.get("input"), dict) else {}
        import json as _json

        if len(_json.dumps(input_value)) > 1_000_000:
            raise ModelAdapterError(
                "unknown",
                "vendor tool_use input exceeds the 1MB limit",
                vendor_ref={"id": data.get("id")},
            )
        return {
            "name": block.get("name"),
            "arguments": input_value,
            "id": block.get("id"),
        }

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        # True SSE streaming when a stream transport is configured; otherwise a
        # deterministic complete-then-re-emit replay.
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
        """Consume Anthropic's SSE endpoint, emitting incremental events."""

        body = self._build_body(request)
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
        tool_blocks: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        stop_reason = "end_turn"
        for data in iter_sse(lines):
            event_type = data.get("type")
            index = data.get("index")
            if event_type in {"content_block_start", "content_block_delta", "content_block_stop"} and index is None:
                raise ModelAdapterError(
                    "unknown",
                    f"malformed stream event: {event_type} missing index",
                    vendor_ref={"type": event_type},
                )
            if event_type == "content_block_start":
                block = data.get("content_block", {})
                if block.get("type") == "tool_use":
                    tool_blocks[index] = {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "arguments_json": "",
                    }
            elif event_type == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        text_parts.append(text)
                        yield StreamEvent(kind="text_delta", text=text)
                elif delta.get("type") == "input_json_delta":
                    block = tool_blocks.get(index)
                    if block is not None:
                        block["arguments_json"] += delta.get("partial_json", "")
            elif event_type == "content_block_stop":
                block = tool_blocks.pop(index, None)
                if block is not None:
                    arguments = _parse_tool_arguments(block["arguments_json"])
                    yield StreamEvent(
                        kind="tool_call",
                        payload={"name": block["name"], "arguments": arguments, "id": block["id"]},
                    )
            elif event_type == "message_delta":
                delta = data.get("delta", {})
                if delta.get("stop_reason"):
                    stop_reason = delta["stop_reason"]
                if data.get("usage"):
                    usage.update(data["usage"])
            elif event_type == "message_start":
                message = data.get("message", {})
                if message.get("usage"):
                    usage.update(message["usage"])
        # Flush any orphaned tool_use blocks (stream ended without a stop):
        # emit their tool_call events and include them in the completed payload,
        # using the same capped, non-crashing argument parse as the stop path.
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
            finish_reason=_FINISH_REASON_MAP.get(stop_reason, "stop"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            vendor_ref={"status": 200, "stream": True, "usage": safe_usage_ref({"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")})},
        )
        yield StreamEvent(kind="completed", payload=response)

    def estimate_cost(self, request: ModelRequest) -> dict[str, Any]:
        input_tokens = sum(len(m.text().split()) for m in request.messages)
        return {
            "model_id": self.model_id,
            "input_tokens": input_tokens,
            "currency": "usd",
            "note": "estimate from word count; real cost from vendor usage in vendor_ref",
        }


def _json_schema_type(type_name: str) -> str:
    return {
        "string": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "boolean",
        "object": "object",
        "array": "array",
    }.get(type_name, "string")


def load_anthropic_adapter(runtime: Any, **adapter_kwargs: Any) -> AnthropicAdapter:
    """Load the Anthropic adapter through a PluginRuntime and return it."""

    adapter = AnthropicAdapter(**adapter_kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"model-anthropic-{adapter.model_id}",
            version="1.0.0",
            capabilities=(f"model:{adapter.model_id}", "model-adapter"),
            max_permission="read",
            side_effects=("network-egress", "billing"),
        ),
        build=lambda: [],
    )
    runtime.load(plugin)
    return adapter
