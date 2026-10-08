"""Card images: an optional visual metaphor for a taste card, never evidence.

A taste card may carry a *visual explanation* -- a metaphor that helps a person
ask "is this still me?"  The multi-modal risk contract (docs/taste-cards.md §7)
is non-negotiable, so this module enforces it structurally:

- **An image is always labelled a visual explanation**, never evidence.  It is
  stored only as rebuildable metadata (model / prompt / seed / version) plus
  the rendered bytes; it is never used for retrieval, ranking, or to infer
  taste back from pixels.
- **No sensitive visual inference.**  The default renderer is pure abstract
  typography -- no faces, no photographs, no demographic cues -- so there is
  nothing to mis-read as personality.
- **The generator is injectable.**  A real image model (an imagegen plugin)
  plugs in behind the same protocol; the default is a deterministic,
  network-free typographic renderer so the visual layer works and is
  verifiable offline.  The user can always choose pure typography (no image).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Protocol

# An image generator maps a structured card summary to (image_bytes, metadata).
ImageGenerator = Callable[[dict[str, Any]], "GeneratedImage"]


@dataclass(frozen=True)
class GeneratedImage:
    """A rendered card image plus its rebuildable metadata contract."""

    image_bytes: bytes
    media_type: str  # e.g. "image/svg+xml"
    metadata: dict[str, Any]  # model / prompt / seed / version (+ renderer notes)


def image_metadata_contract(generator_id: str, prompt: str, seed: int, version: int) -> dict[str, Any]:
    """The rebuildable metadata contract recorded on the card."""

    return {
        "model": generator_id,
        "prompt": prompt,
        "seed": seed,
        "version": version,
    }


def _sanitize_text(value: Any) -> str:
    """Coerce to str and drop lone surrogates (invalid Unicode).

    A corrupted/hostile card can carry lone surrogates (e.g. 'surrog\\ud800ate')
    that no UTF-8 encoder can represent.  Every render seam (seed hashing,
    SVG text) must sanitise rather than crash: encode+replace rewrites each
    undecodable sequence (lone surrogate, control char) to ASCII ``?``,
    keeping the output deterministic and valid XML.
    """

    return str(value).encode("utf-8", errors="replace").decode("utf-8")

def _seed_for(summary: dict[str, Any]) -> int:
    """A deterministic seed from the card content (reproducible).

    The seed only needs determinism, not losslessness, so surrogates are
    sanitised before hashing.
    """

    key = "|".join(
        _sanitize_text(summary.get(field, ""))
        for field in ("title", "attitude", "track")
    )
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") % (2**31)


def local_typographic_image(
    summary: dict[str, Any], *, generator_id: str = "local-typographic-v1"
) -> GeneratedImage:
    """A deterministic, network-free typographic card image (pure SVG).

    Renders the card's title and attitude as restrained abstract typography --
    the same quiet aesthetic as the product site -- with no faces, no photos,
    nothing to mis-read.  Deterministic given the same summary, so it is fully
    rebuildable from its metadata.
    """

    # Sanitise BEFORE truncating/escaping: a lone surrogate is not encodable
    # UTF-8 and is never legal in XML 1.0 output, no matter where it lands.
    title = _sanitize_text(summary.get("title", ""))[:40]
    attitude = _sanitize_text(summary.get("attitude", ""))[:120]
    track = _sanitize_text(summary.get("track", ""))
    seed = _seed_for(summary)
    # A deterministic accent rotation from the seed (subtle, not loud).
    hue = 10 + (seed % 30)  # warm accent range
    prompt = f"typographic metaphor for taste card '{title}' ({track}): {attitude}"
    metadata = {
        **image_metadata_contract(generator_id, prompt, seed, 1),
        "renderer": "typographic-svg",
        "abstract": True,
        "no_faces": True,
        "media": "typography",
    }
    svg = _render_svg(title=title, attitude=attitude, track=track, hue=hue)
    return GeneratedImage(
        image_bytes=svg.encode("utf-8"),
        media_type="image/svg+xml",
        metadata=metadata,
    )


def _xml_safe(text: str) -> str:
    """Escape for XML text context AND drop characters illegal in XML 1.0.

    html.escape handles <, >, & but leaves control characters (\\x00 etc.)
    that make the document malformed XML.  Strip everything outside the XML
    1.0 legal set so the SVG is always well-formed.
    """

    import html

    escaped = html.escape(text)
    return "".join(
        ch
        for ch in escaped
        if ch in ("\t", "\n", "\r")
        or (0x20 <= ord(ch) and not 0xD800 <= ord(ch) <= 0xDFFF)
    )


def _render_svg(*, title: str, attitude: str, track: str, hue: int) -> str:
    # Escape AFTER .upper(): escaping first would corrupt entities (e.g. an
    # escaped "&amp;" becomes "&AMP;" on .upper(), breaking the reference).
    t = _xml_safe(title)
    a = _xml_safe(attitude)
    tr = _xml_safe(track.upper())
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">
  <rect width="640" height="400" fill="#0b0d10"/>
  <circle cx="540" cy="70" r="180" fill="hsl({hue},55%,32%)" opacity="0.12"/>
  <circle cx="80" cy="340" r="140" fill="hsl({hue},55%,32%)" opacity="0.07"/>
  <text x="48" y="80" fill="#e07a5f" font-family="ui-monospace,monospace" font-size="13" letter-spacing="6">{tr}</text>
  <text x="48" y="170" fill="#f2efe9" font-family="Georgia,serif" font-size="44" font-weight="bold">{t}</text>
  <line x1="48" y1="200" x2="240" y2="200" stroke="#e07a5f" stroke-width="1"/>
  <text x="48" y="250" fill="#f2efe9" fill-opacity="0.72" font-family="Georgia,serif" font-size="20">{a}</text>
  <text x="48" y="372" fill="#8a8f98" font-family="ui-monospace,monospace" font-size="11">视觉解释 · 非事实 · visual explanation, not evidence</text>
</svg>"""


def card_image_for(
    card: dict[str, Any], generator: ImageGenerator | None = None
) -> GeneratedImage:
    """Generate the visual metaphor for a card via the injectable generator.

    The summary passed to the generator is the *text* card (title / attitude /
    track), so even a real image model works from the same explainable text
    evidence -- never from the user's other data.
    """

    generate = generator or local_typographic_image
    summary = {
        "title": card.get("title", ""),
        "attitude": card.get("attitude", ""),
        "track": card.get("track", ""),
    }
    return generate(summary)
