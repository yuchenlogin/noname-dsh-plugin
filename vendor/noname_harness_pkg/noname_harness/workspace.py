"""Read-only snapshots of the configured workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def git_snapshot(workspace_root: str | Path) -> dict[str, Any]:
    """Collect git status without changing the workspace.

    A project does not have to be a git repository.  In that case the result
    is still a useful, explicit fact for a handoff package.
    """

    root = Path(workspace_root).expanduser().resolve()
    try:
        status = subprocess.run(
            # The pathspec keeps a workspace nested inside a larger checkout
            # from leaking sibling paths into the handoff snapshot.
            ["git", "status", "--short", "--branch", "--untracked-files=all", "--", "."],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return {
            "git_available": False,
            "workspace_root": str(root),
            "reason": str(exc),
        }

    if status.returncode != 0:
        return {
            "git_available": False,
            "workspace_root": str(root),
            "reason": status.stderr.strip() or "not a git repository",
            "status_code": status.returncode,
        }

    return {
        "git_available": True,
        "workspace_root": str(root),
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "status": status.stdout,
        "status_code": status.returncode,
    }
