"""State Diff: why is it like this now -- a projection, never a new fact.

The State Diff answers "现在为何如此": for each durable-state key, show its
version evolution -- which revision added it, which retired it, and what new
content superseded the old (docs/ledger.md §2.3).  It also shows the taste-card
evolution (accept -> edit -> pause -> retire chains).

Everything is derived from the supersedes chains in ``state_revisions`` and
``taste_cards`` -- pure projection, rebuildable, stores nothing new.
"""

from __future__ import annotations

import html
import json
from typing import Any

from .store import HarnessStore
from .taste_cards import TasteCardService


def build_state_diff_model(store: HarnessStore) -> dict[str, Any]:
    """Assemble the State Diff view-model (a pure projection)."""

    # For every durable-state key that has ANY history -- not just active ones.
    # A retired key is exactly the "旧记忆被新证据取代/失效" story the State
    # Diff exists to show, so it must not be dropped just because it is no
    # longer in the active projection.
    all_keys = store.query(
        "SELECT DISTINCT layer, logical_key FROM state_revisions ORDER BY layer, logical_key"
    )
    key_chains: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in all_keys:
        layer = row["layer"]
        logical_key = row["logical_key"]
        key = (layer, logical_key)
        if key in key_chains:
            continue
        history = store.state_history(layer, logical_key)
        key_chains[key] = history

    canon_diffs = []
    for (layer, logical_key), history in sorted(key_chains.items()):
        versions = []
        for index, revision in enumerate(history):
            number = index + 1
            delta = None
            if index > 0:
                previous = history[index - 1]
                delta = _describe_delta(previous, revision)
            versions.append(
                {
                    "number": number,
                    "status": revision["status"],
                    "content": revision["content"],
                    "approved_by": revision["approved_by"],
                    "valid_from": revision["valid_from"],
                    "valid_to": revision["valid_to"],
                    "created_at": revision["created_at"],
                    "delta_from_previous": delta,
                    "is_head": index == len(history) - 1,
                }
            )
        canon_diffs.append(
            {
                "layer": layer,
                "logical_key": logical_key,
                "versions": versions,
                "version_count": len(versions),
            }
        )

    # Taste-card evolution chains.  Heads are found in ONE query (the same
    # supersedes-set filter _head_rows uses), not four full-table scans; each
    # chain is then walked with single-row lookups (no redundant projections).
    cards_service = TasteCardService(store)
    all_card_rows = store.query("SELECT * FROM taste_cards")
    superseded_ids = {row["supersedes_id"] for row in all_card_rows if row["supersedes_id"]}
    heads = [row for row in all_card_rows if row["id"] not in superseded_ids]
    card_chains = []
    for head_row in heads:
        chain = _card_chain(cards_service, cards_service.get(head_row["id"]))
        if chain:
            card_chains.append(chain)

    return {
        "canon_diffs": canon_diffs,
        "card_chains": card_chains,
        "counts": {"canon_keys": len(canon_diffs), "card_chains": len(card_chains)},
    }


def _describe_delta(previous: dict[str, Any], current: dict[str, Any]) -> str:
    """A short human description of what changed between two revisions."""

    if current["status"] == "retired":
        # A retire built from a *different* proposal content than the current
        # head is a silent content mutation on the way out -- surface it, do
        # not hide it behind the bare "retired" label.
        if previous["content"] != current["content"]:
            return "失效（内容同时变更，保留历史）"
        return "失效（retired，保留历史）"
    if previous["content"] != current["content"]:
        return "内容被新版本取代"
    return "状态更新"


def _card_chain(cards_service: TasteCardService, head: dict[str, Any]) -> dict[str, Any] | None:
    """Follow a card's supersedes chain back to the original, oldest first."""

    lineage = [head]
    current = head
    seen = {head["id"]}
    broken = False
    while current.get("supersedes_id"):
        parent_id = current["supersedes_id"]
        if parent_id in seen:
            broken = True
            break
        try:
            parent = cards_service.get(parent_id)
        except KeyError:
            broken = True
            break
        seen.add(parent_id)
        lineage.append(parent)
        current = parent
    lineage.reverse()
    if not lineage:
        return None
    root_title = lineage[0]["title"] if lineage else head["title"]
    display_title = (
        root_title if root_title == head["title"] else f"{root_title} → {head['title']}"
    )
    return {
        "title": display_title,
        "broken_lineage": broken,
        "head_status": head["status"],
        "versions": [
            {
                "number": index + 1,
                "status": card["status"],
                "title": card["title"],
                "attitude": card["attitude"],
                "last_confirmed_at": card["last_confirmed_at"],
                "is_head": index == len(lineage) - 1,
            }
            for index, card in enumerate(lineage)
        ],
    }


def render_state_diff_html(model: dict[str, Any]) -> str:
    """Render the State Diff as an HTML fragment (progressive disclosure)."""

    def esc(value: Any) -> str:
        return html.escape(json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value)

    rows = []
    for diff in model["canon_diffs"]:
        version_rows = []
        for version in diff["versions"]:
            head_mark = ' <span class="badge">当前</span>' if version["is_head"] else ""
            status_class = "retired" if version["status"] == "retired" else "active"
            if version.get("broken_lineage"):
                delta = '<span class="delta broken">← 链断裂（provenance 不完整）</span>'
            elif version["delta_from_previous"]:
                delta = f'<span class="delta">← {html.escape(version["delta_from_previous"])}</span>'
            elif version["status"] == "retired":
                # A first version that is already retired must not be labelled
                # "added" -- it was dead on arrival.
                delta = '<span class="delta">← 新增即失效</span>'
            else:
                delta = '<span class="delta added">← 新增</span>'
            
            validity = ""
            if version["valid_from"] or version["valid_to"]:
                validity = (
                    f'<span class="meta">valid: {html.escape(str(version["valid_from"] or "…"))} '
                    f'→ {html.escape(str(version["valid_to"] or "…"))}</span>'
                )
            version_rows.append(
                f'<li class="version {status_class}">'
                f'<span class="vnum">v{version["number"]}</span>'
                f'<span class="vstatus">{html.escape(version["status"])}</span>'
                f"{head_mark}{delta}"
                f'<div class="payload">{esc(version["content"])}</div>'
                f'<div class="meta">{html.escape(version["approved_by"])} · {html.escape(version["created_at"])} {validity}</div>'
                f"</li>"
            )
        rows.append(
            f'<li class="diff-group">'
            f"<details>"
            f'<summary><strong>{html.escape(diff["layer"])}/{html.escape(diff["logical_key"])}</strong> '
            f'<span class="meta">{diff["version_count"]} 个版本</span></summary>'
            f'<ul class="versions">{"".join(version_rows)}</ul>'
            f"</details>"
            f"</li>"
        )

    card_rows = []
    for chain in model["card_chains"]:
        version_rows = []
        for version in chain["versions"]:
            head_mark = ' <span class="badge">当前</span>' if version["is_head"] else ""
            version_rows.append(
                f'<li class="version {version["status"]}">'
                f'<span class="vnum">v{version["number"]}</span>'
                f'<span class="vstatus">{html.escape(version["status"])}</span>{head_mark}'
                f'<div class="payload">{esc(version["attitude"])}</div>'
                f'<div class="meta">确认于 {html.escape(version["last_confirmed_at"])}</div>'
                f"</li>"
            )
        card_rows.append(
            f'<li class="diff-group">'
            f"<details>"
            f'<summary><strong>品味卡「{html.escape(chain["title"])}」</strong> '
            f'<span class="meta">{html.escape(chain["head_status"])}</span></summary>'
            f'<ul class="versions">{"".join(version_rows)}</ul>'
            f"</details>"
            f"</li>"
        )

    return (
        '<section class="state-diff">'
        "<h2>版本演进 · 现在为何如此</h2>"
        '<p class="meta">法典与品味卡的版本链：哪版新增、哪版失效、被什么取代。</p>'
        f'<ul class="diff-list">{"".join(rows) or "<p class=empty>暂无法典版本</p>"}'
        f'{"".join(card_rows)}</ul>'
        "</section>"
    )
