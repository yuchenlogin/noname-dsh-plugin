"""A real image-model generator for taste cards, packaged as a plugin.

This fills the "real imagegen plugin" slot left open in DELIVERY.md: the
default ``local_typographic_image`` is a deterministic, network-free
typographic renderer; a real image model plugs in behind the same
:class:`~noname_harness.card_images.ImageGenerator` protocol, and the kernel
never depends on the vendor.  It honours the vision's bet -- a vendor
capability crystallises into a plugin.

The same deliberate choices as the chat/embedding adapters keep it testable
and honest:

- **The transport is injectable.**  Request-building and response-parsing are
  separated from the actual HTTP call (a callable).  Tests inject a
  deterministic replay transport, so request construction, response mapping,
  error classification and vendor_ref safety are all verified without network
  or an API key.  The default transport is the shared credential-safe
  ``urllib`` POST from :mod:`noname_harness.vendor_http`.
- **Credential safety is shared, not re-rolled.**  HTTPS-only base URL by
  default (``allow_insecure`` is a local opt-in), redirects refused (a 302
  would forward the Bearer key), errors classified by cause with a
  retryable flag, and every vendor_ref is an allowlisted *reference* -- an
  error body never reaches the ledger.
- **The API key is never stored or logged.**  It is read from the environment
  and used only in the request header; ``repr=False`` keeps it out of reprs,
  logs and tracebacks.
- **The multi-modal risk contract holds.**  The generator receives only the
  card's *text* summary (``card_image_for`` already enforces that boundary);
  it never accepts image input, so there is no visual inference path back to
  the user's other data.  The generated image is content, persisted by the
  caller (``taste_cards.generate_image``) as an append-only evidence span --
  this module writes no storage of its own.
- **The metadata contract is honest.**  ``model`` / ``prompt`` (exactly what
  was sent) / ``size`` are recorded.  The OpenAI-compatible
  ``/images/generations`` API does not accept a seed, so the metadata says so
  explicitly (``seed: None``, ``seed_supported: False``) instead of forging
  determinism that does not exist.  ``rebuildable: False`` marks the visual
  explanation as non-deterministic -- the typographic renderer remains the
  rebuildable default.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .adapters import ModelAdapterError
from .card_images import GeneratedImage
from .vendor_http import (
    Transport,
    bounded_vendor_int,
    classify_http_status,
    classify_transport_error,
    secure_transport,
    validate_base_url,
    validate_timeout,
)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ENV_KEY = "OPENAI_API_KEY"
_ENV_BASE_URL = "OPENAI_BASE_URL"
_ENV_IMAGE_MODEL = "OPENAI_IMAGE_MODEL"
_DEFAULT_MODEL = "gpt-image-1"
_GENERATOR_PREFIX = "openai-image"

# Prompt-bomb defence: card text is truncated to these local bounds BEFORE the
# prompt is built, so a corrupted/adversarial card cannot turn into an
# unbounded billed egress.
_MAX_TITLE_CHARS = 200
_MAX_ATTITUDE_CHARS = 800
_MAX_TRACK_CHARS = 80
_MAX_PROMPT_CHARS = 4_000

# The prompt preamble: an abstract visual metaphor, aligned with the
# multi-modal contract (a visual explanation, not evidence; nothing to
# mis-read as personality).  A real model cannot be *structurally* guaranteed
# face-free the way the typographic renderer is, so this is an instruction,
# and the metadata honestly carries abstract/no_faces from configuration.
_PROMPT_PREAMBLE = (
    "An abstract, non-photorealistic visual metaphor -- no faces, no people, "
    "no readable text, no demographic cues -- as a visual explanation (not "
    "evidence) for a taste card"
)

# The image usage fields a gpt-image-style response may carry; allowlisted and
# int-coerced, everything else dropped (the same posture as safe_usage_ref,
# which only covers the chat field names).
_IMAGE_USAGE_FIELDS = ("total_tokens", "input_tokens", "output_tokens")

# Response-bomb defence: the request side caps the prompt; the response side
# caps the image.  20MB of decoded image bytes aligns with the multi-modal
# ImageBlock bound; the base64 *string* is length-checked BEFORE decoding, so
# an oversized payload is rejected without ever being amplified into bytes.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_B64_CHARS = (_MAX_IMAGE_BYTES // 3) * 4 + 8  # ~28MB of base64 text

# The media types a vendor image may be persisted as.  Anything active
# (image/svg+xml, text/html, ...) is refused: a vendor-controlled SVG with
# <script> written into the user's workspace would be a stored-XSS channel.
# The whitelist still maps cleanly onto taste_cards._MEDIA_SUFFIX (.png/.jpg)
# and the new .webp suffix.
_ALLOWED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MEDIA_TYPE_SHAPE = re.compile(r"^[a-z]+/[a-z0-9.+-]+$")

def _coerce_media_type(value: Any) -> str:
    """Validate a vendor-supplied media type against the safe whitelist."""

    if not isinstance(value, str) or not _MEDIA_TYPE_SHAPE.match(value):
        raise ModelAdapterError(
            "invalid_request",
            "vendor returned a malformed media_type (unexpected characters)",
        )
    if value not in _ALLOWED_MEDIA_TYPES:
        raise ModelAdapterError(
            "invalid_request",
            f"vendor media_type {value!r} is not allowed "
            "(only image/png, image/jpeg, image/webp are persisted)",
        )
    return value

# The only vendor_ref kept on a successful generation: an allowlisted
# reference, never the response body (which carries megabytes of image data)
# and never anything attacker-controlled that could reach the ledger.
def _safe_generation_ref(data: Any) -> dict[str, Any]:
    ref: dict[str, Any] = {"status": 200}
    if isinstance(data, dict):
        created = bounded_vendor_int(data.get("created"))
        if created is not None:
            ref["created"] = created
        usage = data.get("usage")
        if isinstance(usage, dict):
            ref["usage"] = {
                key: coerced
                for key in _IMAGE_USAGE_FIELDS
                if (coerced := bounded_vendor_int(usage.get(key))) is not None
            }
    return ref

def _coerce_size(size: str) -> str:
    """Validate an OpenAI-compatible image size ("WxH" or "auto")."""

    size = str(size).strip().lower()
    if size == "auto":
        return size
    width, sep, height = size.partition("x")
    if sep and width.isdigit() and height.isdigit() and int(width) > 0 and int(height) > 0:
        return f"{int(width)}x{int(height)}"
    raise ModelAdapterError(
        "invalid_request",
        f"invalid image size {size!r} (expected e.g. '1024x1024' or 'auto')",
    )

def build_card_prompt(summary: dict[str, Any]) -> str:
    """Build the honest, bounded text prompt actually sent to the image model.

    Only the card's text summary is used (the multi-modal contract: the
    generator never sees the user's other data, and never an image).  Each
    field is truncated to a local bound and the final prompt is capped, so a
    corrupted or adversarial card cannot become a prompt bomb.  The prompt is
    recorded verbatim in the metadata, so what was billed is always traceable.
    """

    title = str(summary.get("title", ""))[:_MAX_TITLE_CHARS]
    attitude = str(summary.get("attitude", ""))[:_MAX_ATTITUDE_CHARS]
    track = str(summary.get("track", ""))[:_MAX_TRACK_CHARS]
    prompt = f"{_PROMPT_PREAMBLE} titled '{title}' (track: {track}). Attitude: {attitude}"
    return prompt[:_MAX_PROMPT_CHARS]

@dataclass
class OpenAIImageGenAdapter:
    """An OpenAI-compatible image generator behind the ImageGenerator protocol.

    Works with the OpenAI ``/images/generations`` API (gpt-image / dall-e
    style) and with compatible endpoints (set ``OPENAI_BASE_URL``).  The model
    id and generator id come from configuration, not hard-coded vendor
    specifics.
    """

    model_id: str | None = None  # None -> OPENAI_IMAGE_MODEL or the default
    size: str = "1024x1024"
    timeout: float = 120.0  # image generation is slower than chat
    transport: Transport = secure_transport
    base_url: str | None = None
    # repr=False: the credential must never appear in a repr/log/traceback.
    api_key: str | None = field(default=None, repr=False)
    # Opt-in escape hatch for plaintext HTTP (e.g. a local model server).
    allow_insecure: bool = False
    # Whether the configured model is prompted/attested as abstract and
    # face-free.  These come from configuration and are recorded honestly --
    # the service must NOT stamp no_faces=True on a photorealistic generator.
    abstract: bool = True
    no_faces: bool = True
    # Runtime state: the last-observed usage + vendor_ref (cost audit).
    last_usage: dict[str, int] = field(default_factory=dict, init=False)
    last_vendor_ref: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    # Lifecycle gate (fail-closed): the adapter only performs network egress
    # and billing while the plugin that crystallised it is loaded.  A bare
    # adapter (never loaded, or unloaded) refuses to generate.
    _active: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # Validate at construction: a zero/negative/NaN timeout has undefined
        # transport behaviour; the base URL is a credential trust boundary.
        self.timeout = validate_timeout(self.timeout)
        validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        # Fail-closed normalization: the size is validated once and stored in
        # canonical form, so what was validated is exactly what is sent.
        self.size = _coerce_size(self.size)

    def _mark_plugin_loaded(self) -> None:
        """Activate egress (called by load_image_gen_plugin after runtime.load())."""

        self._active = True

    def _mark_plugin_unloaded(self) -> None:
        """Deactivate egress (called by the plugin's unload hook)."""

        self._active = False

    def resolved_model_id(self) -> str:
        return self.model_id or os.environ.get(_ENV_IMAGE_MODEL) or _DEFAULT_MODEL

    def generator_id(self) -> str:
        """The stable id recorded as ``model`` in the metadata contract."""

        return f"{_GENERATOR_PREFIX}-{self.resolved_model_id()}"

    # -- request/response mapping ------------------------------------------

    def _endpoint(self) -> str:
        base = validate_base_url(
            self.base_url or os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL,
            allow_insecure=self.allow_insecure,
        )
        return f"{base}/images/generations"

    def _headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(_ENV_KEY)
        if not key:
            raise ModelAdapterError("auth", f"{_ENV_KEY} is not set")
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }

    def _build_body(self, prompt: str) -> bytes:
        # self.size was canonicalised in __post_init__ (validate-then-use):
        # the exact validated value is what reaches the wire.
        return json.dumps(
            {"model": self.resolved_model_id(), "prompt": prompt, "n": 1, "size": self.size}
        ).encode("utf-8")

    # -- the ImageGenerator protocol ----------------------------------------

    def __call__(self, summary: dict[str, Any]) -> GeneratedImage:
        """Generate one card image from the card's text summary.

        Fail-closed on lifecycle: an adapter that was never activated by
        ``load_image_gen_plugin`` -- or whose plugin has been unloaded --
        refuses to generate.  Side effects (network egress, billing) are bound
        to the reversible plugin lifecycle, not to a leaked object handle.
        """

        if not self._active:
            raise ModelAdapterError(
                "invalid_request",
                "image generator is not active: load it via load_image_gen_plugin "
                "(an unloaded plugin cannot perform network egress or billing)",
            )
        prompt = build_card_prompt(summary)
        size = self.size  # canonicalised in __post_init__
        try:
            status, raw = self.transport(
                self._endpoint(), self._headers(), self._build_body(prompt), self.timeout
            )
        except ModelAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised at the vendor seam
            raise classify_transport_error(exc, self.timeout) from exc
        if status != 200:
            # A failed call must not inherit the previous success's usage:
            # cost readers would otherwise bill this failure at the last
            # success's rate.
            self.last_usage = {}
            self.last_vendor_ref = {}
            raise classify_http_status(status, raw)
        try:
            image_bytes, media_type, ref = self._map_response(raw)
        except Exception:
            self.last_usage = {}
            self.last_vendor_ref = {}
            raise
        self.last_vendor_ref = ref
        self.last_usage = dict(ref.get("usage", {}))
        metadata = {
            "model": self.generator_id(),
            "prompt": prompt,
            # The OpenAI-compatible API accepts no seed: record that honestly
            # instead of forging a seed that would imply determinism.
            "seed": None,
            "version": 1,
            "seed_supported": False,
            "seed_note": "the vendor API accepts no seed; generation is not deterministic",
            "rebuildable": False,
            "renderer": "vendor-image-model",
            "size": size,
            "abstract": self.abstract,
            "no_faces": self.no_faces,
            "usage": dict(ref.get("usage", {})),
            "vendor_ref": ref,
        }
        return GeneratedImage(image_bytes=image_bytes, media_type=media_type, metadata=metadata)

    def _map_response(self, raw: bytes) -> tuple[bytes, str, dict[str, Any]]:
        """Parse a successful response into (image_bytes, media_type, vendor_ref)."""

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelAdapterError(
                "unknown",
                f"unparseable image generation response: {exc}",
                vendor_ref={"status": 200, "bytes_received": len(raw)},
            ) from exc
        # The vendor_ref is an allowlisted reference, never the body (which
        # carries megabytes of image b64 -- content, but too large and
        # unnecessary for the ledger) and never attacker-controlled text.
        ref = _safe_generation_ref(data)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ModelAdapterError(
                "unknown", "image generation response has no image data", vendor_ref=ref
            )
        item = items[0]
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            # Size cap BEFORE decoding: base64 inflates ~4/3x into bytes, so
            # the string length is checked first and an oversized payload is
            # never amplified into memory (response-bomb defence).
            if len(b64) > _MAX_B64_CHARS:
                raise ModelAdapterError(
                    "invalid_request",
                    f"image payload exceeds the {_MAX_IMAGE_BYTES}-byte limit",
                    vendor_ref=ref,
                )
            try:
                image_bytes = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ModelAdapterError(
                    "unknown", "image payload is not valid base64", vendor_ref=ref
                ) from exc
            if len(image_bytes) > _MAX_IMAGE_BYTES:
                raise ModelAdapterError(
                    "invalid_request",
                    f"image payload exceeds the {_MAX_IMAGE_BYTES}-byte limit",
                    vendor_ref=ref,
                )
            raw_media_type = item.get("mime_type") or item.get("media_type") or "image/png"
            try:
                media_type = _coerce_media_type(raw_media_type)
            except ModelAdapterError as exc:
                exc.vendor_ref = ref
                raise
            return image_bytes, media_type, ref
        # Some dall-e-style endpoints return a URL instead of b64.  We refuse
        # to fetch it: a second egress would leave the audited single-endpoint
        # boundary and a URL reference cannot be persisted as image bytes.
        if isinstance(item.get("url"), str):
            raise ModelAdapterError(
                "unknown",
                "vendor returned an image URL, not inline data; URL fetching is refused "
                "(single audited egress only)",
                vendor_ref=ref,
            )
        raise ModelAdapterError(
            "unknown", "image generation response has neither b64_json nor url", vendor_ref=ref
        )

# ---------------------------------------------------------------------------
# Plugin packaging: the generator crystallises into a loadable plugin.
# ---------------------------------------------------------------------------

def load_image_gen_plugin(runtime: Any, **adapter_kwargs: Any) -> OpenAIImageGenAdapter:
    """Load the image generator through a PluginRuntime and return it.

    A vendor capability crystallises into a plugin: the manifest is validated
    and the load audited (``plugin.loaded``) before the generator is handed
    back.  The plugin contributes no ToolRegistry tools -- it is a generator
    injected into the taste-card pipeline, not a model-visible tool -- so the
    manifest honestly declares the real side effects (``network-egress`` and
    ``billing``) that ``max_permission`` cannot cover.  ``build()`` returns an
    empty contribution list, which the runtime accepts: a zero-tool plugin
    simply registers nothing and unloads cleanly.

    The side effects are *bound to the lifecycle*: the returned adapter is
    activated only after ``runtime.load()`` succeeds, and the plugin's unload
    hook deactivates it -- so an unloaded plugin's leaked adapter handle cannot
    keep performing network egress or billing.
    """

    adapter = OpenAIImageGenAdapter(**adapter_kwargs)

    from .plugins import Plugin, PluginManifest

    plugin = Plugin(
        manifest=PluginManifest(
            id=f"imagegen-openai-{adapter.resolved_model_id()}",
            version="1.0.0",
            capabilities=("image-generation",),
            max_permission="read",
            side_effects=("network-egress", "billing"),
        ),
        build=lambda: [],
        unload=adapter._mark_plugin_unloaded,
    )
    runtime.load(plugin)
    # Only activate after the audited load succeeded: egress and billing are
    # gated on the plugin being live.
    adapter._mark_plugin_loaded()
    return adapter
