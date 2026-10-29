"""Versioned, pre-generation writing preferences shared by host integrations.

These are explicit runtime settings, separate from evidence and inferred taste.
The Markdown resource is also shipped unchanged in the DSH kernel snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Literal

WritingStyle = Literal["direct", "off"]
POLICY_FILE = Path(__file__).with_name("policies") / "direct-writing.md"


def validate_writing_style(style: str) -> WritingStyle:
    if style not in ("direct", "off"):
        raise ValueError("writing style must be 'direct' or 'off'")
    return style  # type: ignore[return-value]


def writing_policy(style: str = "direct") -> str:
    """Return instructions before generation; never process model output."""
    return POLICY_FILE.read_text(encoding="utf-8").strip() if validate_writing_style(style) == "direct" else ""


def codex_preferences_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "noname/writing-style.json"


def codex_writing_style() -> WritingStyle:
    path = codex_preferences_path()
    if not path.exists():
        return "direct"
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or set(data) != {"schema_version", "writing_style"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise ValueError(f"invalid NoName writing preferences: {path}")
    return validate_writing_style(data["writing_style"])


def set_codex_writing_style(style: str) -> None:
    """Atomically publish the single user-owned setting. Last explicit write wins."""
    style = validate_writing_style(style)
    path = codex_preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"preserve symlinked writing preferences before changing them: {path}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".writing-style-", delete=False) as f:
            temporary = Path(f.name)
            f.write((json.dumps({"schema_version": 1, "writing_style": style}) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def codex_writing_status() -> dict:
    style = codex_writing_style()
    policy = writing_policy(style)
    return {"style": style, "source": "settings" if codex_preferences_path().exists() else "default",
            "settings_file": str(codex_preferences_path()), "policy_version": 1,
            "policy_sha256": hashlib.sha256(policy.encode("utf-8")).hexdigest() if policy else None,
            "injection_events": ["SessionStart", "UserPromptSubmit"],
            "delivery": "prepared by hook; execution requires host hook support and trust"}
