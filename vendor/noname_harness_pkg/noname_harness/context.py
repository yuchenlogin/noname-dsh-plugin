"""Human-readable rendering for a machine-readable context package."""

from __future__ import annotations

import json
import re
from typing import Any


def _inline(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _single_line(text: str) -> str:
    """Collapse whitespace/newlines so free text stays on one markdown line."""

    return " ".join(str(text).split())


def _fence_for(content: str) -> str:
    """Choose a Markdown fence longer than any backtick run in ``content``."""

    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def render_markdown(package: dict[str, Any]) -> str:
    """Render a context package as a compact handoff document.

    The JSON package remains the canonical projection.  Markdown is only a
    readable view for a person or a model starting a fresh session.
    """

    project = package["project"]
    layers = package["layers"]
    lines = [
        "# NoName Context Package",
        "",
        f"- Project: `{project['name']}`",
        f"- Workspace: `{project['workspace_root']}`",
        # The task is caller-supplied free text; collapse any newlines so it
        # can never inject markdown structure (headings, lists) into the
        # document header.  It is metadata, not content the model should parse.
        f"- Task: {_single_line(package['task'])}",
        f"- Package: `{package['package_id']}`",
        "",
        "## High layer · stable project state",
        "",
    ]
    if layers["high"]:
        for item in layers["high"]:
            lines.extend(
                [
                    f"### `{item['logical_key']}`",
                    f"{_inline(item['content'])}",
                    f"Source events: {', '.join(item['source_event_ids'])}",
                    "",
                ]
            )
    else:
        lines.extend(["_No accepted high-layer state yet._", ""])

    lines.extend(["## Mid layer · current work state", ""])
    if layers["mid"]:
        for item in layers["mid"]:
            lines.extend(
                [
                    f"### `{item['logical_key']}`",
                    f"{_inline(item['content'])}",
                    f"Source events: {', '.join(item['source_event_ids'])}",
                    "",
                ]
            )
    else:
        lines.extend(["_No accepted mid-layer state yet._", ""])

    lines.extend(["## Low layer · recent evidence", ""])
    if layers["low"]:
        # The package keeps work evidence newest-first for machine consumers,
        # with the handoff snapshot appended as an explicit marker.  Sort by
        # its recorded event position for the human-readable chronological
        # view instead of assuming the list is a pure reverse timeline.
        chronological_low = sorted(
            layers["low"],
            key=lambda item: (item.get("occurred_at", ""), item.get("seq", 0), item.get("event_id", "")),
        )
        for item in chronological_low:
            lines.extend(
                [
                    f"- `{item['event_type']}` (session `{item['session_id']}`, seq {item['seq']})",
                    f"  payload: `{_inline(item['payload'])}`",
                    f"  event: `{item['event_id']}`",
                ]
            )
            for evidence in item["evidence"]:
                source = evidence["artifact_uri"] or "inline evidence"
                lines.append(f"  evidence: `{source}` · `{evidence['content_hash'][:12]}`")
                if evidence.get("content"):
                    # Keep the default handoff view self-contained: a fresh
                    # agent should be able to see the captured diff/output,
                    # not only an opaque hash.  Choose a fence longer than
                    # any backtick run in the captured text so raw evidence
                    # cannot terminate the surrounding code block.
                    content = evidence["content"]
                    fence = _fence_for(content)
                    content_lines = content.splitlines()
                    lines.append("  evidence content:")
                    lines.append(f"    {fence}text")
                    lines.extend(f"    {line}" for line in content_lines)
                    if not content_lines:
                        lines.append("    ")
                    lines.append(f"    {fence}")
            lines.append("")
    else:
        lines.extend(["_No recent evidence._", ""])

    # Taste is rendered in its own section, explicitly labelled as a soft
    # influence.  It never appears inside the factual layers above.
    preference = package.get("preference")
    lines.extend(["", "## Preference · taste (soft influence, not fact)", ""])
    if preference and (preference["tracks"]["authored"] or preference["tracks"]["adopted"]):
        lines.append(f"_{preference['note']}_")
        lines.append("")
        for track in ("authored", "adopted"):
            items = preference["tracks"][track]
            if not items:
                continue
            label = "Authored · your own words" if track == "authored" else "Adopted · chosen from model moments"
            lines.extend([f"### {label}", ""])
            for item in items:
                # Taste content is rendered inside a fenced block, never as
                # inline markdown.  Even though JSON encoding already flattens
                # newlines, a fence makes it impossible for taste text to ever
                # be reinterpreted as document structure (headings, lists,
                # code-fence boundaries) -- the fact/attitude boundary holds in
                # the rendered layer too, not only in the data model.
                content_text = _inline(item["content"])
                fence = _fence_for(content_text)
                lines.append(
                    f"- scope `{item['scope']}`, status `{item['status']}`, "
                    f"taste `{item['id']}`:"
                )
                lines.append(f"  {fence}json")
                lines.append(f"  {content_text}")
                lines.append(f"  {fence}")
            lines.append("")
    else:
        lines.append("_No active taste yet._")
        lines.append("")

    lines.extend(["", "## Next-step candidates · advisory", ""])
    if package.get("next_step_candidates"):
        for candidate in package["next_step_candidates"]:
            sources = ", ".join(candidate["source_event_ids"]) or "none"
            lines.append(
                f"- {candidate['text']} (confidence {candidate['confidence']:.2f}; "
                f"reason: {candidate['reason']}; source: {sources})"
            )
    else:
        lines.append("_No heuristic next-step candidate._")

    lines.extend(["", "## Pending review", ""])
    if package["pending_review"]:
        for item in package["pending_review"]:
            conflicts = ", ".join(item["conflict_with_ids"]) or "none"
            lines.append(
                f"- `{item['layer']}/{item['logical_key']}` proposal `{item['id']}` "
                f"from {', '.join(item['source_event_ids'])}; "
                f"content: `{_inline(item['content'])}`; conflicts: `{conflicts}`; "
                f"reason: `{item.get('reason') or 'not supplied'}`"
            )
    else:
        lines.append("_Nothing is waiting for review._")

    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Durable state is reviewable and versioned; it is not silently rewritten.",
            "- Recent details remain traceable to append-only events and evidence.",
            f"- Workspace boundary: `{package['guardrails']['workspace_root']}`",
            f"- Workspace policy: {package['guardrails'].get('workspace_policy', 'stay within the configured workspace root')}.",
            "",
            "## Provenance",
            "",
            f"- Event ids: {', '.join(package['provenance']['event_ids']) or 'none'}",
            f"- Evidence ids: {', '.join(package['provenance'].get('evidence_ids', [])) or 'none'}",
            f"- State revision ids: {', '.join(package['provenance']['state_revision_ids']) or 'none'}",
            "",
        ]
    )
    return "\n".join(lines)
