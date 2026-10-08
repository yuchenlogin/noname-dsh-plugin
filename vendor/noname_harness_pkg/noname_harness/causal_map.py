"""Causal map: why did this happen -- a projection, never a new fact.

The causal map answers "为什么这样做": pick a result, and see what it depends
on -- the user instruction, the canon it cites, the evidence it selected, the
model recipe and route reason, the tool approval behind it.  It lets a person
tell whether an error came from evidence, memory, routing, the model, or a
tool.

Everything here is derived from the append-only event stream and existing
provenance (source_event_ids, origin_proposal_id, approved_by, recipe ids) --
it can be rebuilt at any time and stores nothing new.

**Taste boundary (non-negotiable):**  taste may appear in the causal map ONLY
labelled as "影响了排序/表达" (a soft influence on ordering/expression) --
never as a factual justification.  This is what lets the map distinguish "an
attitude shaped the phrasing" from "a fact supported the conclusion".
"""

from __future__ import annotations

import html
import json
from typing import Any

from .store import HarnessStore
from .taste import TasteService


def build_causal_model(store: HarnessStore, *, session_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Assemble the causal-map view-model from the store (a pure projection)."""

    events = store.list_events(session_id=session_id, limit=limit)
    by_id = {event.id: event for event in events}

    def event_label(event_id: str) -> dict[str, Any]:
        # Provenance resolution must not be limited by the display window:
        # durable source events are looked up individually by id, so a canon
        # created long ago (or in another session) still resolves its "why".
        event = by_id.get(event_id)
        if event is None:
            try:
                event = store.get_event(event_id)
            except KeyError:
                return {
                    "id": event_id[:12],
                    "label": event_id[:12],
                    "kind": "event",
                    "missing": True,
                    "detail": "（事件不存在）",
                }
        payload_text = json.dumps(event.payload, ensure_ascii=False, default=str)
        if len(payload_text) > 80:
            payload_text = payload_text[:80] + "…"
        return {
            "id": event.id[:12],
            "label": f"{event.event_type}",
            "detail": payload_text,
            "kind": "event",
            "missing": False,
        }

    results: list[dict[str, Any]] = []

    # Results: reviewed canon/task-state revisions (why is this canon true now?).
    for revision in store.active_state("high") + store.active_state("mid"):
        dependencies = [
            {**event_label(event_id), "role": "用户指令/证据"}
            for event_id in revision["source_event_ids"]
        ]
        dependencies.append(
            {
                "id": revision.get("origin_proposal_id") or "",
                "label": "审核提案",
                "kind": "review",
                "role": f"由 {revision.get('approved_by', '?')} 批准",
            }
        )
        results.append(
            {
                "id": revision["id"],
                "title": f"{revision['layer']}/{revision['logical_key']}",
                "kind": "canon",
                "summary": json.dumps(revision["content"], ensure_ascii=False, default=str)[:100],
                "dependencies": dependencies,
            }
        )

    # Taste boundary: an event cited by an ACTIVE taste (the "model moment"
    # that justified an adoption) is a soft influence on ordering/expression,
    # never factual evidence.  Collect those ids up front so the package's
    # dependency list can separate them from real evidence.
    taste_source_ids = {
        event_id
        for record in TasteService(store).active()
        for event_id in record["source_event_ids"]
    }

    # Results: context assemblies (why was this context assembled this way?).
    for event in events:
        if event.event_type != "context.assembled":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        dependencies = []
        for event_id in payload.get("source_event_ids", []):
            if event_id in taste_source_ids:
                # Taste-cited moment: label as soft influence, NOT evidence.
                dependencies.append(
                    {**event_label(event_id), "role": "影响了排序/表达", "kind": "taste"}
                )
            else:
                dependencies.append({**event_label(event_id), "role": "选中的证据/记忆"})
        if payload.get("recipe_id"):
            dependencies.append(
                {
                    "id": payload["recipe_id"],
                    "label": f"模型配方 {payload['recipe_id']}",
                    "kind": "recipe",
                    "role": "模型配方",
                }
            )
        if payload.get("model_id"):
            dependencies.append(
                {"id": payload["model_id"], "label": f"模型 {payload['model_id']}", "kind": "model", "role": "目标模型"}
            )
        results.append(
            {
                "id": payload.get("package_id", event.id),
                "title": f"上下文包 · {str(payload.get('task') or '')[:40]}",
                "kind": "package",
                "summary": f"配方 {payload.get('recipe_id') or '无'}",
                "dependencies": dependencies,
            }
        )

    # Results: gated tool executions (why did this tool run?).
    tool_events = [e for e in events if e.event_type.startswith("tool.")]
    for event in tool_events:
        if event.event_type != "tool.completed":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        dependencies = []
        token_id = payload.get("approval_token_id")
        if token_id:
            # Verify the token against the ledger: a bare token id on a
            # tool.completed event is not proof of a human approval (a forged
            # event could carry any id).  Only label it as human-approved when
            # a matching grant/approval exists in the ledger.
            approver = _verify_approval_token(store, token_id, events)
            if approver is not None:
                dependencies.append(
                    {
                        "id": token_id,
                        "label": "审批令牌",
                        "kind": "approval",
                        "role": f"人工审批（由 {approver} 批准）",
                    }
                )
            else:
                dependencies.append(
                    {
                        "id": token_id,
                        "label": "审批令牌",
                        "kind": "approval",
                        "role": "审批令牌（未在账本中核实）",
                    }
                )
        dependencies.append(
            {"id": event.id[:12], "label": f"工具 {payload.get('name', '?')}", "kind": "tool", "role": "工具结果"}
        )
        results.append(
            {
                "id": event.id,
                "title": f"工具执行 · {payload.get('name', '?')}",
                "kind": "tool",
                "summary": f"{payload.get('elapsed_ms', '?')}ms",
                "dependencies": dependencies,
            }
        )

    # Taste (soft influence, never a factual justification).
    active_taste = TasteService(store).active()
    taste_note = {
        "count": len(active_taste),
        "label": "影响了排序/表达",
        "warning": "品味只影响态度层（排序/表达/取舍），不是事实依据",
    }

    return {
        "session_id": session_id,
        "results": results,
        "taste_note": taste_note,
        "truncated": len(events) >= limit,
        "limit": limit,
        "counts": {"results": len(results), "events": len(events)},
    }


def _verify_approval_token(store: HarnessStore, token_id: str, events: list[Any]) -> str | None:
    """Return the approver id if a matching approval grant exists, else None."""

    for event in store.list_events(session_id=None, limit=100000):
        if event.event_type == "tool.approval_granted" and event.payload.get("token_id") == token_id:
            return event.payload.get("approver_id")
    return None


def render_causal_html(model: dict[str, Any]) -> str:
    """Render the causal map as an HTML fragment (progressive disclosure)."""

    def esc(value: Any) -> str:
        return html.escape(str(value))

    rows = []
    for result in model["results"]:
        dep_items = []
        for dep in result["dependencies"]:
            role = html.escape(dep.get("role", ""), quote=True)
            label = html.escape(dep.get("label", ""), quote=True)
            detail = html.escape(dep.get("detail", ""), quote=True)
            dep_items.append(
                f'<li class="dep dep-{dep["kind"]}">'
                f'<span class="dep-role">{role}</span> '
                f'<span class="dep-label">{label}</span>'
                f'<span class="dep-detail">{detail}</span>'
                f"</li>"
            )
        dep_html = "".join(dep_items) or '<li class="dep"><span class="meta">无显式依赖</span></li>'
        rows.append(
            f'<li class="causal-node">'
            f"<details>"
            f'<summary><span class="badge kind-{result["kind"]}">{html.escape(result["kind"], quote=True)}</span> '
            f"<strong>{esc(result['title'])}</strong> "
            f'<span class="meta">{esc(result["summary"])}</span></summary>'
            f'<ul class="deps">{dep_html}</ul>'
            f"</details>"
            f"</li>"
        )
    taste = model["taste_note"]
    taste_banner = (
        f'<p class="taste-note">品味（{taste["count"]} 条活跃）：<strong>{html.escape(taste["label"])}</strong>'
        f" · {html.escape(taste['warning'])}</p>"
    )
    trunc = (
        f'<p class="trunc">仅显示最近 {model["limit"]} 条事件的结果（历史被截断）</p>'
        if model.get("truncated")
        else ""
    )
    return (
        '<section class="causal-map">'
        "<h2>因果图 · 为什么这样做</h2>"
        f"{trunc}"
        '<p class="meta">点击一个结果，看它依赖什么——识别错误来自证据、记忆、路由、模型还是工具。'
        "品味只标注影响，不是事实依据。</p>"
        f"{taste_banner}"
        f'<ul class="causal-list">{"".join(rows) or "<p class=empty>暂无可追溯的结果</p>"}</ul>'
        "</section>"
    )
