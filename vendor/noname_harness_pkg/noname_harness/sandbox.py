"""Execution world / sandbox: real side effects inside a physical boundary.

This is where real work happens -- reading and writing files, running
commands.  The guarantees here are **physical**, not a model's promise:

- **Workspace containment.**  Every file path is resolved and confined to the
  configured workspace root (the same boundary the store enforces).  A path
  that escapes simply cannot be touched -- there is no flag to turn this off.
- **Command allow-listing + timeouts.**  Only explicitly allowed command
  prefixes run, each with a hard timeout.  Anything else is refused before a
  process ever starts.
- **Everything is evidence.**  Reads and writes produce events and evidence
  spans in the ledger, so a real side effect is always traceable.

The sandbox **produces tools** that are registered in the
:class:`~noname_harness.tools.ToolRegistry`, so the approval gate is a second,
independent line of defence on top of the sandbox: reads run freely, writes
need approval, command execution is destructive and always gated.  A caller
must go through both the sandbox boundary and the approval gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Sequence

from .models import EvidenceInput
from .store import HarnessStore, WorkspaceBoundaryError
from .tools import Tool, ToolRegistry, ToolSchema

# Default allow-list: strictly read-only commands with no execution hook.
#
# Filtering by executable name is NOT enough: ``git`` and ``find`` are
# themselves arbitrary-execution primitives (``git -c alias.x='!cmd' x``,
# ``git -c core.fsmonitor=...``, a malicious ``[alias]`` in the workspace's own
# ``.git/config``, ``find -exec``/``-delete``).  Their exec surface cannot be
# enumerated, so they are NOT in the default list.  Only commands that purely
# read and have no run-a-program / write-file flag are safe defaults.
#
# Interpreters (python, node, sh, ...), test runners (pytest, ...), git, find,
# sed, awk, xargs, and anything with an exec/write hook are all absent.  A
# deployment that needs one must pass ``allowed_commands`` explicitly and
# accept that the boundary then rests on the approval gate alone.
#
# Even a read-only command can exfiltrate ``/etc/passwd`` via a path argument,
# so every path-like argument is additionally confined to the workspace (see
# run_command).  The denylist per command blocks known write/exec knobs.
DEFAULT_ALLOWED_COMMANDS: dict[str, tuple[str, ...]] = {
    "ls": (),
    "cat": (),
    "echo": (),
    "grep": (),
    "wc": (),
    "head": (),
    "tail": (),
    "pwd": (),
    "date": (),
}


class SandboxError(Exception):
    """Raised when a sandbox boundary would be crossed."""


class Sandbox:
    """A physical execution boundary over the configured workspace."""

    def __init__(
        self,
        store: HarnessStore,
        *,
        allowed_commands: Sequence[str] | None = None,
        command_timeout: float = 30.0,
        max_output_chars: int = 100_000,
    ):
        self.store = store
        if allowed_commands is None:
            self.allowed_commands = dict(DEFAULT_ALLOWED_COMMANDS)
        elif isinstance(allowed_commands, dict):
            self.allowed_commands = dict(allowed_commands)
        else:
            # A bare sequence means "allowed, with no extra argument filter".
            self.allowed_commands = {name: () for name in allowed_commands}
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        self.command_timeout = command_timeout
        self.max_output_chars = max_output_chars

    # ------------------------------------------------------------------
    # file operations (physically confined to the workspace)
    # ------------------------------------------------------------------
    def read_file(self, path: str, *, session_id: str) -> dict[str, Any]:
        """Read a file inside the workspace, recording it as evidence."""

        import stat as _stat

        target = self.store.validate_workspace_path(path)
        # Only regular files may be read: a FIFO or device node passes is_file()
        # for some types but read_bytes() would block forever -- a trivial DoS.
        if not target.exists() or not _stat.S_ISREG(target.stat().st_mode):
            raise SandboxError(f"not a readable regular file inside the workspace: {path}")
        relative = str(target.relative_to(Path(self.store.project()["workspace_root"])))
        raw = target.read_bytes()
        # Binary safety: never crash on non-UTF-8 content.  Undecodable bytes
        # are stored losslessly as hex with an explicit encoding marker, so a
        # binary read is still evidence rather than a silent ledger gap.
        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
            evidence_text = text
        except UnicodeDecodeError:
            encoding = "hex"
            text = raw.hex()
            evidence_text = raw.hex()
        truncated = len(evidence_text) > self.max_output_chars
        evidence = evidence_text[: self.max_output_chars]
        self.store.append_event(
            session_id,
            "file.read",
            {
                "path": relative,
                "size": len(raw),
                "encoding": encoding,
                "truncated": truncated,
                "evidence_chars": len(evidence),
            },
            [EvidenceInput(evidence, f"file://{relative}")],
        )
        return {"path": relative, "content": text, "size": len(raw), "encoding": encoding}

    def write_file(self, path: str, content: str, *, session_id: str) -> dict[str, Any]:
        """Write a file inside the workspace, recording the change as evidence.

        The workspace boundary is enforced before anything is written; a path
        outside the root is refused, and the harness database's own files are
        off-limits.
        """

        # Delegate to the store's shared O_NOFOLLOW writer: validation confines
        # the path, and the write closes the TOCTOU window against a symlink
        # swap, so check and write are atomic for the final component.
        target = self.store.write_text_nofollow(path, content)
        relative = str(target.relative_to(Path(self.store.project()["workspace_root"])))
        self.store.append_event(
            session_id,
            "artifact.changed",
            {"path": relative, "change": "written via sandbox", "size": len(content)},
            [EvidenceInput(content[: self.max_output_chars], f"file://{relative}")],
        )
        return {"path": relative, "size": len(content)}

    # ------------------------------------------------------------------
    # command execution (allow-listed, timed, captured)
    # ------------------------------------------------------------------
    def run_command(
        self,
        command: Sequence[str],
        *,
        session_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run an allow-listed command inside the workspace with a hard timeout.

        The command runs with the workspace as its working directory, its
        output captured and recorded.  A non-allow-listed command or a timeout
        is refused/aborted without touching anything outside the boundary.
        """

        if not command:
            raise SandboxError("command cannot be empty")
        raw_executable = str(command[0])
        # A path separator means the caller named a specific binary (./git,
        # /usr/bin/git, workspace/git).  Resolving it by basename would let a
        # workspace-local or arbitrary executable run under a trusted name, so
        # only bare command names resolved via PATH are allowed.
        if "/" in raw_executable or "\\" in raw_executable:
            raise SandboxError(
                f"command must be a bare name resolved via PATH, not a path: {raw_executable!r}"
            )
        executable = raw_executable
        if executable not in self.allowed_commands:
            raise SandboxError(
                f"command '{executable}' is not in the allow-list: {sorted(self.allowed_commands)}"
            )
        # Argument-level guard: even an allow-listed command is refused if any
        # argument matches a forbidden execution/write knob for that command.
        forbidden = self.allowed_commands.get(executable, ())
        for arg in command[1:]:
            token = str(arg)
            for pattern in forbidden:
                if token == pattern or token.startswith(pattern + "="):
                    raise SandboxError(
                        f"argument '{token}' gives '{executable}' an execution/write "
                        "capability and is refused"
                    )
        # Path confinement: even a read-only command can exfiltrate secrets via
        # a path argument (``cat /etc/passwd``).  Every argument that resolves
        # to a filesystem path must stay inside the workspace; anything that
        # escapes (absolute outside path, ``..`` traversal, a symlink pointing
        # out) is refused before the process starts.
        for arg in command[1:]:
            self._confine_argument_path(str(arg))
        effective_timeout = timeout if timeout is not None else self.command_timeout
        if effective_timeout <= 0:
            raise SandboxError("timeout must be positive")
        root = self.store.project()["workspace_root"]
        import os
        import signal

        def _decode(data: bytes | None) -> str:
            if not data:
                return ""
            return data.decode("utf-8", errors="replace")

        # Start the child in its own process group so a timeout kills the whole
        # group, not just the direct child -- detached grandchildren cannot
        # outlive the timeout and keep running outside the boundary.
        try:
            process = subprocess.Popen(
                list(command),
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            # A failed execve (e.g. command not found) is still an execution
            # attempt: record it and surface a clean SandboxError, not a raw
            # FileNotFoundError that skips the ledger.
            self.store.append_event(
                session_id,
                "tool.failed",
                {"command": list(command), "error": str(exc)},
            )
            raise SandboxError(f"failed to start command '{executable}': {exc}") from exc
        timed_out = False
        try:
            stdout_b, stderr_b = process.communicate(timeout=effective_timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            # Bound the pipe drain: a detached grandchild holding the pipe open
            # must not turn the "hard" timeout into an unbounded wait.  Drain
            # with a grace period, then abandon the pipes.
            try:
                stdout_b, stderr_b = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout_b, stderr_b = b"", b""
            returncode = None
        stdout = _decode(stdout_b)
        stderr = _decode(stderr_b)

        output = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        output = output[: self.max_output_chars]
        payload = {
            "command": list(command),
            "executable": executable,
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout_chars": len(stdout),
            "stderr_chars": len(stderr),
        }
        # The event type reflects the outcome honestly: a non-zero exit or a
        # timeout is a failure, not a silent "completed".
        succeeded = not timed_out and returncode == 0
        self.store.append_event(
            session_id,
            "tool.completed" if succeeded else "tool.failed",
            payload,
            [EvidenceInput(output, f"cmd://{executable}")],
        )
        if timed_out:
            raise SandboxError(f"command '{executable}' timed out after {effective_timeout}s")
        return {
            "command": list(command),
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    # ------------------------------------------------------------------
    # tools: the sandbox's operations, gated through the ToolRegistry
    # ------------------------------------------------------------------
    def _confine_argument_path(self, token: str) -> None:
        """Refuse an argument that points outside the workspace.

        Only tokens that look like paths are checked: absolute paths, and
        relative paths that exist (or whose parent exists) relative to the
        workspace.  Flags, globs and bare words are left alone.  A token that
        resolves outside the root is a boundary crossing.
        """

        if token.startswith("-"):
            return
        root = Path(self.store.project()["workspace_root"]).resolve()
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            return
        if not (resolved.exists() or resolved.parent.exists()):
            return
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise SandboxError(
                f"argument path escapes the workspace: {token!r}"
            ) from exc

    def tools(self, *, session_id: str) -> list[Tool]:
        """Return the sandbox's operations as approval-gated tools.

        These register into the normal :class:`ToolRegistry`, so the approval
        gate is a second line of defence: reads run freely, writes need
        approval, command execution is destructive and always gated.
        """

        return [
            Tool(
                ToolSchema(
                    name="sandbox.read_file",
                    description="Read a file inside the workspace",
                    input_schema={"path": "string"},
                ),
                execute=lambda a: self.read_file(a["path"], session_id=session_id)["content"],
                permission="read",
                approval="never",
                scope="session",
                session_id=session_id,
            ),
            Tool(
                ToolSchema(
                    name="sandbox.write_file",
                    description="Write a file inside the workspace (approval required)",
                    input_schema={"path": "string", "content": "string"},
                ),
                execute=lambda a: self.write_file(a["path"], a["content"], session_id=session_id)["path"],
                permission="write",
                approval="always",
                scope="session",
                session_id=session_id,
            ),
            Tool(
                ToolSchema(
                    name="sandbox.run_command",
                    description="Run an allow-listed command in the workspace (approval required)",
                    input_schema={"command": "array"},
                ),
                execute=lambda a: self.run_command(
                    [str(part) for part in a["command"]], session_id=session_id
                ),
                permission="destructive",
                approval="always",
                scope="session",
                session_id=session_id,
            ),
        ]

    def register_tools(self, registry: ToolRegistry, *, session_id: str) -> list[str]:
        """Register the sandbox's tools into a registry; returns their names."""

        names = []
        for tool in self.tools(session_id=session_id):
            registry.register(tool)
            names.append(tool.schema.name)
        return names
