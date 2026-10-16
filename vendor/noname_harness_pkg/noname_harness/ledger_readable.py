"""Deterministic, escaped presentation of ledger content; never a memory writer.

Keep unfamiliar fields visible. These labels translate structure, not meaning:
the renderer does not ask a model to rewrite evidence or invent a summary.
"""

from __future__ import annotations

import html
import json
from typing import Any


LABELS = {
    "text": "内容", "summary": "摘要", "goal": "目标", "decision": "决定",
    "reason": "理由", "rationale": "为什么", "next_step": "下一步",
    "next": "下一步", "status": "进度", "constraints": "约束",
    "constraint": "约定", "attitude": "判断与态度", "judgement": "判断",
    "example": "具体例子", "avoid": "希望避免", "title": "标题",
    "tensions": "需要权衡", "influence": "影响方式", "content": "内容",
    "name": "名称", "path": "位置", "message": "说明", "result": "结果",
    "task": "工作", "key": "主题", "value": "内容", "scope": "作用范围",
    "action": "操作", "reviewer_id": "签署人", "approved_by": "批准人",
    "database": "数据存储", "boundary": "工作区边界", "checkpoint": "工作接续",
    "architecture": "架构决定", "language": "沟通语言", "testing": "验证约定",
}

_FIELD_ORDER = {key: i for i, key in enumerate((
    "title", "text", "summary", "decision", "goal", "attitude", "judgement",
    "content", "reason", "rationale", "example", "avoid", "constraints",
    "status", "next_step", "next", "tensions", "influence",
))}


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def label(key: str) -> str:
    return LABELS.get(key, key.replace("_", " ").replace(".", " / "))


def plain(value: Any) -> str:
    """Search and short labels use literal source text, never generated claims."""
    if isinstance(value, dict):
        return " · ".join(f"{label(str(key))}：{plain(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "；".join(plain(item) for item in value)
    if value is None:
        return "未提供"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def readable(value: Any, *, depth: int = 0) -> str:
    """Show every field without raw JSON being the reading surface."""
    if depth > 12:
        return f'<pre>{escape(json.dumps(value, ensure_ascii=False))}</pre>'
    if isinstance(value, dict):
        if not value:
            return '<p class="muted">暂无内容</p>'
        if len(value) == 1 and next(iter(value)) in {"text", "content", "summary"}:
            return readable(next(iter(value.values())), depth=depth + 1)
        return '<dl class="readable">' + "".join(
            f'<div><dt>{escape(label(str(key)))}</dt><dd>{readable(item, depth=depth + 1)}</dd></div>'
            for key, item in sorted(value.items(), key=lambda pair: _FIELD_ORDER.get(pair[0], 100))
        ) + '</dl>'
    if isinstance(value, list):
        if not value:
            return '<p class="muted">暂无</p>'
        return '<ul class="content-list">' + "".join(
            f'<li>{readable(item, depth=depth + 1)}</li>' for item in value
        ) + '</ul>'
    return f'<p class="prose">{escape(plain(value))}</p>'


def raw_details(value: Any, title: str = "技术详情 · 原始记录") -> str:
    return (f'<details class="technical"><summary>{escape(title)}</summary>'
            f'<pre>{escape(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))}</pre></details>')


EVENT_LABELS = {
    "codex.user": "你提出了新的目标或反馈", "codex.assistant": "Agent 完成了一轮回应",
    "dsh.tool.completed": "工具结果已留作证据", "context.assembled": "组装了一份接续上下文",
    "state.proposed": "提出了一条记忆候选", "state.reviewed": "完成了一次记忆审核",
    "state.accepted": "一条记忆已生效", "project.initialized": "项目账本已启用",
    "memory.proposed": "提出了一条记忆候选", "memory.reviewed": "完成了一次记忆审核",
    "codex.note": "整理了一条工作记忆", "taste.card.reviewed": "品味卡有了新的判断",
    "taste.reviewed": "完成了一次品味审核",
    "test.failed": "验证发现了问题", "test.passed": "验证已通过",
    "task.updated": "工作进度有了变化", "project.constraint": "记录了一项项目约定",
    "taste.authored": "记录了你明确表达的偏好", "taste.proposed": "发现了一项可能的偏好",
}


def event_label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, label(event_type))
