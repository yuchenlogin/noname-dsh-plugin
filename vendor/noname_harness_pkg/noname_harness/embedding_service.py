"""A real embedding service, behind the same EmbeddingFn protocol.

This upgrades semantic recall from the deterministic local lexical embedding
to true semantic embeddings, while the *contract never changes*: the vector
index is still a rebuildable projection (never the source of truth), the
``EmbeddingFn`` signature is unchanged, and a real service plugs in exactly
where the local one did.

Credential safety is shared with the vendor adapters via
:mod:`noname_harness.vendor_http`: no redirects, HTTPS-only base URL, a
vendor_ref that is a *reference* (never the body, never credentials), and
cause-based error classification.  The API key is read from the environment
and used only in the request header -- it is never stored or logged.

The transport is injectable (a real ``urllib`` POST by default, a
deterministic replay transport in tests), so the contract is verified with no
network and no API key.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

from .adapters import ModelAdapterError
from .vendor_http import (
    Transport,
    classify_http_status,
    classify_transport_error,
    safe_usage_ref,
    secure_transport,
    validate_base_url,
)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ENV_KEY = "OPENAI_API_KEY"
_ENV_BASE_URL = "OPENAI_BASE_URL"

# A conservative input bound, well under the vendor token limit.
_MAX_INPUT_CHARS = 32_000


@dataclass
class OpenAIEmbedding:
    """An OpenAI embeddings service behind the EmbeddingFn protocol.

    ``model_id`` doubles as the embedding-space identifier recorded in the
    vector projection, so a query with a different embedding function is
    refused instead of silently comparing across spaces.
    """

    model_id: str = "text-embedding-3-small"
    timeout: float = 30.0
    transport: Transport = secure_transport
    base_url: str | None = None
    # repr=False: the credential must never appear in a repr/log/traceback.
    api_key: str | None = field(default=None, repr=False)
    # Runtime state: pinned embedding dimension and last-observed usage (for cost audit).
    _dimensions: int | None = field(default=None, init=False, repr=False)
    last_usage: dict[str, Any] = field(default_factory=dict, init=False)
    allow_insecure: bool = False

    def _endpoint(self) -> str:
        base = validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        return f"{base}/embeddings"

    def _headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(_ENV_KEY)
        if not key:
            raise ModelAdapterError("auth", f"{_ENV_KEY} is not set")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}

    def __call__(self, text: str) -> list[float]:
        """Embed a single text into a float vector (the EmbeddingFn protocol)."""

        # Enforce a conservative input bound locally, so a corrupted/adversarial
        # huge event fails fast instead of an unbounded egress + a guaranteed-
        # failed billed request (the vendor's 8191-token cap is far above this).
        if len(text) > _MAX_INPUT_CHARS:
            raise ModelAdapterError(
                "invalid_request",
                f"input text exceeds the {_MAX_INPUT_CHARS}-character embedding limit",
            )
        body = json.dumps({"model": self.model_id, "input": text}).encode("utf-8")
        try:
            status, raw = self.transport(self._endpoint(), self._headers(), body, self.timeout)
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
                f"unparseable embedding response: {exc}",
                vendor_ref={"status": 200, "bytes": len(raw)},
            ) from exc
        return self._map_vector(data)

    def _map_vector(self, data: dict[str, Any]) -> list[float]:
        if not isinstance(data, dict):
            raise ModelAdapterError("unknown", "embedding response is not an object")
        # The vendor's model field must match what we asked for: a proxy/alias
        # returning a different model would silently mislabel the embedding
        # space the cross-space guard relies on.
        vendor_model = data.get("model")
        if isinstance(vendor_model, str) and vendor_model != self.model_id:
            raise ModelAdapterError(
                "unknown",
                f"vendor returned model {vendor_model!r}, expected {self.model_id!r}",
                vendor_ref={"id": data.get("id")},
            )
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise ModelAdapterError(
                "unknown", "embedding response has no data", vendor_ref={"id": data.get("id")}
            )
        item = items[0]
        if not isinstance(item, dict):
            raise ModelAdapterError(
                "unknown", "embedding data item is malformed", vendor_ref={"id": data.get("id")}
            )
        # Capture usage for cost audit (the plugin declares a billing side effect).
        self.last_usage = safe_usage_ref(data.get("usage"))
        vector = item.get("embedding")
        # type(v) excludes bool, which isinstance(int) would otherwise admit.
        if not isinstance(vector, list) or not all(type(v) in (int, float) for v in vector):
            raise ModelAdapterError(
                "unknown", "embedding vector is malformed", vendor_ref={"id": data.get("id")}
            )
        # A numeric component can still be non-finite: NaN/inf pass the type
        # check but poison every cosine similarity computed against the index.
        # Fail closed at the vendor seam (classified non-retryable).
        if any(not math.isfinite(v) for v in vector):
            raise ModelAdapterError(
                "invalid_request",
                "embedding vector contains non-finite components (NaN/inf)",
                vendor_ref={"id": data.get("id")},
            )
        # Pin the dimension on the first successful call and refuse any later
        # drift: a vendor that changes dimension mid-index corrupts the whole
        # projection (and only fails at query time otherwise).
        if self._dimensions is None:
            self._dimensions = len(vector)
        elif len(vector) != self._dimensions:
            raise ModelAdapterError(
                "unknown",
                f"embedding dimension changed from {self._dimensions} to {len(vector)} mid-use",
                vendor_ref={"id": data.get("id")},
            )
        return [float(v) for v in vector]


def load_openai_embedding(runtime: Any, **kwargs: Any) -> OpenAIEmbedding:
    """Load the embedding service through a PluginRuntime and return it.

    A vendor capability crystallises into a plugin: the manifest is validated
    and the load audited before the service is handed back.  The manifest
    honestly declares network-egress and billing side effects.
    """

    service = OpenAIEmbedding(**kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"embedding-openai-{service.model_id}",
            version="1.0.0",
            capabilities=(f"embedding:{service.model_id}", "embedding-service"),
            max_permission="read",
            side_effects=("network-egress", "billing"),
        ),
        build=lambda: [],
    )
    runtime.load(plugin)
    return service
