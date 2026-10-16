"""A human-facing ledger, derived entirely from the existing read model."""

from __future__ import annotations

import json
from typing import Any

from .causal_map import render_causal_html
from .ledger_assets import BROWSE_SCRIPT, STYLE
from .ledger_readable import escape as esc, event_label, label, plain, raw_details, readable


def _search(value: Any) -> str:
    # Equal signs in source text are encoded too, keeping apparent inline
    # handlers inert and unambiguous to simple markup scanners. The DOM
    # decodes them for literal search without changing the underlying record.
    text = esc(plain(value)).replace('=', '&#61;')
    return f'data-search="{text}"'


def _time(value: str) -> str:
    text = value.replace('T', ' ').replace('+00:00', ' UTC').replace('Z', ' UTC')
    return f'<time datetime="{esc(value)}">{esc(text)}</time>'


def _sources(sources: list[dict[str, Any]]) -> str:
    rows = []
    for source in sources:
        if source.get('unavailable'):
            rows.append('<div class="source-event"><p class="conflict-note">无法解析的引用：'
                        f'{esc(source["id"])}。请核对原始来源。</p></div>')
            continue
        rows.append(
            '<div class="source-event"><div class="source-heading">'
            f'{esc(event_label(source["event_type"]))} · {_time(source.get("occurred_at", ""))}</div>'
            f'{readable(source.get("payload"))}'
            f'{raw_details(source, "事件详情 · ID 与完整记录")}</div>'
        )
    return (f'<details class="sources"><summary>查看来源事件 · {len(sources)} 项</summary>'
            + (''.join(rows) or '<p class="muted">没有附带来源事件；请在判断前确认依据。</p>')
            + '</details>')


def _heading(key: str, fallback: str) -> str:
    # Unknown machine keys remain in technical details; do not fabricate a
    # confident semantic title from an opaque identifier.
    from .ledger_readable import LABELS
    return label(key) if key in LABELS else fallback


def _state_cards(items: list[dict[str, Any]], *, task: bool = False) -> str:
    if not items:
        return ('<div class="empty"><strong>还没有需要接续的工作</strong>在阶段结束时，'
                '让 Agent 提议保存当前进度、未决问题和下一步。审核后会出现在这里。</div>' if task else
                '<div class="empty"><strong>重要的决定，值得留下来</strong>普通讨论先作为证据留档。'
                '当项目方向、约束或决定明确后，让 Agent 提议一条简短记忆，由你确认。</div>')
    rows = []
    for item in items:
        title = _heading(item['logical_key'], '工作接续' if task else '项目约定')
        rows.append(
            f'<article class="card state-card" {_search([item["logical_key"], item["content"]])}>'
            f'<div class="eyebrow"><span class="dot"></span>{"WORK IN PROGRESS" if task else "DECISION, KEPT"}</div>'
            f'<h3>{esc(title)}</h3>{readable(item["content"])}'
            f'<div class="card-foot"><span class="meta">由 {esc(item.get("approved_by") or "未注明的签署人")} 确认</span>'
            '<span class="tag">当前有效</span></div>'
            f'{_sources(item.get("source_events", []))}'
            f'{raw_details(item, "记忆详情 · 主题键、版本与有效时间")}</article>'
        )
    return ''.join(rows)


def _taste_cards(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<div class="empty"><strong>你的判断，会慢慢变得清晰</strong>偏好保留具体例子与判断；模型发现的品味需要你明确采纳。</div>'
    rows = []
    for item in items:
        authored = item['track'] == 'authored'
        rows.append(
            f'<article class="card" {_search(item["content"])}>'
            f'<div class="review-heading"><span class="tag taste">{"你明确表达" if authored else "你已采纳"}</span>'
            f'<span class="meta">{"当前项目" if item["scope"] == "project" else "用户偏好"}</span></div>'
            f'{readable(item["content"])}'
            '<p class="section-note">影响排序、权衡与表达；不作为事实依据。</p>'
            f'{_sources(item.get("source_events", []))}{raw_details(item)}</article>'
        )
    return ''.join(rows)


def _confirmed_taste_cards(items: list[dict[str, Any]]) -> str:
    if not items:
        return ''
    rows = []
    for item in items:
        warning = '<p class="conflict-note">来源品味已经变化；这张卡需要复核，不能视为新的确认。</p>' if item.get('stale') else ''
        rows.append(
            f'<article class="card" {_search(item)}><span class="tag taste">已确认的品味卡</span>'
            f'<h3>{esc(item["title"])}</h3>{readable({"attitude": item["attitude"], "tensions": item["tensions"]})}'
            f'{warning}{raw_details(item, "品味卡详情 · 来源与版本")}</article>'
        )
    return '<div class="section-heading"><h2>慢慢形成的品味</h2></div>' + ''.join(rows)


def _review_form(item: dict[str, Any], index: int, context: dict[str, Any] | None) -> str:
    if context is None:
        return ''
    kind = item['kind']
    record = item['record']
    prefix = f'review-{index}'
    friendly = ''
    if kind == 'state_proposal':
        actions = [('accept', '确认并记住'), ('reject', '不采纳'), ('defer', '暂缓判断')]
        editable = record['content']
        edit_label = '签署修改并采纳'
        edit_note = '修改后的内容将直接成为已批准状态。先核对修改，再签署。'
        if isinstance(editable, dict) and editable and len(editable) <= 12 and all(isinstance(v, str) for v in editable.values()):
            fields = ''.join(
                f'<div><label for="{prefix}-field-{i}">{esc(label(key))}</label>'
                f'<textarea id="{prefix}-field-{i}" data-edit-field="{esc(key)}" rows="3">{esc(value)}</textarea></div>'
                for i, (key, value) in enumerate(editable.items())
            )
            friendly = f'<div class="friendly-fields">{fields}</div>'
    elif kind == 'taste_candidate':
        actions = [('adopt', '这符合我的判断'), ('retire', '不采纳')]
        editable = None
        edit_label = edit_note = ''
    else:
        actions = [('accept', '确认这张品味卡'), ('retire', '不采纳')]
        editable = {key: record[key] for key in ('title', 'attitude', 'tensions', 'influence', 'image')}
        edit_label = '保存修改，继续待审'
        edit_note = '修改会生成新的待审卡片；采纳需要另行签署。'
    buttons = ''.join(
        f'<button type="submit" name="action" value="{action}" class="{"primary" if action in {"accept", "adopt"} else "secondary"}">{text}</button>'
        for action, text in actions
    )
    edit_html = ''
    if kind != 'taste_candidate':
        raw_editor = (f'<label for="{prefix}-edit">内容 JSON</label>'
                      f'<textarea id="{prefix}-edit" name="edited_content" rows="8" spellcheck="false">'
                      f'{esc(json.dumps(editable, ensure_ascii=False, indent=2, sort_keys=True))}</textarea>')
        if friendly:
            raw_editor = '<details class="advanced-edit"><summary>高级 · 编辑完整结构</summary>' + raw_editor + '</details>'
        edit_html = (f'<details class="edit-details"><summary>修改后再确认</summary><p class="meta">{edit_note}</p>'
                     f'{friendly}{raw_editor}<button type="submit" name="action" value="edit" class="secondary">{edit_label}</button></details>')
    head = ''
    if kind == 'state_proposal':
        head_id = item['replaces'][0]['id'] if item.get('replaces') else ''
        head = f' data-expected-head-id="{esc(head_id)}"'
    return (
        f'<form class="review-form" data-kind="{esc(kind)}" data-id="{esc(item["id"])}" '
        f'data-version="{esc(item["expected_version"])}"{head}>'
        '<div class="review-fields"><div>'
        f'<label for="{prefix}-reviewer">签署人 <span aria-hidden="true">*</span></label>'
        f'<input id="{prefix}-reviewer" name="reviewer_id" required maxlength="200" autocomplete="name" placeholder="你的姓名或身份" /></div>'
        f'<div><label for="{prefix}-reason">判断理由（可选）</label><textarea id="{prefix}-reason" name="reason" rows="2" placeholder="什么让你做出这个判断？"></textarea></div></div>'
        f'<div class="review-actions">{buttons}</div>{edit_html}'
        '<p class="review-feedback" role="status" aria-live="polite"></p>'
        '<div class="draft-actions"><button type="button" class="reconcile-draft secondary" hidden>已核对最新内容，继续使用草稿</button>'
        '<button type="button" class="discard-draft secondary" hidden>丢弃旧草稿</button></div>'
        '<button type="button" class="reload-page secondary" hidden>保留草稿并刷新候选</button></form>'
    )


def _inbox(model: dict[str, Any], context: dict[str, Any] | None) -> str:
    groups = [('canon_pending', '项目约定 · 法典候选', '项目约定'),
              ('task_pending', '工作接续 · 任务候选', '工作接续'),
              ('taste_pending', '品味候选', '品味'), ('card_pending', '品味卡候选', '品味卡')]
    sections = []
    index = 0
    for key, title, kind_label in groups:
        rows = []
        for item in model['inbox'][key]:
            index += 1
            record = item.get('record', item)
            replacements = item.get('replaces', [])
            content = record.get('content', {field: record[field] for field in ('title', 'attitude', 'tensions', 'influence') if field in record})
            heading = record.get('title') or _heading(item.get('logical_key', ''), kind_label)
            if replacements:
                old = replacements[0]
                old_content = old.get('content', {field: old[field] for field in ('title', 'attitude', 'tensions', 'influence') if field in old})
                current = item.get('replacement_is_current', old.get('status') == 'active')
                effect = ('采纳将替代当前版本；旧内容保留在来路与变化中。' if current else
                          '当前没有生效版本。采纳后新增有效内容，并接续已有历史版本链。')
                old_label = '现在的版本' if current else '上一个历史版本 · 当前未生效'
                replacement = (
                    f'<div class="effect">{effect}</div>'
                    f'<div class="comparison"><div><span class="caption">{old_label}</span>'
                    f'{readable(old_content)}</div><div><span class="caption">你将确认的新版本</span>'
                    f'{readable(content)}</div></div>'
                )
            else:
                impact = ('确认后成为当前项目决定或工作接续。' if item['kind'] == 'state_proposal'
                          else '确认后作为软影响参与协作；不会变成事实依据。')
                replacement = f'<div class="effect">{impact}</div>'
            conflicts = record.get('conflict_with_ids', [])
            conflict_note = (f'<p class="conflict-note">存在 {len(conflicts)} 项冲突。请先核对现在与新版本的差别。</p>' if conflicts else '')
            alternatives = ''
            peers = item.get('same_topic_candidates', [])
            if peers:
                which = '较新的提议' if item.get('latest_for_topic') else '先前的提议'
                alternatives = (
                    f'<p class="conflict-note">这是{which}；同一主题还有 {len(peers)} 条待确认内容。请先对照，选择适用的决定；这些内容尚未生效。</p>'
                    f'<details class="sources"><summary>对照同一主题的其他候选 · {len(peers)} 项</summary>'
                    + ''.join(f'<div class="source-event">{readable(peer["content"])}{raw_details(peer)}</div>' for peer in peers)
                    + '</details>'
                )
            stale = ('<p class="conflict-note">卡片来源已经变化（stale）；请先核对关联品味。</p>' if record.get('stale') else '')
            related = ''
            if item.get('related_taste'):
                related = (f'<details class="sources"><summary>关联品味 · {len(item["related_taste"])} 项</summary>'
                           + ''.join(readable(t['content']) + raw_details(t) for t in item['related_taste']) + '</details>')
            reason = f'<p class="section-note">为什么提出：{esc(record["reason"])}</p>' if record.get('reason') else ''
            candidate_content = '' if replacements else f'<div class="candidate-content">{readable(content)}</div>'
            rows.append(
                f'<article class="card review-card" {_search([heading, content, record.get("reason", "")])}>'
                f'<div class="review-heading"><span class="tag review">待你确认</span><h3>{esc(heading)}</h3></div>'
                f'{candidate_content}{reason}{conflict_note}{stale}{alternatives}{replacement}'
                f'{_sources(item.get("source_events", []))}{related}'
                f'{raw_details(record, "完整候选内容与审核记录")}{_review_form(item, index, context)}</article>'
            )
        if rows:
            sections.append(f'<section class="inbox-section"><h2>{esc(title)}</h2>{"".join(rows)}</section>')
    return ''.join(sections) or '<div class="empty"><strong>没有等待你判断的事</strong>不必为了填满账本保存内容。重要决定或偏好出现时，再提议、再确认。</div>'


def _activities(model: dict[str, Any]) -> str:
    rows = []
    for node in reversed(model['timeline']):
        seq = f'{node["session_id"]}#{node["seq"]}' if model['session_id'] is None else f'#{node["seq"]}'
        rows.append(
            f'<li class="activity-item" data-signal="{"key" if node["high_signal"] else "record"}" '
            f'{_search([node["event_type"], node["payload"], node["session_id"]])}>'
            f'<div class="activity-heading"><h3>{esc(event_label(node["event_type"]))}</h3>{_time(node["occurred_at"])}</div>'
            f'<details class="activity-content"><summary>阅读记录内容</summary>{readable(node["payload"])}</details>'
            f'{raw_details({**node, "sequence": seq}, "原始事件 · " + node["event_type"] + " · " + seq)}</li>'
        )
    return ''.join(rows) or '<li class="empty">还没有活动记录。启用集成后，受支持的交互会作为证据留档。</li>'


def _history(model: dict[str, Any], active_state: dict[str, Any]) -> str:
    """Human-readable version chains; preserve full metadata behind disclosure."""
    rows = []
    active_keys = {(item['layer'], item['logical_key']) for items in active_state.values() for item in items}
    for chain in model['canon_diffs']:
        versions = []
        for version in reversed(chain['versions']):
            current = version['is_head'] and version['status'] == 'active' and (chain['layer'], chain['logical_key']) in active_keys
            status = ('当前有效' if current else '已失效' if version['status'] == 'retired' else
                      '最新版本 · 当前未生效' if version['is_head'] else '历史版本')
            versions.append(
                f'<li class="version {"active" if current else "historical"}">'
                f'<div class="review-heading"><span class="tag">{status}</span><span class="meta">第 {version["number"]} 版 · '
                f'{esc(version.get("approved_by", ""))} 确认</span></div>'
                f'{readable(version["content"])}'
                f'<p class="meta">{esc(version.get("delta_from_previous") or "最初确认的版本")} · {_time(version.get("created_at", ""))}</p>'
                f'{raw_details(version, "版本详情 · 签署与有效时间")}</li>'
            )
        title = label(chain['logical_key'])
        rows.append(
            f'<li class="diff-group" {_search(chain)}><details><summary><strong>{esc(title)}</strong>'
            f'<span class="meta"> · {chain["version_count"]} 个版本 · {"项目约定" if chain["layer"] == "high" else "工作接续"}</span></summary>'
            f'<ul class="versions">{"".join(versions)}</ul></details></li>'
        )
    for chain in model['card_chains']:
        versions = ''.join(
            f'<li class="version"><span class="tag">{"当前" if version["is_head"] else "历史版本"}</span>'
            f'{readable({"title": version["title"], "attitude": version["attitude"]})}{raw_details(version)}</li>'
            for version in reversed(chain['versions'])
        )
        rows.append(f'<li class="diff-group" {_search(chain)}><details><summary>品味卡 · {esc(chain["title"])}</summary><ul class="versions">{versions}</ul></details></li>')
    return '<ul class="diff-list">' + ''.join(rows) + '</ul>' if rows else '<div class="empty">还没有已确认的版本变化。决定生效与修正后，来路会保留在这里。</div>'


def render_page(model: dict[str, Any], review_context: dict[str, Any] | None, review_script: str) -> str:
    project = esc(model['project']['name'])
    pending = model['inbox']['counts']['total']
    live = review_context is not None
    nav_count = f'<span class="nav-count">{pending}</span>' if pending else ''
    nav_items = [('now', '此刻'), ('inbox', '等你判断'), ('taste', '品味与偏好'), ('history', '来路与变化'), ('archive', '活动档案')]
    nav = ''.join(f'<a data-nav href="#{key}"><span class="nav-number">0{i}</span>{name}{nav_count if key == "inbox" else ""}</a>' for i, (key, name) in enumerate(nav_items, 1))
    source_count = len(model['timeline'])
    package_events = [e for e in model['timeline'] if e['event_type'] == 'context.assembled']
    latest_record = next((e for e in reversed(model['timeline']) if e['event_type'] != 'context.assembled'), None)
    latest = package_events[-1] if package_events else None
    context_note = (
        f'<p class="meta">最近一次接续上下文组装于 {_time(latest["occurred_at"])}；这表示已组装，不代表模型已采用每条内容。</p>'
        + raw_details(latest, '接续详情 · 组装记录') if latest else
        '<p class="meta">当前展示范围内还没有接续包记录。新会话读取项目记忆后，这里会留下可核验的组装记录。</p>'
    )
    record_note = (f'最近留证于 {_time(latest_record["occurred_at"])}。' if latest_record else '当前展示范围内暂无交互留证。')
    session_note = (f'当前活动范围：{esc(model["session_id"])}。收件箱与状态是全局视图，不受会话过滤影响。' if model['session_id'] else '活动范围：全部会话。')
    truncation = (f'<p class="section-note trunc">仅显示最近 {model["limit"]} 条（历史被截断）；已确认状态与审核收件箱仍为全局投影。</p>' if model.get('truncated') else '')
    mode = '本地交互模式' if live else '只读快照'
    freshness_note = ('此刻闲置时自动更新；阅读、搜索或编辑时只提示新变化。' if live else
                      '只读快照反映导出时的记录；打开本地入口可查看后续变化。')
    review_mode = (
        '<div class="review-mode">你只需判断真正值得长期影响协作的内容。填写签署人后逐条确认，结果会写入项目账本。</div>' if live else
        '<details class="review-mode"><summary>这是只读快照 · 如何打开审核？</summary><p>此 HTML 可展开浏览；签署需要本地服务。Codex 使用 <code>noname-codex ledger</code>；DSH 调用 <code>noname_ledger</code>。也可在 NoNameAgentHarness 仓库运行 <code>.venv/bin/python -m noname_harness ledger-serve --db /path/to/project/.noname/harness.db --background</code>，打开返回的本地 URL。</p></details>'
    )
    attention = ('<h2>有几件事，值得你的判断。</h2><p>只确认重要决定与偏好。其余细节，留在档案里。</p>' if pending else '<h2>此刻，没有需要你处理的事。</h2><p>安心继续工作。新的长期影响，需要你的判断时再确认。</p>')
    attention_link = f'<a class="button" href="#inbox">查看 {pending} 项候选 <span aria-hidden="true">↗</span></a>' if pending else '<a class="text-link" href="#archive">看看来路 ↗</a>'
    state = _state_cards(model['state']['high'])
    task = _state_cards(model['state']['mid'], task=True)
    # A single script owns browsing and (in live mode) signing. Store content is
    # always escaped text/attributes; no record is interpolated into executable JS.
    js = BROWSE_SCRIPT + (review_script.removeprefix('<script>').removesuffix('</script>') if live else '')
    token = (f' data-review-token="{esc(review_context["token"])}" '
             f'data-ledger-revision="{esc(review_context.get("revision", ""))}"') if live else ''
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="referrer" content="no-referrer"/><title>NoName 账本 · {project}</title><style>{STYLE}</style></head>
<body{token}><a class="skip" href="#main-content">跳到正文</a><div class="shell">
<aside class="sidebar"><a href="#now" class="wordmark" aria-label="NoName 此刻"><span class="mark" aria-hidden="true">n</span>NoName</a><div class="brand-note">CONTEXT, COMPOUNDING.</div>
<div class="sidebar-project"><div class="project-label">YOUR WORKSPACE</div><p class="project-name">{project}</p></div>
<nav class="nav" aria-label="账本导航">{nav}</nav><div class="sidebar-foot"><strong>重要的事，带去下一次。</strong><p>决定留下来。<br/>细节有来路。<br/>判断仍属于你。</p><q>上下文，是你的资产。</q></div></aside>
<main class="main" id="main-content"><div class="topbar"><div class="breadcrumb">工作区 / {project}</div><div class="searchbar" role="search"><label for="ledger-search" class="sr-label" hidden>搜索当前页面</label><input type="search" id="ledger-search" placeholder="搜索当前页面…" aria-label="搜索当前页面" autocomplete="off"/><button id="clear-search" type="button">清除</button></div><span class="mode">{mode}</span></div>
<noscript><p class="noscript">已展示所有页面；左侧链接可定位内容，来源详情可展开。搜索与页面签署需要启用 JavaScript；也可使用 CLI 审核。</p></noscript>
<div class="live-updates" id="live-updates" role="status" aria-live="polite" hidden><span id="updates-message">账本有新变化。</span><button type="button" class="secondary" id="refresh-ledger">保留草稿并更新</button></div><p class="browse-feedback" id="browse-feedback" role="status" aria-live="polite" hidden></p><div class="filter-empty" id="filter-empty" hidden>当前页面没有匹配项。清除搜索，或切换到其他页面查找。</div>
<section class="panel" id="now" data-panel><header class="hero"><p class="eyebrow">A LITTLE CONTEXT. A LONG WAY.</p><h1 tabindex="-1">把重要的事，接着做。</h1><p class="lede">每次协作，都可以从上一次往前走。<br/>这里是已确认的决定与工作接续；对话细节留在档案，需要时再找。</p></header>
<div class="quiet-callout"><div>{attention}</div>{attention_link}</div>
<div class="current-grid"><section><div class="column-head"><h2>已经记住的决定</h2><span class="caption">稳定约定 · 已确认</span></div>{state}</section><section><div class="column-head"><h2>工作进行到这里</h2><span class="caption">当前进度 · 已确认</span></div>{task}</section></div>
<div class="presence"><div class="presence-head"><span class="presence-dot" aria-hidden="true"></span>协作留下的痕迹</div><p class="meta">{record_note}</p>{context_note}<div class="presence-steps"><span>交互留证</span><span>少量重要候选</span><span>你来确认</span><span>下次接着做</span></div><p class="section-note">留证不等于记住。只有审核后的决定与接续进入当前状态；品味以软影响单独呈现。{freshness_note}</p></div>{truncation}</section>
<section class="panel" id="inbox" data-panel><header class="hero"><p class="eyebrow">YOUR JUDGEMENT MATTERS.</p><h1 tabindex="-1">只留下，值得记住的。</h1><p class="lede">审核收件箱 · {pending} 项等待你的判断。确认前看看内容、来路，以及它会替代什么。</p></header>{review_mode}<div id="review-notice" class="review-notice" role="status" aria-live="polite"></div>{_inbox(model, review_context)}</section>
<section class="panel" id="taste" data-panel><header class="hero"><p class="eyebrow">A WAY OF SEEING.</p><h1 tabindex="-1">不只记住做什么，也记住你在意什么。</h1><p class="lede">偏好来自你的明确表达，或你对一个具体时刻的采纳。它影响协作的分寸与表达。</p></header><p class="taste-note">品味 · 软影响（非事实）：不改写证据，不降低验证标准，不覆盖当前指令。模型提议的品味需你明确采纳；自述轨表示你曾明确表达。</p>{_taste_cards(model['taste']['active'])}{_confirmed_taste_cards(model['taste'].get('cards', []))}</section>
<section class="panel" id="history" data-panel><header class="hero"><p class="eyebrow">NOTHING IMPORTANT, LOST.</p><h1 tabindex="-1">想法会改变，来路一直在。</h1><p class="lede">当前状态保留最新有效版本。修正生效后，旧内容退到历史，仍可以追问为什么。</p></header><details class="history-group" open><summary>决定如何演进</summary>{_history(model['state_diff'], model['state'])}</details><details class="history-group"><summary>追问为什么 · 来源关系</summary>{render_causal_html(model['causal'])}</details></section>
<section class="panel" id="archive" data-panel><header class="hero"><p class="eyebrow">THE DETAILS HAVE A HOME.</p><h1 tabindex="-1">需要细节时，随时回来。</h1><p class="lede">时间线 · 发生过的交互、工具结果与记忆变化。档案是历史证据，不代表每条内容仍然有效。</p></header><p class="section-note">{session_note}</p><div class="archive-toolbar"><p class="meta">本次载入 {source_count} 条记录 · {model['folded_low_signal']} 条日常记录 / 低层簿记事件</p><label for="activity-filter" hidden>活动类型</label><select id="activity-filter" aria-label="活动类型"><option value="key">重要变化</option><option value="all">全部记录</option><option value="record">日常留证</option></select></div>{truncation}<ul class="activity-list">{_activities(model)}</ul></section>
<footer class="footer"><span>NoName · Context as an asset.</span><span>本地账本 · 确定的事留下，变化的事有迹可循。</span></footer></main></div><script>{js}</script></body></html>'''
