"""Optimistic concurrency tokens for human review surfaces."""

from __future__ import annotations

import hashlib
import json
from typing import Any


class StaleReviewError(ValueError):
    """The candidate changed after the human opened it."""


def review_version(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def check_review_version(record: dict[str, Any], expected: str | None) -> None:
    if expected is not None and review_version(record) != expected:
        raise StaleReviewError("候选内容或审核状态已改变，请刷新账本后重新判断。")
