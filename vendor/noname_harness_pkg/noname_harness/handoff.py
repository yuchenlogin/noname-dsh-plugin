"""Small, transparent heuristics for a fresh agent's first next-step hints."""

from __future__ import annotations

from typing import Any, Iterable


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("payload")
    return value if isinstance(value, dict) else {}


def _same_test_target(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Match test events conservatively when either side is a suite result."""

    first_path = _payload(first).get("path")
    second_path = _payload(second).get("path")
    return not first_path or not second_path or first_path == second_path


def _is_successful_test_result(item: dict[str, Any]) -> bool:
    event_type = item.get("event_type")
    if event_type == "test.passed":
        return True
    if event_type != "test.completed":
        return False
    payload = _payload(item)
    status = str(payload.get("status", "")).lower()
    return payload.get("success") is True or status in {"ok", "pass", "passed", "success"}


def infer_next_steps(
    events: Iterable[dict[str, Any]],
    mid_state: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return advisory next-step candidates with explicit provenance.

    These rules are intentionally modest.  They make the handoff useful before
    an LLM planner exists, while keeping the distinction between an observed
    fact and a suggested action visible in the package.
    """

    candidates: list[dict[str, Any]] = []

    for item in mid_state:
        content = item.get("content")
        if isinstance(content, dict) and content.get("next"):
            candidates.append(
                {
                    "text": str(content["next"]),
                    "reason": "accepted mid-layer task state",
                    "source_event_ids": list(item.get("source_event_ids", [])),
                    "confidence": 0.9,
                }
            )

    chronological = sorted(
        list(events),
        key=lambda item: (item.get("occurred_at", ""), item.get("seq", 0)),
    )
    for index, item in enumerate(chronological):
        event_type = item.get("event_type")
        payload = _payload(item)
        event_id = item.get("event_id")
        if event_type == "test.failed":
            later_test_results = [
                later
                for later in chronological[index + 1 :]
                if later.get("event_type") in {"test.failed", "test.passed", "test.completed"}
                and _same_test_target(item, later)
            ]
            if any(later.get("event_type") == "test.failed" for later in later_test_results):
                # Only the newest failure for one test target should advise
                # the next agent; older repetitions remain in the evidence.
                continue
            if any(_is_successful_test_result(later) for later in later_test_results):
                continue
            path = payload.get("path", "失败的测试")
            candidates.append(
                {
                    "text": f"检查 {path} 的失败原因，并对照最近一次改动",
                    "reason": "the latest unresolved result for this test is a failure",
                    "source_event_ids": [event_id] if event_id else [],
                    "confidence": 0.65,
                }
            )
        elif event_type == "artifact.changed" and payload.get("path"):
            later_types = {
                later.get("event_type")
                for later in chronological[index + 1 :]
            }
            if later_types & {"test.failed", "test.passed", "test.completed"}:
                continue
            candidates.append(
                {
                    "text": f"运行覆盖 {payload['path']} 的相关测试",
                    "reason": "an artifact changed without a later test result",
                    "source_event_ids": [event_id] if event_id else [],
                    "confidence": 0.55,
                }
            )
        elif event_type == "tool.failed":
            candidates.append(
                {
                    "text": "检查工具失败原因，再决定重试或换一条路径",
                    "reason": "the latest observed event is a tool failure",
                    "source_event_ids": [event_id] if event_id else [],
                    "confidence": 0.5,
                }
            )

    # Keep the first occurrence of the same suggestion while preserving the
    # stronger, more explicit mid-layer hint when one exists.
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(candidate["text"], candidate)
    return list(unique.values())
