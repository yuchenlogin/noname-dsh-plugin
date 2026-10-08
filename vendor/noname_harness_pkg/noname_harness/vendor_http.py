"""Shared, credential-safe HTTP plumbing for vendor model adapters.

Both the OpenAI and Anthropic adapters (and any future vendor) need exactly
the same hard-won guarantees: no redirect-following (which would forward
credentials to another host), HTTPS-only base URLs by default, error bodies
that never reach the ledger, and cause-based error classification.  Those live
here once, so the guarantees cannot drift between vendors.  Vendor adapters
supply only their *differences*: request building and response parsing.
"""

from __future__ import annotations

import json
import math
import socket
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

import re

from .adapters import ModelAdapterError

# snake_case identifier shape for a trusted error enum token.
_ERROR_CODE_SHAPE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

# A vendor error code is attacker-controlled text.  A *shape* allowlist is not
# enough: a credential with its known prefix stripped (e.g. the 32-char
# lowercase body of an API key) matches the shape perfectly, so a hostile
# endpoint could echo secrets into the ledger under the "error_code" name.
# Only *known* enum tokens are recorded; anything else (even well-shaped) is
# collapsed to "unlisted" so the classification signal survives without
# carrying attacker entropy.
_KNOWN_ERROR_CODES = frozenset(
    {
        # OpenAI-style codes / types.
        "invalid_api_key",
        "incorrect_api_key",
        "authentication_error",
        "rate_limit_exceeded",
        "insufficient_quota",
        "model_not_found",
        "invalid_request_error",
        "content_policy_violation",
        "server_error",
        "overloaded",
        "billing_hard_limit_reached",
        # Anthropic-style error types.
        "api_error",
        "invalid_request",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "request_too_large",
        "timeout_error",
        "billing_error",
    }
)

_UNLISTED_ERROR_CODE = "unlisted"

# Numeric fields from a vendor response (usage counters, `created`) must be
# real, bounded ints: a malicious endpoint returning negatives, bools or
# 2**63-scale values would poison cost accounting that aggregates them.
MAX_VENDOR_INT = 10**12

def bounded_vendor_int(value: Any) -> int | None:
    """Coerce a vendor numeric field, refusing bools/negatives/huge values."""

    if type(value) is int and 0 <= value <= MAX_VENDOR_INT:
        return value
    return None

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

def validate_timeout(timeout: Any) -> float:
    """Return a finite, strictly positive timeout or fail loudly.

    A zero/negative/NaN/inf timeout has undefined behaviour in urllib and
    silently broken retry semantics everywhere else.
    """

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ModelAdapterError(
            "invalid_request", f"timeout must be a number, got {type(timeout).__name__}"
        )
    value = float(timeout)
    if math.isnan(value) or math.isinf(value) or value <= 0:
        raise ModelAdapterError(
            "invalid_request", f"timeout must be finite and > 0, got {value!r}"
        )
    return value

# A transport maps (url, headers, body_bytes, timeout) -> (status, response_bytes).
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]

# The only usage fields ever preserved in a vendor_ref (allowlist, coerced int).
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: following one would forward the Authorization /
    x-api-key credential to whatever host the redirect points at."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def secure_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    """A credential-safe POST transport (stdlib only).

    Redirects are refused so credentials only ever reach the configured
    endpoint.  Failures are classified by cause: a true timeout is retryable;
    DNS/connection/TLS failures are not.
    """

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ModelAdapterError(
                "invalid_request",
                f"endpoint redirected ({exc.code}); redirects are refused to protect credentials",
            ) from exc
        return exc.code, exc.read()
    except (socket.timeout, TimeoutError) as exc:
        raise ModelAdapterError("timeout", f"request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ModelAdapterError("timeout", f"request timed out after {timeout}s") from exc
        raise ModelAdapterError(
            "overloaded", f"cannot reach the endpoint: {type(exc.reason).__name__}"
        ) from exc


def classify_transport_error(exc: Exception, timeout: float) -> ModelAdapterError:
    """Classify a transport-layer exception by cause (used for injected transports)."""

    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return ModelAdapterError("timeout", f"request timed out after {timeout}s")
    if isinstance(exc, urllib.error.URLError):
        return ModelAdapterError("overloaded", f"cannot reach the endpoint: {type(cause).__name__}")
    return ModelAdapterError("unknown", f"transport failed: {type(exc).__name__}")


def validate_base_url(base: str, *, allow_insecure: bool) -> str:
    """Return a normalised base URL, enforcing HTTPS unless opted out.

    The base URL is a trust boundary: the credential is sent to whatever host
    it names.  Plaintext or non-HTTP(S) schemes are refused unless
    ``allow_insecure`` is set (e.g. a local model server).
    """

    # Check BEFORE strip(): str.strip() also removes \x0b-\x0d, which would
    # otherwise launder a control character out of the checked string.
    if _CONTROL_CHARS.search(base):
        raise ModelAdapterError(
            "auth", "refusing base URL with control characters (trust boundary violation)"
        )
    stripped = base.strip()
    parts = urlsplit(stripped.lower())
    scheme = parts.scheme
    allowed = {"https"} if not allow_insecure else {"https", "http"}
    if scheme not in allowed:
        raise ModelAdapterError(
            "auth",
            f"refusing base URL scheme {scheme!r} (would send credentials over a "
            "non-HTTPS channel); pass allow_insecure=True for a local plaintext endpoint",
        )
    # The credential is sent to whatever this URL names: embedded userinfo or a
    # trailing-dot host would smuggle credentials into the URL itself or bypass
    # host comparisons.
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise ModelAdapterError(
            "auth", "refusing base URL with embedded userinfo (credential smuggling risk)"
        )
    hostname = parts.hostname or ""
    if hostname.endswith("."):
        raise ModelAdapterError(
            "auth", "refusing base URL with a trailing-dot host (trust boundary violation)"
        )
    return base.rstrip("/")


def safe_error_ref(status: int, raw: bytes) -> dict[str, Any]:
    """A vendor_ref for an HTTP error: a *reference*, never the body.

    An error body can contain anything -- including, for a 401, the credential
    itself -- and would be persisted verbatim into the append-only ledger.  Only
    the status and a short vendor error code are kept.
    """

    ref: dict[str, Any] = {"status": status}
    try:
        body = json.loads(raw.decode("utf-8"))
        error = body.get("error") if isinstance(body, dict) else None
        code: Any = None
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
        elif isinstance(body, dict):
            code = body.get("type")
        # An error_code is attacker-controlled text; only keep tokens from the
        # known vendor enum.  A well-shaped but unknown token (e.g. a stripped
        # credential echoed back by a hostile endpoint) is recorded as
        # "unlisted": the "the vendor did name an error" signal survives
        # without attacker entropy ever reaching the ledger.
        if isinstance(code, str) and _ERROR_CODE_SHAPE.match(code):
            ref["error_code"] = code if code in _KNOWN_ERROR_CODES else _UNLISTED_ERROR_CODE
    except (ValueError, UnicodeDecodeError):
        pass
    return ref


def safe_usage_ref(usage: Any) -> dict[str, int]:
    """The allowlisted numeric usage fields from a vendor usage object."""

    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in _USAGE_FIELDS:
        value = bounded_vendor_int(usage.get(key))
        if value is not None:
            result[key] = value
    return result


def json_schema_type(type_name: str) -> str:
    """Map a harness input-schema type name to a JSON-schema type."""

    return {
        "string": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "boolean",
        "object": "object",
        "array": "array",
    }.get(type_name, "string")


def word_count_cost(model_id: str, request: Any, currency: str = "usd") -> dict[str, Any]:
    """A shared, honest cost estimate from word count (not real tokens)."""

    input_words = sum(len(m.text().split()) for m in request.messages)
    return {
        "model_id": model_id,
        "estimated_input_words": input_words,
        "currency": currency,
        "note": "estimate from word count, not real tokens; real cost from vendor usage in vendor_ref",
    }


def classify_http_status(status: int, raw: bytes) -> ModelAdapterError:
    """Map an HTTP status to a unified error class with a safe vendor_ref."""

    ref = safe_error_ref(status, raw)
    if status in {401, 403}:
        return ModelAdapterError("auth", f"vendor auth failed ({status})", vendor_ref=ref)
    if status == 429:
        return ModelAdapterError("rate_limit", "vendor rate limit (429)", vendor_ref=ref)
    if status in {500, 502, 503, 504}:
        return ModelAdapterError("overloaded", f"vendor overloaded ({status})", vendor_ref=ref)
    if status == 400:
        return ModelAdapterError("invalid_request", "vendor rejected request (400)", vendor_ref=ref)
    return ModelAdapterError("unknown", f"vendor error ({status})", vendor_ref=ref)

# A stream transport maps (url, headers, body_bytes, timeout) -> an iterator of
# raw bytes lines (an SSE byte stream).  Tests inject a deterministic replay.
StreamTransport = Callable[[str, dict[str, str], bytes, float], Any]


def secure_stream_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
    """The default real SSE transport: a streaming POST that yields raw lines.

    Redirects are refused (credentials never forwarded) and the response body
    is read line by line as it arrives, so a real SSE stream is consumed
    incrementally rather than buffered whole.  Errors mid-stream are raised as
    classified ModelAdapterError.
    """

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ModelAdapterError(
                "invalid_request",
                f"endpoint redirected ({exc.code}); redirects are refused to protect credentials",
            ) from exc
        raise classify_http_status(exc.code, exc.read()) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise ModelAdapterError("timeout", f"request timed out after {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ModelAdapterError("timeout", f"request timed out after {timeout}s") from exc
        raise ModelAdapterError(
            "overloaded", f"cannot reach the endpoint: {type(exc.reason).__name__}"
        ) from exc

    def _lines() -> Any:
        try:
            with response:
                for raw_line in response:
                    yield raw_line
        except (socket.timeout, TimeoutError) as exc:
            raise ModelAdapterError("timeout", f"stream timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            raise ModelAdapterError(
                "overloaded", f"stream connection failed: {type(exc.reason).__name__}"
            ) from exc

    return _lines()


def iter_sse_json_lines(lines: Any) -> Any:
    """Parse a vendor wire-format SSE stream into JSON payloads.

    This is deliberately scoped to the OpenAI/Anthropic wire format: **one JSON
    object per ``data:`` line**, ``data: [DONE]`` terminates.  It is NOT a full
    SSE-spec parser (the spec also allows an event to span multiple ``data:``
    lines joined by newlines) -- neither OpenAI nor Anthropic emits multi-line
    data today, so the simpler line-based shape is used and documented here.

    Handles CRLF, leading/trailing whitespace, and a UTF-8 BOM.  Comment lines
    (``:``), ``event:`` lines and blanks are skipped; a malformed ``data:``
    line raises a classified error rather than crashing the consumer.
    """

    for raw_line in lines:
        try:
            line = raw_line.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            continue
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except ValueError as exc:
            raise ModelAdapterError(
                "unknown", f"malformed SSE data line: {exc}", vendor_ref={"line": payload[:100]}
            ) from exc


# Backwards-compatible alias for the line-based vendor wire parser.
iter_sse = iter_sse_json_lines

