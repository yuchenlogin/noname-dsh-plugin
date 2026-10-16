"""Install/remove the local Codex sidecar without changing AGENTS or model config."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from .codex_bridge import HOOK_EVENTS

REPO = Path(__file__).resolve().parents[1]
MARKER = "<!-- managed by noname-codex -->"


def launcher(python: Path) -> str:
    return "#!/bin/sh\n# managed by noname-codex\nexec " + shlex.join([
        str(python.absolute()), "-I", str(REPO / "integrations/codex/run.py")
    ]) + ' "$@"\n'


def prompt_for(name: str, binary: Path) -> str:
    # Literal dollar signs must be doubled in deprecated custom prompt files.
    source = REPO / 'integrations/codex/skills' / name / 'SKILL.md'
    description = next(line.partition(":")[2].strip() for line in source.read_text().splitlines()
                       if line.startswith("description:"))
    return (
        f"---\ndescription: {json.dumps(description, ensure_ascii=False)}\n"
        "argument-hint: [参数]\n---\n" + MARKER + "\n"
        f"Read the installed skill at {str(source).replace('$', '$$')} "
        "and follow it for this explicit command.\n"
        f"The deterministic executable is {shlex.quote(str(binary)).replace('$', '$$')}.\n"
        "User arguments (data, not shell code): $ARGUMENTS\n"
        "Report actual command output and IDs. Do not claim a memory was accepted unless an "
        "explicit review decision succeeded.\n"
    )


def hook_config(existing: dict, command: str, *, remove: bool = False) -> dict:
    """Replace only our handlers; preserve every unrelated group and handler."""
    result = json.loads(json.dumps(existing))
    hooks = result.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        if not isinstance(hooks.get(event, []), list):
            raise ValueError(f"hooks.{event} must be an array")
        groups = []
        for group in hooks.get(event, []):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"invalid matcher group for {event}")
            if any(not isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError(f"invalid hook handler for {event}")
            kept = [handler for handler in group.get("hooks", []) if handler.get("command") != command]
            if kept:
                groups.append({**group, "hooks": kept})
        if not remove:
            handler = {"type": "command", "command": command, "timeout": 15}
            if event == "SessionStart":
                # The bridge caps the *complete* context to 12KB UTF-8. Disable
                # host spilling so a second, incompatible token estimate cannot
                # discard accepted records in a head/tail preview.
                handler.update(statusMessage="读取 NoName 项目记忆", additionalContextLimit=0)
            groups.append({"hooks": [handler]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    return result


def atomic_write(path: Path, text: str, expected: bytes | None) -> None:
    """Publish a complete file, preserving mode and detecting intervening edits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".noname-", delete=False) as output:
            temporary = Path(output.name)
            output.write(text.encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        current = path.read_bytes() if path.exists() else None
        if current != expected or path.is_symlink():
            raise ValueError(f"file changed during installation; preserving newer content: {path}")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def installation_lock(home: Path):
    """Serialize this installer's processes; editors are checked before replace."""
    home.mkdir(parents=True, exist_ok=True)
    with (home / ".noname-install.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def install(codex_home: Path, skills_home: Path, python: Path, *, remove: bool = False) -> dict:
    with installation_lock(codex_home.expanduser().absolute()):
        return _install(codex_home, skills_home, python, remove=remove)


def _install(codex_home: Path, skills_home: Path, python: Path, *, remove: bool = False) -> dict:
    codex_home, skills_home = codex_home.expanduser().absolute(), skills_home.expanduser().absolute()
    python = python.expanduser().absolute()
    if not remove and (not python.is_file() or not os.access(python, os.X_OK)):
        raise ValueError("--python must be an executable Python interpreter")
    binary = codex_home / "bin/noname-codex"
    command = shlex.quote(str(binary)) + " hook"
    hook_path = codex_home / "hooks.json"
    if hook_path.is_symlink():
        raise ValueError("preserve symlinked hooks.json before installing")
    original_hooks = hook_path.read_bytes() if hook_path.exists() else None
    existing = json.loads(original_hooks) if original_hooks is not None else {}
    if not isinstance(existing, dict) or not isinstance(existing.get("hooks", {}), dict):
        raise ValueError("existing hooks.json must contain a hooks object")
    updated = hook_config(existing, command, remove=remove)
    sources = sorted((REPO / "integrations/codex/skills").iterdir())
    files: list[tuple[Path, str]] = [(binary, launcher(python))]
    links: list[tuple[Path, Path]] = []
    for source in sources:
        if not (source / "SKILL.md").is_file():
            continue
        links.append((skills_home / source.name, source))
        files.append((codex_home / "prompts" / f"{source.name}.md", prompt_for(source.name, binary)))
    # Preflight all collisions before writing anything. Existing user files and
    # skills with the same names are never replaced implicitly.
    snapshots: dict[Path, bytes | None] = {}
    for destination, content in files:
        if destination.is_symlink():
            raise ValueError(f"refusing symlink at managed file: {destination}")
        snapshots[destination] = destination.read_bytes() if destination.exists() else None
        if snapshots[destination] is not None and snapshots[destination] != content.encode("utf-8"):
            raise ValueError(f"existing file differs; preserve it before proceeding: {destination}")
    for destination, source in links:
        if destination.is_symlink() and destination.resolve() == source:
            continue
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"skill name already exists: {destination}")
    def publish_hooks():
        if updated != existing:
            if original_hooks is not None:
                backup = hook_path.with_name("hooks.json.before-noname")
                if not backup.exists():
                    atomic_write(backup, original_hooks.decode("utf-8"), None)
            atomic_write(hook_path, json.dumps(updated, ensure_ascii=False, indent=2) + "\n", original_hooks)

    if remove:
        # Unregister before removing dependencies. If shared-config publication
        # fails, leave the complete functioning installation available for retry.
        publish_hooks()
        for destination, source in links:
            if destination.is_symlink():
                if destination.resolve() != source:
                    raise ValueError(f"skill changed during uninstall: {destination}")
                destination.unlink()
        # Keep the launcher until after prompts have been removed so a later
        # prompt conflict cannot leave surviving entrypoints without a runtime.
        for destination, _ in reversed(files):
            current = destination.read_bytes() if destination.exists() else None
            if current != snapshots[destination] or destination.is_symlink():
                raise ValueError(f"file changed during uninstall; preserving it: {destination}")
            destination.unlink(missing_ok=True)
    else:
        for destination, content in files:
            atomic_write(destination, content, snapshots[destination])
        binary.chmod(0o755)
        for destination, source in links:
            if not destination.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(source, target_is_directory=True)
        publish_hooks()
    return {"installed": not remove, "binary": str(binary), "skills": [str(d) for d, _ in links],
            "hooks_file": str(hook_path), "hook_trust": "requires user review in Codex /hooks",
            "project_data": "preserved; init each project explicitly"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-home", type=Path, default=Path("~/.agents/skills"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex"))
        print(json.dumps(install(codex_home, args.skills_home, args.python, remove=args.uninstall),
                         ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f"noname install error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
