"""Ledger projection: turn the append-only store into an interactive map.

The ledger is *not* a log viewer and never a new fact: everything it shows is
derived from the append-only event stream, the review inbox, and the taste
projection, and can be rebuilt at any time.  This module produces a
self-contained, dependency-free HTML page.  Offline snapshots can be opened
by double-click; the local server enables explicit, authenticated review.
Both modes answer the three questions the ledger exists for:

- **Timeline -- what happened?**  High-signal nodes by default; low-level
  noise is folded away but expandable.
- **State -- why is it like this now?**  Canon, task state and taste, each
  traced back to its source events.
- **Inbox -- what needs my judgement?**  The pending canon/task/taste/card
  candidates, visually distinct from plain browsing.

Visual principles honoured (docs/ledger.md): a clean timeline by default,
progressive disclosure (click a node to drill into its source events), review
actions visually distinct from browsing, colour for event *type* not for
value, and no giant stat panels.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .causal_map import build_causal_model
from .state_diff import build_state_diff_model
from .store import HarnessStore
from .taste import TasteService
from .taste_cards import TasteCardService

# Event types that are LOW-signal bookkeeping, folded into an expandable group
# by default.  The fold set is *inverted* (an explicit low-signal allowlist, not
# a high-signal one): any new event type is visible by default, so the ledger
# fails open toward showing things rather than silently hiding a future
# high-signal event.  These are the types that describe machinery, not work.
_LOW_SIGNAL_PREFIXES = ("loop.", "plugin.build_", "codex.hook.")
_LOW_SIGNAL_EXACT = {
    "file.read", "workspace.snapshot", "model.requested", "model.completed",
    "tool.requested", "tool.completed", "tool.registered", "tool.unregistered",
    "tool.shadowed", "test.passed", "test.completed", "context.assembled",
    "codex.user", "codex.assistant", "dsh.tool.completed",
}


def _is_low_signal(event_type: str) -> bool:
    if event_type in _LOW_SIGNAL_EXACT:
        return True
    return any(event_type.startswith(prefix) for prefix in _LOW_SIGNAL_PREFIXES)

_TYPE_LABELS = {
    "project": "法典", "decision": "决策", "task": "任务", "phase": "阶段",
    "memory": "记忆", "taste": "品味", "tool": "工具", "route": "路由",
    "model": "模型", "loop": "循环", "plugin": "插件", "test": "测试",
    "artifact": "文件", "workspace": "快照", "file": "文件",
}


def _type_group(event_type: str) -> str:
    prefix = event_type.split(".", 1)[0]
    return _TYPE_LABELS.get(prefix, "其它")


def build_ledger_model(
    store: HarnessStore, *, session_id: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    """Assemble the ledger view-model from the store (a pure projection).

    ``limit=None`` (the default) shows the full history; each render is a full
    projection, so there is no incremental-load reason to cap it.  When a limit
    *is* given the model records that the timeline is truncated, so the page can say so
    honestly instead of implying a complete history.
    """

    fetch_limit = limit if limit is not None else 1_000_000
    events = store.list_events(session_id=session_id, limit=fetch_limit)
    truncated = limit is not None and len(events) >= limit
    # list_events is newest-first; present oldest-first for a timeline.
    timeline = []
    folded = 0
    for event in reversed(events):
        low = _is_low_signal(event.event_type)
        if low:
            folded += 1
        timeline.append(
            {
                "seq": event.seq,
                "event_type": event.event_type,
                "group": _type_group(event.event_type),
                "occurred_at": event.occurred_at,
                "session_id": event.session_id,
                "high_signal": not low,
                "payload": event.payload,
                "id": event.id,
            }
        )

    inbox = store.review_inbox()
    taste = TasteService(store)
    cards = TasteCardService(store)
    # The inbox's compact summary is useful for counts, but a signature needs
    # the complete candidate, evidence, and replacement effect.  Keep the
    # authoritative record separate from derived details so the optimistic
    # review version hashes exactly the same object as the local server.
    for section in ("canon_pending", "task_pending", "taste_pending", "card_pending"):
        for item in inbox[section]:
            related_taste = []
            if item["kind"] == "state_proposal":
                record = store.get_proposal(item["id"])
                history = store.state_history(record["layer"], record["logical_key"])
                replaces = history[-1:]  # the current head, including retired heads
            elif item["kind"] == "taste_candidate":
                record = taste.get(item["id"])
                replaces = [taste.get(record["supersedes_id"])] if record["supersedes_id"] else []
            else:
                record = cards.get(item["id"])
                related_taste = [
                    {**taste.get(taste_id), "current_head": taste.current_head(taste_id)}
                    for taste_id in record["taste_ids"]
                ]
                replaces = [cards.get(record["supersedes_id"])] if record["supersedes_id"] else []
            source_ids = list(record.get("source_event_ids", []))
            source_ids.extend(record.get("representative_evidence", []))
            for related in related_taste:
                source_ids.extend(related["source_event_ids"])
                source_ids.extend(related["current_head"]["source_event_ids"])
            sources = []
            for source_id in dict.fromkeys(source_ids):
                try:
                    sources.append(asdict(store.get_event(source_id)))
                except KeyError:
                    # Card representative evidence can contain references
                    # outside the event table; surface that fact explicitly.
                    sources.append({"id": source_id, "unavailable": True})
            item.update({
                "record": record,
                "expected_version": hashlib.sha256(json.dumps(
                    record, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                ).encode("utf-8")).hexdigest(),
                "source_events": sources,
                "replaces": replaces,
                "related_taste": related_taste,
            })
    active_taste = taste.active()
    state = {
        "high": store.active_state("high"),
        "mid": store.active_state("mid"),
    }
    active_ids = {item["id"] for items in state.values() for item in items}
    for section in ("canon_pending", "task_pending"):
        for item in inbox[section]:
            peers = [peer for peer in inbox[section] if peer["logical_key"] == item["logical_key"]]
            item["same_topic_candidates"] = [peer["record"] for peer in peers if peer["id"] != item["id"]]
            item["latest_for_topic"] = peers[-1]["id"] == item["id"]
            item["replacement_is_current"] = bool(item["replaces"] and item["replaces"][0]["id"] in active_ids)
    active_cards = cards.by_status("active")
    # Reading the current state should also explain its source, even when that
    # source is older than the activity window or belongs to another session.
    for item in state["high"] + state["mid"] + active_taste:
        item["source_events"] = []
        for source_id in item.get("source_event_ids", []):
            try:
                item["source_events"].append(asdict(store.get_event(source_id)))
            except KeyError:
                item["source_events"].append({"id": source_id, "unavailable": True})

    causal = build_causal_model(store, session_id=session_id, limit=fetch_limit)
    state_diff = build_state_diff_model(store)

    return {
        "project": store.project(),
        "session_id": session_id,
        "timeline": timeline,
        "causal": causal,
        "state_diff": state_diff,
        "folded_low_signal": folded,
        "truncated": truncated,
        "limit": limit,
        "inbox": inbox,
        "state": state,
        "taste": {"active": active_taste, "cards": active_cards},
        "counts": {
            "events": len(timeline),
            "pending_total": inbox["counts"]["total"],
        },
    }


def render_ledger_html(model: dict[str, Any], *, review_context: dict[str, Any] | None = None) -> str:
    """Render the shared human-facing page without changing persisted memory."""
    from .ledger_page import render_page
    return render_page(model, review_context, _REVIEW_SCRIPT)


# Candidate data stays in escaped HTML attributes/text, never executable JS.
_REVIEW_SCRIPT = """<script>
(() => {
  'use strict';
  const token = document.body.dataset.reviewToken;
  const storagePrefix = 'noname-ledger:' + token + ':';
  const forms = Array.from(document.querySelectorAll('.review-form'));
  let hasDraft = false;
  function readStored(key) {
    try { return sessionStorage.getItem(storagePrefix + key); } catch (_) { return null; }
  }
  function writeStored(key, value) {
    try { sessionStorage.setItem(storagePrefix + key, value); } catch (_) {}
  }
  function removeStored(key) {
    try { sessionStorage.removeItem(storagePrefix + key); } catch (_) {}
  }
  const notice = readStored('notice');
  if (notice) {
    document.getElementById('review-notice').textContent = notice;
    removeStored('notice');
  }
  for (const form of forms) {
    const draftKey = 'draft:' + form.dataset.kind + ':' + form.dataset.id;
    const reviewer = form.elements.namedItem('reviewer_id');
    const reason = form.elements.namedItem('reason');
    const edit = form.elements.namedItem('edited_content');
    const friendlyFields = Array.from(form.querySelectorAll('[data-edit-field]'));
    const feedback = form.querySelector('.review-feedback');
    const reload = form.querySelector('.reload-page');
    const reconcile = form.querySelector('.reconcile-draft');
    const discard = form.querySelector('.discard-draft');
    const initialEdit = edit ? edit.value : null;
    let busy = false;
    let staleDraft = false;
    let draftVersion = form.dataset.version;
    let draftHead = form.dataset.expectedHeadId;
    let draft = null;
    try { draft = JSON.parse(readStored(draftKey)); } catch (_) {}
    if (draft && typeof draft === 'object') {
      hasDraft = true;
      if (typeof draft.reviewer_id === 'string') reviewer.value = draft.reviewer_id;
      if (typeof draft.reason === 'string') reason.value = draft.reason;
      if (edit && typeof draft.edited_content === 'string') edit.value = draft.edited_content;
      draftVersion = draft.expected_version;
      draftHead = draft.expected_head_id;
      if (draft.expected_version !== form.dataset.version ||
          draft.expected_head_id !== form.dataset.expectedHeadId) {
        feedback.textContent = '已恢复先前草稿，但候选或将被替代的状态已变化。请核对完整内容和当前版本后再签署。';
        feedback.classList.add('error');
        staleDraft = true;
        reconcile.hidden = false;
        discard.hidden = false;
      }
    } else {
      reviewer.value = readStored('reviewer') || '';
    }
    function restoreFriendly() {
      if (!edit || !friendlyFields.length) return;
      try {
        const content = JSON.parse(edit.value);
        for (const field of friendlyFields) {
          if (content && typeof content[field.dataset.editField] === 'string') {
            field.value = content[field.dataset.editField];
          }
        }
      } catch (_) {}
    }
    restoreFriendly();
    function saveDraft() {
      hasDraft = true;
      writeStored(draftKey, JSON.stringify({
        reviewer_id: reviewer.value, reason: reason.value,
        edited_content: edit ? edit.value : null,
        expected_version: draftVersion,
        expected_head_id: draftHead,
      }));
    }
    reconcile.addEventListener('click', () => {
      draftVersion = form.dataset.version;
      draftHead = form.dataset.expectedHeadId;
      staleDraft = false;
      reconcile.hidden = true;
      discard.hidden = true;
      feedback.textContent = '已确认草稿基于当前显示的候选与版本。可以继续签署。';
      feedback.classList.remove('error');
      saveDraft();
    });
    discard.addEventListener('click', () => {
      reason.value = '';
      if (edit) edit.value = initialEdit;
      restoreFriendly();
      draftVersion = form.dataset.version;
      draftHead = form.dataset.expectedHeadId;
      staleDraft = false;
      reconcile.hidden = true;
      discard.hidden = true;
      feedback.textContent = '已丢弃旧草稿，恢复当前候选内容。';
      feedback.classList.remove('error');
      removeStored(draftKey);
    });
    form.addEventListener('input', (event) => {
      if (edit && friendlyFields.includes(event.target)) {
        try {
          const content = JSON.parse(edit.value);
          for (const field of friendlyFields) {
            Object.defineProperty(content, field.dataset.editField, {
              value: field.value, enumerable: true, configurable: true, writable: true,
            });
          }
          edit.value = JSON.stringify(content, null, 2);
        } catch (_) {
          feedback.textContent = '完整结构 JSON 无效，请先在高级编辑中修正；草稿已保留。';
          feedback.classList.add('error');
        }
      } else if (event.target === edit) {
        restoreFriendly();
      }
      saveDraft();
    });
    reload.addEventListener('click', () => { saveDraft(); location.reload(); });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (busy || !event.submitter) return;
      if (staleDraft) {
        feedback.textContent = '旧草稿所依据的版本已变化。请先核对最新内容，再明确继续使用草稿或丢弃草稿。';
        feedback.classList.add('error');
        reconcile.focus();
        return;
      }
      const reviewerId = reviewer.value.trim();
      if (!reviewerId) {
        feedback.textContent = '请填写签署人，审核不会自动代你签署。';
        feedback.classList.add('error');
        reviewer.focus();
        return;
      }
      const action = event.submitter.value;
      const payload = {
        kind: form.dataset.kind, id: form.dataset.id, action,
        reviewer_id: reviewerId, reason: reason.value.trim(),
        expected_version: form.dataset.version,
      };
      if (form.dataset.kind === 'state_proposal') {
        payload.expected_head_id = form.dataset.expectedHeadId;
      }
      if (action === 'edit') {
        try {
          payload.edited_content = JSON.parse(edit.value);
          if (payload.edited_content === null ||
              (form.dataset.kind === 'taste_card_candidate' &&
               (typeof payload.edited_content !== 'object' || Array.isArray(payload.edited_content)))) {
            throw new Error('invalid content');
          }
        } catch (_) {
          feedback.textContent = '内容 JSON 无效。卡片需要 JSON 对象，状态内容不能为 null；草稿已保留。';
          feedback.classList.add('error');
          edit.focus();
          return;
        }
      }
      saveDraft();
      busy = true;
      form.setAttribute('aria-busy', 'true');
      const buttons = Array.from(form.querySelectorAll('button'));
      buttons.forEach(button => { button.disabled = true; });
      feedback.classList.remove('error');
      feedback.textContent = '正在写入审核记录…';
      reload.hidden = true;
      try {
        const response = await fetch(new URL('api/review', location.href), {
          method: 'POST', headers: {
            'Content-Type': 'application/json', 'X-NoName-Token': token,
          }, body: JSON.stringify(payload), credentials: 'same-origin',
        });
        let result = {};
        try { result = await response.json(); } catch (_) {}
        if (!response.ok) {
          const error = typeof result.error === 'string' ? result.error :
            (result.error && result.error.message) || result.message || ('HTTP ' + response.status);
          const conflict = response.status === 409;
          feedback.textContent = (conflict ? '候选已变化或已被审核。' : '审核未完成。') +
            ' ' + error + ' 草稿已保留；可刷新核对最新内容后重试。';
          feedback.classList.add('error');
          reload.hidden = false;
          return;
        }
        removeStored(draftKey);
        writeStored('reviewer', reviewerId);
        const labels = {accept:'已确认并记住', adopt:'已采纳品味', reject:'已拒绝候选',
          retire:'已退役候选', defer:'已暂缓，候选仍在收件箱',
          edit:form.dataset.kind === 'taste_card_candidate' ? '修改已保存，卡片继续待审' : '修改已确认并记住'};
        writeStored('notice', (labels[action] || '审核已写入账本') + ' · 签署人 ' + reviewerId + '。');
        location.reload();
      } catch (_) {
        feedback.textContent = '连接失败，审核结果尚未确认。草稿已保留；刷新账本核对后再提交。';
        feedback.classList.add('error');
        reload.hidden = false;
      } finally {
        busy = false;
        form.removeAttribute('aria-busy');
        buttons.forEach(button => { button.disabled = false; });
      }
    });
  }
  const revision = document.body.dataset.ledgerRevision;
  if (revision) {
    const updateNotice = document.getElementById('live-updates');
    const message = document.getElementById('updates-message');
    const refresh = document.getElementById('refresh-ledger');
    refresh.addEventListener('click', () => location.reload());
    let stopped = false;
    async function checkUpdates() {
      if (stopped || document.visibilityState !== 'visible') return;
      try {
        const response = await fetch(new URL('api/updates', location.href), {
          credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(5000),
        });
        if (!response.ok) throw new Error('disconnected');
        const result = await response.json();
        if (typeof result.revision !== 'string' || result.revision === revision) return;
        const current = document.querySelector('[data-panel]:not([hidden])');
        const focused = document.activeElement;
        const typing = focused && ['INPUT','TEXTAREA','SELECT'].includes(focused.tagName);
        const searching = document.getElementById('ledger-search').value.trim();
        const signing = forms.some(form => form.getAttribute('aria-busy') === 'true');
        if (current && current.id === 'now' && !hasDraft && !typing && !searching &&
            !current.querySelector('details[open]') && !signing) {
          location.reload();
          return;
        }
        message.textContent = '账本有新变化。你的阅读位置和审核草稿已保留。';
        updateNotice.hidden = false;
        refresh.hidden = false;
      } catch (_) {
        message.textContent = '暂时无法连接本地账本。当前内容与草稿仍在；可重新打开账本入口后查看最新变化。';
        updateNotice.hidden = false;
        refresh.hidden = false;
      }
    }
    const timer = window.setInterval(checkUpdates, 15000);
    window.addEventListener('pagehide', () => {stopped = true; clearInterval(timer);});
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') checkUpdates();
    });
  }
})();
</script>"""
