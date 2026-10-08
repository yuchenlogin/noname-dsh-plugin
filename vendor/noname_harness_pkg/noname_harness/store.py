"""SQLite-backed evidence, state and context-package store.

The prototype deliberately keeps the source of truth small and explicit:
events and evidence are append-only, while durable state is represented by
immutable revisions.  A future model-powered curator can use the same store
without changing these contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .handoff import infer_next_steps
from .models import EvidenceInput, Event, ModelProfile
from .workspace import git_snapshot


SCHEMA_VERSION = 7
VALID_LAYERS = {"high", "mid"}
VALID_REVIEW_ACTIONS = {"accept", "reject", "edit", "defer", "retire"}
VALID_TASTE_TRACKS = {"authored", "adopted"}
VALID_TASTE_SCOPES = {"user", "project"}
VALID_TASTE_STATUSES = {"candidate", "active", "paused", "retired"}
VALID_TASTE_ACTIONS = {"adopt", "edit", "pause", "resume", "retire"}
VALID_CARD_TRACKS = {"authored", "adopted", "mixed"}
VALID_CARD_STATUSES = {"candidate", "active", "paused", "retired"}
VALID_CARD_ACTIONS = {"accept", "edit", "pause", "resume", "retire", "split"}

# INSERT-boundary guards for supersede chains, shared by the initial schema and
# the v4 -> v5 migration so both paths stay byte-for-byte identical.
_SUPERSEDE_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS taste_records_supersede_guard
BEFORE INSERT ON taste_records
WHEN NEW.supersedes_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'supersede target must be an existing, different taste record')
    WHERE NEW.supersedes_id = NEW.id
       OR NOT EXISTS (SELECT 1 FROM taste_records WHERE id = NEW.supersedes_id);
END;

CREATE TRIGGER IF NOT EXISTS taste_cards_supersede_guard
BEFORE INSERT ON taste_cards
WHEN NEW.supersedes_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'supersede target must be an existing, different taste card')
    WHERE NEW.supersedes_id = NEW.id
       OR NOT EXISTS (SELECT 1 FROM taste_cards WHERE id = NEW.supersedes_id);
END;

CREATE TRIGGER IF NOT EXISTS state_revisions_supersede_guard
BEFORE INSERT ON state_revisions
WHEN NEW.supersedes_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'supersede target must be an existing, different state revision in the same layer and key')
    WHERE NEW.supersedes_id = NEW.id
       OR NOT EXISTS (
           SELECT 1 FROM state_revisions
           WHERE id = NEW.supersedes_id
             AND layer = NEW.layer
             AND logical_key = NEW.logical_key
       );
END;
"""


class WorkspaceBoundaryError(ValueError):
    """Raised when a path leaves the configured project workspace."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decode(value: str) -> Any:
    return json.loads(value)


def _parse_instant(value: str, field: str) -> str:
    """Parse an ISO-8601 instant and return its normalised UTC form.

    Bitemporal bounds are compared chronologically, not lexicographically, so
    they must be real timestamps.  Naive input is assumed to be UTC; aware
    input is converted.  Anything unparseable is rejected rather than stored.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO-8601 instant: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")



def _check_finite_vector(vector: Any, *, context: str) -> None:
    """Refuse non-finite embedding vectors before they can poison a projection.

    A NaN/inf component makes every cosine similarity computed against the
    vector NaN, silently corrupting recall/ranking order.  The vendor adapter
    checks at the network seam; this is the defence-in-depth check at the
    store seam so no embedding implementation can bypass it.
    """

    try:
        components = list(vector)
    except TypeError:
        raise TypeError(
            f"{context}: embedding function must return a sequence of floats, "
            f"got {type(vector).__name__}"
        ) from None
    for component in components:
        if not isinstance(component, (int, float)) or isinstance(component, bool):
            raise TypeError(
                f"{context}: embedding vector components must be numbers, "
                f"got {type(component).__name__}"
            )
        if not math.isfinite(component):
            raise ValueError(
                f"{context}: embedding vector contains a non-finite component "
                "({!r}); refusing to poison the recall projection".format(component)
            )


def _is_read_only_sql(sql: str) -> bool:
    """Return whether a statement is a plain read (SELECT / WITH / read PRAGMA).

    A public query seam must never be a write backdoor around the kernel's
    review gates.  Only reads are allowed; PRAGMA is limited to its query
    forms (``PRAGMA table_info(...)`` etc.), never state-changing assignments
    like ``PRAGMA foreign_keys = OFF``.
    """

    stripped = sql.lstrip().lower()
    if stripped.startswith("select") or stripped.startswith("with"):
        return True
    if stripped.startswith("pragma"):
        # A PRAGMA containing '=' is an assignment (a write).  Query forms
        # either have no '=' or use the function-call form "name(...)".
        return "=" not in stripped
    return False


class HarnessStore:
    """The local fact base for one NoName project.

    Concurrency note: a HarnessStore wraps a single SQLite connection.  SQLite
    serializes writers across connections (WAL), so **each actor (thread,
    process, CLI, agent) should open its own HarnessStore on the same database
    file** -- events written by one connection are visible to the others, which
    is exactly how cross-actor cancellation and cross-session handoffs work.
    Do not share one HarnessStore (one connection) across threads: use one per
    actor.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_available = False
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        try:
            self._ensure_schema()
        except Exception:
            # A half-initialised store must not leak its connection: if schema
            # setup fails (read-only fs, corrupt file), close before the
            # exception propagates so the caller is not left holding an open
            # handle on an object that never finished construction.
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "HarnessStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Public write transaction for services built on the store."""

        with self._transaction() as connection:
            yield connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _ensure_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS harness_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE (session_id, seq)
        );

        CREATE TABLE IF NOT EXISTS evidence_spans (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES session_events(id),
            artifact_uri TEXT,
            start_offset INTEGER,
            end_offset INTEGER,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS state_proposals (
            id TEXT PRIMARY KEY,
            layer TEXT NOT NULL CHECK (layer IN ('high', 'mid')),
            kind TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            content_json TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL,
            conflict_with_ids_json TEXT NOT NULL,
            proposed_by TEXT NOT NULL,
            confidence REAL,
            proposal_reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proposal_reviews (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES state_proposals(id),
            action TEXT NOT NULL CHECK (action IN ('accept', 'reject', 'edit', 'defer', 'retire')),
            reviewer_id TEXT NOT NULL,
            edited_content_json TEXT,
            reason TEXT,
            reviewed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS state_revisions (
            id TEXT PRIMARY KEY,
            layer TEXT NOT NULL CHECK (layer IN ('high', 'mid')),
            logical_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            content_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
            source_event_ids_json TEXT NOT NULL,
            origin_proposal_id TEXT REFERENCES state_proposals(id),
            supersedes_id TEXT REFERENCES state_revisions(id),
            approved_by TEXT NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_packages (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            session_id TEXT,
            package_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TRIGGER IF NOT EXISTS session_events_append_only_update
        BEFORE UPDATE ON session_events
        BEGIN
            SELECT RAISE(ABORT, 'session_events is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS project_append_only_update
        BEFORE UPDATE ON project
        BEGIN
            SELECT RAISE(ABORT, 'project metadata is append-only');
        END;

        CREATE TABLE IF NOT EXISTS taste_cards (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            attitude TEXT NOT NULL,
            track TEXT NOT NULL CHECK (track IN ('authored', 'adopted', 'mixed')),
            scope TEXT NOT NULL CHECK (scope IN ('user', 'project')),
            taste_ids_json TEXT NOT NULL,
            representative_evidence_json TEXT NOT NULL,
            tensions TEXT,
            influence TEXT,
            image_json TEXT,
            status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'paused', 'retired')),
            valid_from TEXT,
            valid_to TEXT,
            last_confirmed_at TEXT NOT NULL,
            supersedes_id TEXT REFERENCES taste_cards(id),
            origin TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );

        -- Vector index for semantic recall.  This is a REBUILDABLE PROJECTION:
        -- the embedding of each event's searchable text, stored so semantic
        -- recall does not re-embed on every query.  It can be dropped and
        -- rebuilt from session_events/evidence_spans at any time; it is never
        -- the source of truth.
        CREATE TABLE IF NOT EXISTS event_embeddings (
            event_id TEXT PRIMARY KEY REFERENCES session_events(id),
            dimensions INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            model_id TEXT NOT NULL DEFAULT 'local_hash',
            embedded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS taste_records (
            id TEXT PRIMARY KEY,
            track TEXT NOT NULL CHECK (track IN ('authored', 'adopted')),
            scope TEXT NOT NULL CHECK (scope IN ('user', 'project')),
            content_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'paused', 'retired')),
            source_event_ids_json TEXT NOT NULL,
            supersedes_id TEXT REFERENCES taste_records(id),
            origin TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            reason TEXT,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS taste_reviews (
            id TEXT PRIMARY KEY,
            taste_id TEXT NOT NULL REFERENCES taste_records(id),
            action TEXT NOT NULL CHECK (action IN ('adopt', 'edit', 'pause', 'resume', 'retire')),
            reviewer_id TEXT NOT NULL,
            edited_content_json TEXT,
            reason TEXT,
            reviewed_at TEXT NOT NULL
        );

        CREATE TRIGGER IF NOT EXISTS project_append_only_delete
        BEFORE DELETE ON project
        BEGIN
            SELECT RAISE(ABORT, 'project metadata is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS session_events_append_only_delete
        BEFORE DELETE ON session_events
        BEGIN
            SELECT RAISE(ABORT, 'session_events is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS evidence_spans_append_only_update
        BEFORE UPDATE ON evidence_spans
        BEGIN
            SELECT RAISE(ABORT, 'evidence_spans is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS evidence_spans_append_only_delete
        BEFORE DELETE ON evidence_spans
        BEGIN
            SELECT RAISE(ABORT, 'evidence_spans is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS state_revisions_append_only_update
        BEFORE UPDATE ON state_revisions
        BEGIN
            SELECT RAISE(ABORT, 'state_revisions is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS state_revisions_append_only_delete
        BEFORE DELETE ON state_revisions
        BEGIN
            SELECT RAISE(ABORT, 'state_revisions is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS state_proposals_append_only_update
        BEFORE UPDATE ON state_proposals
        BEGIN
            SELECT RAISE(ABORT, 'state_proposals is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS state_proposals_append_only_delete
        BEFORE DELETE ON state_proposals
        BEGIN
            SELECT RAISE(ABORT, 'state_proposals is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS proposal_reviews_append_only_update
        BEFORE UPDATE ON proposal_reviews
        BEGIN
            SELECT RAISE(ABORT, 'proposal_reviews is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS proposal_reviews_append_only_delete
        BEFORE DELETE ON proposal_reviews
        BEGIN
            SELECT RAISE(ABORT, 'proposal_reviews is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS context_packages_append_only_update
        BEFORE UPDATE ON context_packages
        BEGIN
            SELECT RAISE(ABORT, 'context_packages is append-only');
        END;

        CREATE UNIQUE INDEX IF NOT EXISTS state_revisions_one_child_per_parent
            ON state_revisions(supersedes_id) WHERE supersedes_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS taste_records_one_child_per_parent
            ON taste_records(supersedes_id) WHERE supersedes_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS taste_cards_one_child_per_parent
            ON taste_cards(supersedes_id) WHERE supersedes_id IS NOT NULL;

        CREATE TRIGGER IF NOT EXISTS taste_cards_append_only_update
        BEFORE UPDATE ON taste_cards
        BEGIN
            SELECT RAISE(ABORT, 'taste_cards is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS taste_cards_append_only_delete
        BEFORE DELETE ON taste_cards
        BEGIN
            SELECT RAISE(ABORT, 'taste_cards is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS taste_records_append_only_update
        BEFORE UPDATE ON taste_records
        BEGIN
            SELECT RAISE(ABORT, 'taste_records is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS taste_records_append_only_delete
        BEFORE DELETE ON taste_records
        BEGIN
            SELECT RAISE(ABORT, 'taste_records is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS taste_reviews_append_only_update
        BEFORE UPDATE ON taste_reviews
        BEGIN
            SELECT RAISE(ABORT, 'taste_reviews is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS taste_reviews_append_only_delete
        BEFORE DELETE ON taste_reviews
        BEGIN
            SELECT RAISE(ABORT, 'taste_reviews is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS context_packages_append_only_delete
        BEFORE DELETE ON context_packages
        BEGIN
            SELECT RAISE(ABORT, 'context_packages is append-only');
        END;
        """
        with self._connection:
            self._connection.executescript(schema)
            self._connection.executescript(_SUPERSEDE_GUARD_SQL)
            try:
                self._connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS event_search USING fts5("
                    "event_id UNINDEXED, session_id UNINDEXED, event_type, payload_text, evidence_text)"
                )
                self._fts_available = True
            except sqlite3.OperationalError:
                # FTS5 is an acceleration index, never the source of truth.
                self._fts_available = False
            # event_embeddings is a rebuildable projection, so an idempotent
            # ALTER to add a missing model_id column is safe for databases that
            # created the table before the column existed.
            embedding_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(event_embeddings)")
            }
            if embedding_columns and "model_id" not in embedding_columns:
                self._connection.execute(
                    "ALTER TABLE event_embeddings ADD COLUMN model_id TEXT NOT NULL DEFAULT 'local_hash'"
                )
            current = self._connection.execute(
                "SELECT value FROM harness_meta WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                self._connection.execute(
                    "INSERT INTO harness_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                current_version = int(current["value"])
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Unsupported schema version {current['value']}; expected {SCHEMA_VERSION}"
                    )
                # Migrations are append-only and idempotent: each step upgrades
                # exactly one version, so a chain v1 -> v2 -> v3 always runs in
                # order and can be tested step by step.
                if current_version == 1:
                    columns = {
                        row["name"]
                        for row in self._connection.execute("PRAGMA table_info(state_proposals)")
                    }
                    if "proposal_reason" not in columns:
                        self._connection.execute(
                            "ALTER TABLE state_proposals ADD COLUMN proposal_reason TEXT"
                        )
                    current_version = 2
                if current_version == 2:
                    # v3 introduces the taste layer (taste_records / taste_reviews).
                    # The tables are created by the shared schema above via
                    # CREATE TABLE IF NOT EXISTS, so the version bump itself is
                    # the only durable change required here.
                    current_version = 3
                if current_version == 3:
                    # v4 adds bitemporal validity to durable state revisions:
                    # valid_from / valid_to record when a fact is true in the
                    # real world, while created_at keeps when the system
                    # learned it.  Existing rows stay valid with NULL bounds.
                    columns = {
                        row["name"]
                        for row in self._connection.execute("PRAGMA table_info(state_revisions)")
                    }
                    if "valid_from" not in columns:
                        self._connection.execute(
                            "ALTER TABLE state_revisions ADD COLUMN valid_from TEXT"
                        )
                    if "valid_to" not in columns:
                        self._connection.execute(
                            "ALTER TABLE state_revisions ADD COLUMN valid_to TEXT"
                        )
                    current_version = 4
                if current_version == 4:
                    # v5 hardens the supersede INSERT boundary for existing
                    # databases.  Fresh databases get the same triggers from the
                    # shared schema via CREATE TRIGGER IF NOT EXISTS, so this
                    # step only needs to run them idempotently.
                    self._connection.executescript(_SUPERSEDE_GUARD_SQL)
                    current_version = 5
                if current_version == 5:
                    # v6 introduces taste cards (taste_cards).  The table and
                    # append-only triggers come from the shared schema via
                    # CREATE ... IF NOT EXISTS; the supersede guard is in the
                    # shared guard script.  Only the version bump is durable here.
                    current_version = 6
                if current_version == 6:
                    # v7 adds the event_embeddings vector-index projection.  It
                    # is created by the shared schema via CREATE TABLE IF NOT
                    # EXISTS (and is rebuildable), so only the version bump is
                    # durable here.
                    current_version = 7
                if current_version != SCHEMA_VERSION:  # pragma: no cover - defensive
                    raise RuntimeError(
                        f"Unsupported schema version {current_version}; expected {SCHEMA_VERSION}"
                    )
                self._connection.execute(
                    "UPDATE harness_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )

    def initialize_project(self, workspace_root: str | Path, name: str = "NoName project") -> dict[str, str]:
        root = Path(workspace_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"workspace root must be an existing directory: {root}")
        existing = self._connection.execute("SELECT * FROM project WHERE id = 1").fetchone()
        if existing is not None:
            if Path(existing["workspace_root"]).resolve() != root:
                raise ValueError(
                    f"database already belongs to workspace {existing['workspace_root']}"
                )
            return dict(existing)
        created_at = _now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO project(id, name, workspace_root, created_at) VALUES(1, ?, ?, ?)",
                (name, str(root), created_at),
            )
        return {"id": 1, "name": name, "workspace_root": str(root), "created_at": created_at}

    def project(self) -> dict[str, str]:
        row = self._connection.execute("SELECT * FROM project WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("project is not initialized; run initialize_project first")
        return dict(row)

    def validate_workspace_path(self, path: str | Path) -> Path:
        root = Path(self.project()["workspace_root"]).resolve()
        raw = Path(path).expanduser()
        candidate = (root / raw if not raw.is_absolute() else raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise WorkspaceBoundaryError(
                f"path {candidate} is outside workspace {root}"
            ) from exc
        return candidate

    def validate_output_path(self, path: str | Path) -> Path:
        """Validate a generated-file path without allowing DB replacement."""

        destination = self.validate_workspace_path(path)
        database = self.db_path.expanduser().resolve()
        # Refuse the database and its sidecar files, robustly:
        #  * name match (case-insensitive) covers db, -wal, -shm and -journal;
        #  * inode match (os.path.samefile) covers a hardlink to the database,
        #    which a name check cannot see.
        db_name = database.name.lower()
        sidecar_names = {db_name, f"{db_name}-wal", f"{db_name}-shm", f"{db_name}-journal"}
        if destination.parent == database.parent and destination.name.lower() in sidecar_names:
            raise WorkspaceBoundaryError(
                "refusing to use the harness database or its sidecar files as an output file"
            )
        if destination.exists():
            # Compare by inode against the database AND every sidecar: the live
            # database spans harness.db + -wal (+ -shm/-journal), so a hardlink
            # to any of them is a corruption vector a name check cannot see.
            for protected in self._db_family_paths(database):
                try:
                    if protected.exists() and os.path.samefile(destination, protected):
                        raise WorkspaceBoundaryError(
                            "refusing to write through a hardlink to the harness database"
                        )
                except OSError:
                    raise WorkspaceBoundaryError(
                        f"cannot verify output path is not the database: {destination}"
                    )
            if destination.is_dir():
                raise ValueError("context package output path must be a file")
        return destination

    @staticmethod
    def _db_family_paths(database: Path) -> list[Path]:
        """The database and its WAL-mode sidecar files (corruption targets)."""

        return [
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
            database.with_name(database.name + "-journal"),
        ]

    def write_text_nofollow(self, path: str | Path, content: str) -> Path:
        """Write text inside the workspace without following a final symlink.

        Shared by every writer (context packages, sandbox file writes) so the
        TOCTOU window between path validation and the write stays closed.  The
        open uses O_NOFOLLOW (fail-closed if unavailable) and O_EXCL semantics
        are approximated by refusing an existing final symlink.
        """

        # validate_output_path resolves the path to confirm it stays inside the
        # workspace.  But the *write* must use the un-resolved path with
        # O_NOFOLLOW: resolving first would silently follow a symlink, and a
        # symlink swapped in afterward would redirect the write.  Opening the
        # original path with O_NOFOLLOW refuses a final-component symlink at
        # the moment of open, closing the validate->write TOCTOU window.
        validated = self.validate_output_path(path)
        root = Path(self.project()["workspace_root"]).resolve()
        raw = Path(path).expanduser()
        open_target = raw if raw.is_absolute() else (root / raw)
        open_target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        else:
            raise WorkspaceBoundaryError(
                "O_NOFOLLOW is unavailable; refusing to write without symlink protection"
            )
        try:
            fd = os.open(str(open_target), flags, 0o644)
        except OSError as exc:
            raise WorkspaceBoundaryError(
                f"refusing to write through a symlink or unreadable path: {open_target}"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        return validated

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: Any,
        evidence: Iterable[EvidenceInput] | None = None,
        occurred_at: str | None = None,
    ) -> Event:
        # Events without a project/workspace cannot be safely handed off or
        # checked against the configured boundary.
        self.project()
        # The ledger is str-keyed: anything else (notably bytes, which have a
        # .strip() that would pass a blank check) would be stored as a SQLite
        # BLOB and become an invisible split-brain ledger -- written under one
        # key type, unreachable via the str form.  Fail loudly at the seam.
        if not isinstance(session_id, str):
            raise TypeError(
                f"session_id must be a str, got {type(session_id).__name__}"
            )
        if not isinstance(event_type, str):
            raise TypeError(
                f"event_type must be a str, got {type(event_type).__name__}"
            )
        if not session_id.strip():
            raise ValueError("session_id cannot be empty")
        if not event_type.strip():
            raise ValueError("event_type cannot be empty")
        timestamp = occurred_at or _now()
        evidence_items = list(evidence or [])
        with self._transaction() as connection:
            return self._insert_event(
                connection,
                session_id,
                event_type,
                payload,
                evidence_items,
                timestamp,
            )

    def append_workspace_snapshot(self, session_id: str) -> Event:
        """Record a read-only git/workspace snapshot as low-layer evidence."""

        snapshot = git_snapshot(self.project()["workspace_root"])
        evidence_text = snapshot.get("status", snapshot.get("reason", ""))
        return self.append_event(
            session_id,
            "workspace.snapshot",
            snapshot,
            [EvidenceInput(evidence_text, "git://status")],
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        event_type: str,
        payload: Any,
        evidence: Iterable[EvidenceInput] = (),
        occurred_at: str | None = None,
    ) -> Event:
        """Insert an event inside an existing transaction."""

        timestamp = occurred_at or _now()
        event_id = _id("evt")
        event_hash = _hash({"event_type": event_type, "payload": payload})
        evidence_texts: list[str] = []
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM session_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = int(row["next_seq"])
        connection.execute(
            "INSERT INTO session_events "
            "(id, session_id, seq, event_type, payload_json, occurred_at, content_hash) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (event_id, session_id, seq, event_type, _json(payload), timestamp, event_hash),
        )
        for item in evidence:
            if item.start_offset is not None and item.start_offset < 0:
                raise ValueError("evidence start_offset cannot be negative")
            if item.end_offset is not None and item.end_offset < 0:
                raise ValueError("evidence end_offset cannot be negative")
            if (
                item.start_offset is not None
                and item.end_offset is not None
                and item.end_offset < item.start_offset
            ):
                raise ValueError("evidence end_offset cannot precede start_offset")
            if item.artifact_uri and item.artifact_uri.startswith("file://"):
                self.validate_workspace_path(item.artifact_uri[7:])
            connection.execute(
                "INSERT INTO evidence_spans "
                "(id, event_id, artifact_uri, start_offset, end_offset, content, content_hash, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _id("evd"),
                    event_id,
                    item.artifact_uri,
                    item.start_offset,
                    item.end_offset,
                    item.content,
                    hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                    timestamp,
                ),
            )
            evidence_texts.append(item.content)
            if item.artifact_uri:
                evidence_texts.append(item.artifact_uri)
        if self._fts_available:
            connection.execute(
                "INSERT INTO event_search(event_id, session_id, event_type, payload_text, evidence_text) "
                "VALUES(?, ?, ?, ?, ?)",
                (event_id, session_id, event_type, _json(payload), "\n".join(evidence_texts)),
            )
        return Event(event_id, session_id, seq, event_type, payload, timestamp, event_hash)

    def record_event(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        event_type: str,
        payload: Any,
        occurred_at: str | None = None,
    ) -> Event:
        """Append an event inside an existing transaction.

        This is the sanctioned way for a service (taste, inbox) to write ledger
        events atomically with its own table writes, without touching private
        store internals.
        """

        return self._insert_event(
            connection, session_id, event_type, payload, (), occurred_at
        )

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Run a read-only query against the store for service projections.

        This is a *read* seam: a statement that writes (INSERT/UPDATE/DELETE/
        DDL) would bypass every review gate the kernel enforces, so anything
        that is not a plain SELECT/WITH...SELECT is refused here.  Services
        that need to write go through the explicit store methods instead.
        """

        if not _is_read_only_sql(sql):
            raise ValueError("query() is read-only; use store methods for writes")
        return self._connection.execute(sql, tuple(args)).fetchall()

    def query_one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        """Run a read-only query expected to return at most one row."""

        if not _is_read_only_sql(sql):
            raise ValueError("query_one() is read-only; use store methods for writes")
        return self._connection.execute(sql, tuple(args)).fetchone()

    def get_event(self, event_id: str) -> Event:
        row = self._connection.execute(
            "SELECT * FROM session_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown event: {event_id}")
        return Event(
            row["id"],
            row["session_id"],
            row["seq"],
            row["event_type"],
            _decode(row["payload_json"]),
            row["occurred_at"],
            row["content_hash"],
        )

    def list_events(self, session_id: str | None = None, limit: int = 50) -> list[Event]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if session_id is None:
            rows = self._connection.execute(
                "SELECT * FROM session_events ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM session_events WHERE session_id = ? "
                "ORDER BY seq DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            Event(
                row["id"],
                row["session_id"],
                row["seq"],
                row["event_type"],
                _decode(row["payload_json"]),
                row["occurred_at"],
                row["content_hash"],
            )
            for row in rows
        ]

    def list_work_events(
        self,
        session_id: str | None = None,
        limit: int = 50,
        *,
        include_workspace_snapshots: bool = True,
    ) -> list[Event]:
        """List recent work events while omitting projection bookkeeping.

        A handoff can request only substantive work events and append its
        current snapshot separately, so repeated snapshots do not consume the
        evidence window.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        # Bookkeeping and state-machine noise never belong in the low evidence
        # window: it exists to carry the work itself (diffs, test results,
        # commands, errors), not the loop/tool/plugin/card machinery around it.
        clauses = [
            "event_type NOT LIKE 'memory.%'",
            "event_type != 'context.assembled'",
            "event_type NOT LIKE 'loop.%'",
            "event_type NOT LIKE 'tool.%'",
            "event_type NOT LIKE 'plugin.%'",
            "event_type NOT LIKE 'taste.%'",
        ]
        args: list[Any] = []
        if not include_workspace_snapshots:
            clauses.append("event_type != 'workspace.snapshot'")
        if session_id is not None:
            clauses.append("session_id = ?")
            args.append(session_id)
        order = "seq DESC" if session_id is not None else "occurred_at DESC, rowid DESC"
        args.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM session_events WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order} LIMIT ?",
            tuple(args),
        ).fetchall()
        return [
            Event(
                row["id"],
                row["session_id"],
                row["seq"],
                row["event_type"],
                _decode(row["payload_json"]),
                row["occurred_at"],
                row["content_hash"],
            )
            for row in rows
        ]

    def evidence_for_event(self, event_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM evidence_spans WHERE event_id = ? ORDER BY rowid", (event_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "event_id": row["event_id"],
                "artifact_uri": row["artifact_uri"],
                "start_offset": row["start_offset"],
                "end_offset": row["end_offset"],
                "content": row["content"],
                "content_hash": row["content_hash"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def search_events(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[Event]:
        """Search event payloads and evidence, using FTS5 when available."""

        if not query.strip():
            raise ValueError("query cannot be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        rows: list[sqlite3.Row]
        if self._fts_available:
            safe_query = query.replace('"', " ")
            safe_query = f'"{safe_query}"'
            sql = (
                "SELECT e.* FROM event_search s "
                "JOIN session_events e ON e.id = s.event_id "
                "WHERE event_search MATCH ?"
            )
            args: list[Any] = [safe_query]
            if session_id is not None:
                sql += " AND e.session_id = ?"
                args.append(session_id)
            sql += " ORDER BY e.occurred_at DESC, e.rowid DESC LIMIT ?"
            args.append(limit)
            try:
                rows = self._connection.execute(sql, tuple(args)).fetchall()
            except sqlite3.OperationalError:
                rows = self._search_events_like(query, session_id, limit)
        else:
            rows = self._search_events_like(query, session_id, limit)
        return [
            Event(
                row["id"],
                row["session_id"],
                row["seq"],
                row["event_type"],
                _decode(row["payload_json"]),
                row["occurred_at"],
                row["content_hash"],
            )
            for row in rows
        ]

    def _search_events_like(
        self,
        query: str,
        session_id: str | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        needle = f"%{query}%"
        sql = (
            "SELECT e.* FROM session_events e "
            "WHERE (e.event_type LIKE ? OR e.payload_json LIKE ? OR EXISTS ("
            "SELECT 1 FROM evidence_spans s WHERE s.event_id = e.id "
            "AND (s.content LIKE ? OR s.artifact_uri LIKE ?)))"
        )
        args: list[Any] = [needle, needle, needle, needle]
        if session_id is not None:
            sql += " AND e.session_id = ?"
            args.append(session_id)
        sql += " ORDER BY e.occurred_at DESC, e.rowid DESC LIMIT ?"
        args.append(limit)
        return self._connection.execute(sql, tuple(args)).fetchall()

    def rebuild_search_index(self) -> bool:
        """Rebuild the optional FTS index from append-only source tables."""

        if not self._fts_available:
            return False
        with self._transaction() as connection:
            connection.execute("DELETE FROM event_search")
            rows = connection.execute(
                "SELECT e.id, e.session_id, e.event_type, e.payload_json, "
                "COALESCE(group_concat(CASE WHEN s.artifact_uri IS NOT NULL "
                "THEN s.artifact_uri || char(10) || s.content ELSE s.content END, char(10)), '') AS evidence_text "
                "FROM session_events e LEFT JOIN evidence_spans s ON s.event_id = e.id "
                "GROUP BY e.id ORDER BY e.rowid"
            ).fetchall()
            connection.executemany(
                "INSERT INTO event_search(event_id, session_id, event_type, payload_text, evidence_text) "
                "VALUES(?, ?, ?, ?, ?)",
                [
                    (
                        row["id"],
                        row["session_id"],
                        row["event_type"],
                        row["payload_json"],
                        row["evidence_text"],
                    )
                    for row in rows
                ],
            )
        return True

    # ------------------------------------------------------------------
    # Semantic recall (vector projection, opt-in enhancement over FTS5)
    # ------------------------------------------------------------------

    def _searchable_text(self, event: Event) -> str:
        """The text an event is embedded/searched by.

        This MUST mirror what the FTS index covers (event_type, payload,
        evidence content, evidence artifact_uri) so the keyword and vector
        recall paths project the *same* text and their results are comparable.
        """

        parts = [event.event_type, _json(event.payload)]
        for evidence in self.evidence_for_event(event.id):
            if evidence["artifact_uri"]:
                parts.append(evidence["artifact_uri"])
            parts.append(evidence["content"])
        return "\n".join(parts)

    def build_embedding_index(
        self,
        embedding_fn: Any,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """(Re)build the vector-index projection from append-only events.

        This is a projection, never the source of truth: it deletes and
        re-embeds the searchable text of every event (optionally scoped to a
        session).  The embedding function is injectable -- a real embedding
        service plugs in behind the same protocol.
        """

        from .embeddings import local_hash_embedding

        embed = embedding_fn or local_hash_embedding
        model_id = getattr(embedding_fn, "model_id", None) or (
            "local_hash" if embedding_fn is None or embedding_fn is local_hash_embedding
            else getattr(embedding_fn, "__name__", "custom")
        )
        # Paginate in chunks so a huge store is fully indexed -- never silently
        # truncated by a hard-coded limit.  Events are fetched newest-first per
        # page; embedding proceeds oldest-first within each page.
        page_size = 5000
        offset = 0
        count = 0
        dimensions = 0
        now = _now()
        with self._transaction() as connection:
            if session_id is None:
                connection.execute("DELETE FROM event_embeddings")
            else:
                connection.execute(
                    "DELETE FROM event_embeddings WHERE event_id IN "
                    "(SELECT id FROM session_events WHERE session_id = ?)",
                    (session_id,),
                )
            while True:
                page = self._connection.execute(
                    "SELECT * FROM session_events "
                    + ("WHERE session_id = ? " if session_id is not None else "")
                    + "ORDER BY rowid LIMIT ? OFFSET ?",
                    ((session_id,) if session_id is not None else ()) + (page_size, offset),
                ).fetchall()
                if not page:
                    break
                for row in page:
                    event = Event(
                        row["id"], row["session_id"], row["seq"], row["event_type"],
                        _decode(row["payload_json"]), row["occurred_at"], row["content_hash"],
                    )
                    vector = embed(self._searchable_text(event))
                    _check_finite_vector(vector, context="embedding index build")
                    dimensions = len(vector)
                    connection.execute(
                        "INSERT INTO event_embeddings(event_id, dimensions, vector_json, model_id, embedded_at) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (event.id, dimensions, _json(vector), model_id, now),
                    )
                    count += 1
                offset += len(page)
                if len(page) < page_size:
                    break
        return {"embedded": count, "dimensions": dimensions, "model_id": model_id}

    def search_events_semantic(
        self,
        query: str,
        embedding_fn: Any,
        *,
        session_id: str | None = None,
        limit: int = 20,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Three-stage semantic recall over the vector projection.

        Stage 1 (recall): cosine similarity against the vector projection, with
        session/scoping and a similarity floor.  Stage 2 (rerank): similarity
        plus recency.  Stage 3 (construct): each result carries the event and
        its short reference id so a caller can drill down to the source.

        The query is embedded with the same injectable function.  This never
        replaces FTS5 keyword search; it is an opt-in semantic complement.
        """

        from .embeddings import cosine_similarity, local_hash_embedding

        if not query.strip():
            raise ValueError("query cannot be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        embed = embedding_fn or local_hash_embedding
        model_id = getattr(embedding_fn, "model_id", None) or (
            "local_hash" if embedding_fn is None or embedding_fn is local_hash_embedding
            else getattr(embedding_fn, "__name__", "custom")
        )

        # Guard BEFORE embedding the query: a vector built by one embedding
        # function is meaningless against an index built by another, and a real
        # embedding service would otherwise send the query text to the vendor
        # (and bill for it) on a call that is guaranteed to be refused.  A
        # heterogeneous index (more than one model) is never queryable.
        index_models = {
            row["model_id"]
            for row in self._connection.execute(
                "SELECT DISTINCT model_id FROM event_embeddings"
            ).fetchall()
        }
        if len(index_models) > 1:
            raise ValueError(
                f"the embedding index is heterogeneous (built with {sorted(index_models)}); "
                "rebuild it with a single embedding function"
            )
        if index_models and model_id not in index_models:
            raise ValueError(
                f"query embedding '{model_id}' does not match the index "
                f"(built with {sorted(index_models)}); rebuild the index with the "
                "same embedding function"
            )
        query_vector = embed(query)
        _check_finite_vector(query_vector, context="semantic query")

        clauses = []
        args: list[Any] = []
        if session_id is not None:
            clauses.append("e.session_id = ?")
            args.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT e.*, v.vector_json FROM event_embeddings v "
            f"JOIN session_events e ON e.id = v.event_id {where}",
            tuple(args),
        ).fetchall()

        scored = []
        for row in rows:
            vector = _decode(row["vector_json"])
            if len(vector) != len(query_vector):
                raise ValueError(
                    "query vector dimension does not match the index; rebuild "
                    "the index with the same embedding function"
                )
            similarity = cosine_similarity(query_vector, vector)
            if similarity < min_similarity:
                continue
            scored.append((similarity, row))
        # Rerank: similarity first, then recency, then a deterministic unique
        # tie-break (event_id) so ordering is meaningful across sessions.
        scored.sort(key=lambda item: (item[0], item[1]["occurred_at"], item[1]["id"]), reverse=True)
        return [
            {
                "event": Event(
                    row["id"],
                    row["session_id"],
                    row["seq"],
                    row["event_type"],
                    _decode(row["payload_json"]),
                    row["occurred_at"],
                    row["content_hash"],
                ),
                "similarity": similarity,
                "ref_id": row["id"][:12],
            }
            for similarity, row in scored[:limit]
        ]

    def search_events_ranked(
        self,
        query: str,
        embedding_fn: Any,
        *,
        session_id: str | None = None,
        limit: int = 20,
        min_similarity: float = 0.0,
        rerank_fn: Any = None,
    ) -> list[dict[str, Any]]:
        """Three-stage retrieval: recall + rerank + construct (docs/memory-model §6).

        Stage 1 (recall) runs :meth:`search_events_semantic`.  Stage 2 (rerank)
        re-orders the candidates by task relevance, source quality, review
        status and freshness via an injectable ``RerankFn`` (default
        :func:`noname_harness.rerank.default_rerank`).  Stage 3 (construct)
        returns each result with its short ref id and the ``rerank_reasons``
        behind its placement, so the ordering is explainable.

        Reranking is a projection: it re-orders and annotates, never alters the
        underlying events/evidence.
        """

        from .rerank import default_rerank

        # Recall (reuses the semantic recall with its guards).
        recalled = self.search_events_semantic(
            query,
            embedding_fn,
            session_id=session_id,
            limit=limit,
            min_similarity=min_similarity,
        )
        # Promoted event ids (canon/task state) feed the review-status signal.
        # Scoped to the queried session when one is given, so an unrelated
        # session's promotion cannot boost these candidates.
        promoted = frozenset(
            event_id
            for item in self.active_state("high") + self.active_state("mid")
            for event_id in item["source_event_ids"]
            if session_id is None or self._event_session(event_id) == session_id
        )
        # Evidence counts for the source-quality signal, in ONE aggregate query
        # (not N+1 full-row fetches).
        event_ids = [hit["event"].id for hit in recalled]
        evidence_counts = self._evidence_counts(event_ids)
        for hit in recalled:
            hit["evidence"] = [None] * evidence_counts.get(hit["event"].id, 0)
            # Review-status is marked on the candidate itself, so ANY reranker
            # (default, partial-wrapped, or custom) sees it without needing a
            # special kwarg -- injectability never forks the signal.
            hit["promoted"] = hit["event"].id in promoted
        if rerank_fn is None:
            rerank_fn = default_rerank
        ranked = rerank_fn(query, recalled)
        return [
            {
                "event": item.event,
                "similarity": item.similarity,
                "ref_id": item.ref_id,
                "score": item.score,
                "rerank_reasons": list(item.rerank_reasons),
            }
            for item in ranked[:limit]
        ]

    def state_history(self, layer: str, logical_key: str) -> list[dict[str, Any]]:
        """Return the full revision chain for a durable-state key, oldest first.

        Follows the supersedes chain backwards from the current head to the
        original revision, so a State Diff can show "what changed between v11
        and v12" -- which revision added it, which retired it, and what
        superseded what.  Pure projection: reads only.
        """

        if layer not in VALID_LAYERS:
            raise ValueError(f"invalid layer: {layer}")
        rows = self._connection.execute(
            "SELECT * FROM state_revisions WHERE layer = ? AND logical_key = ?",
            (layer, logical_key),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        if not rows:
            return []
        # Walk the supersedes chain from the true head (the revision nothing in
        # this key supersedes), NOT by wall-clock created_at -- a clock that
        # moves backwards between reviews would otherwise invert the chain and
        # show the superseded parent as the head.  Append-only triggers plus the
        # one-child-per-parent index make exactly one head per key.
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        heads = [row for row in rows if row["id"] not in superseded]
        head = heads[0] if heads else rows[-1]  # pragma: no cover - defensive
        chain: list[Any] = []
        current = head
        seen = {head["id"]}
        broken = False
        while True:
            chain.append(current)
            parent_id = current["supersedes_id"]
            if parent_id is None:
                break
            parent = by_id.get(parent_id)
            if parent is None or parent["id"] in seen:
                # A missing or cycling parent: mark the lineage broken rather
                # than silently truncating the audit trail.
                broken = True
                break
            seen.add(parent["id"])
            current = parent
        chain.reverse()
        result = [
            {
                "id": row["id"],
                "layer": row["layer"],
                "logical_key": row["logical_key"],
                "kind": row["kind"],
                "content": _decode(row["content_json"]),
                "status": row["status"],
                "source_event_ids": _decode(row["source_event_ids_json"]),
                "origin_proposal_id": row["origin_proposal_id"],
                "supersedes_id": row["supersedes_id"],
                "approved_by": row["approved_by"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "created_at": row["created_at"],
                "broken_lineage": broken,
            }
            for row in chain
        ]
        return result

    def _evidence_counts(self, event_ids: list[str]) -> dict[str, int]:
        """Evidence span counts per event id, in a single aggregate query."""

        if not event_ids:
            return {}
        placeholders = ",".join("?" for _ in event_ids)
        rows = self._connection.execute(
            f"SELECT event_id, COUNT(*) AS n FROM evidence_spans "
            f"WHERE event_id IN ({placeholders}) GROUP BY event_id",
            tuple(event_ids),
        ).fetchall()
        return {row["event_id"]: row["n"] for row in rows}

    def _event_session(self, event_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT session_id FROM session_events WHERE id = ?", (event_id,)
        ).fetchone()
        return row["session_id"] if row else None

    def check_event_ids(self, source_event_ids: Sequence[str]) -> None:
        """Public contract: assert every cited source event exists.

        Services layered on the store (for example the taste service) call this
        instead of reaching into private internals.
        """

        self._check_event_ids(source_event_ids)

    def _check_event_ids(self, source_event_ids: Sequence[str]) -> None:
        if not source_event_ids:
            raise ValueError("a durable proposal must cite at least one source event")
        placeholders = ",".join("?" for _ in source_event_ids)
        count = self._connection.execute(
            f"SELECT COUNT(*) AS count FROM session_events WHERE id IN ({placeholders})",
            tuple(source_event_ids),
        ).fetchone()["count"]
        if count != len(set(source_event_ids)):
            raise KeyError("one or more source event ids do not exist")

    def proposal_exists_for_event(self, event_id: str, logical_key: str | None = None) -> bool:
        """Return whether a proposal already cites an event.

        Source ids are stored as a JSON array so this small prototype checks
        them in Python.  A later migration can add a normalized join table if
        proposal volume makes that worthwhile.
        """

        rows = self._connection.execute(
            "SELECT logical_key, source_event_ids_json FROM state_proposals"
        ).fetchall()
        for row in rows:
            if logical_key is not None and row["logical_key"] != logical_key:
                continue
            if event_id in _decode(row["source_event_ids_json"]):
                return True
        return False

    def _active_revisions(
        self,
        layer: str | None = None,
        as_of: str | None = None,
        *,
        apply_validity: bool = True,
    ) -> list[dict[str, Any]]:
        """Project current durable state, honouring bitemporal validity.

        ``as_of`` defaults to now.  When ``apply_validity`` is true a revision
        is only projected when it is ``active`` *and* valid at ``as_of``:
        ``valid_from <= as_of`` and (``valid_to`` is NULL or ``as_of <=
        valid_to``).  This is what makes validity behavioural rather than
        write-only: an expired constraint drops out of the projection and
        therefore out of any context package.

        Internal callers that resolve supersede chains or detect conflicts set
        ``apply_validity=False`` so a not-yet-valid or already-expired head is
        still found and correctly superseded rather than silently orphaned.
        """

        if layer is not None and layer not in VALID_LAYERS:
            raise ValueError(f"invalid layer: {layer}")
        moment = (
            _parse_instant(as_of, "as_of")
            if as_of is not None
            else (_now() if apply_validity else None)
        )
        query = "SELECT * FROM state_revisions"
        args: tuple[Any, ...] = ()
        if layer is not None:
            query += " WHERE layer = ?"
            args = (layer,)
        rows = self._connection.execute(query, args).fetchall()
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            if row["id"] in superseded:
                continue
            key = (row["layer"], row["logical_key"])
            previous = latest.get(key)
            if previous is None or (row["created_at"], row["id"]) > (
                previous["created_at"],
                previous["id"],
            ):
                latest[key] = row
        return [
            {
                "id": row["id"],
                "layer": row["layer"],
                "logical_key": row["logical_key"],
                "kind": row["kind"],
                "content": _decode(row["content_json"]),
                "status": row["status"],
                "source_event_ids": _decode(row["source_event_ids_json"]),
                "origin_proposal_id": row["origin_proposal_id"],
                "supersedes_id": row["supersedes_id"],
                "approved_by": row["approved_by"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "created_at": row["created_at"],
            }
            for row in latest.values()
            if row["status"] == "active"
            and (
                not apply_validity
                or (
                    (row["valid_from"] is None or row["valid_from"] <= moment)
                    and (row["valid_to"] is None or moment <= row["valid_to"])
                )
            )
        ]

    def _conflicts(self, layer: str, logical_key: str, content: Any) -> list[str]:
        return [
            item["id"]
            for item in self._active_revisions(layer, apply_validity=False)
            if item["logical_key"] == logical_key and item["content"] != content
        ]

    def create_proposal(
        self,
        layer: str,
        logical_key: str,
        content: Any,
        source_event_ids: Sequence[str],
        *,
        kind: str = "state",
        proposed_by: str = "curator",
        confidence: float | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if layer not in VALID_LAYERS:
            raise ValueError("durable proposals may only use high or mid layer")
        if not isinstance(logical_key, str):
            raise TypeError(
                f"logical_key must be a str, got {type(logical_key).__name__}"
            )
        if not logical_key.strip():
            raise ValueError("logical_key cannot be empty")
        if confidence is not None:
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
                or not math.isfinite(confidence)
            ):
                raise ValueError("confidence must be a finite number between 0 and 1")
        self._check_event_ids(source_event_ids)
        proposal_id = _id("prp")
        created_at = _now()
        conflict_ids = self._conflicts(layer, logical_key, content)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO state_proposals "
                "(id, layer, kind, logical_key, content_json, source_event_ids_json, "
                "conflict_with_ids_json, proposed_by, confidence, proposal_reason, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id,
                    layer,
                    kind,
                    logical_key,
                    _json(content),
                    _json(list(dict.fromkeys(source_event_ids))),
                    _json(conflict_ids),
                    proposed_by,
                    confidence,
                    reason,
                    created_at,
                ),
            )
            source_session = connection.execute(
                "SELECT session_id FROM session_events WHERE id = ?",
                (source_event_ids[0],),
            ).fetchone()["session_id"]
            self._insert_event(
                connection,
                source_session,
                "memory.proposed",
                {
                    "proposal_id": proposal_id,
                    "layer": layer,
                    "logical_key": logical_key,
                    "source_event_ids": list(dict.fromkeys(source_event_ids)),
                    "conflict_with_ids": conflict_ids,
                    "reason": reason,
                },
                occurred_at=created_at,
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM state_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown proposal: {proposal_id}")
        reviews = self._connection.execute(
            "SELECT * FROM proposal_reviews WHERE proposal_id = ? ORDER BY reviewed_at, rowid",
            (proposal_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "layer": row["layer"],
            "kind": row["kind"],
            "logical_key": row["logical_key"],
            "content": _decode(row["content_json"]),
            "source_event_ids": _decode(row["source_event_ids_json"]),
            "conflict_with_ids": _decode(row["conflict_with_ids_json"]),
            "proposed_by": row["proposed_by"],
            "confidence": row["confidence"],
            "reason": row["proposal_reason"],
            "created_at": row["created_at"],
            "reviews": [
                {
                    "id": review["id"],
                    "action": review["action"],
                    "reviewer_id": review["reviewer_id"],
                    "edited_content": _decode(review["edited_content_json"])
                    if review["edited_content_json"] is not None
                    else None,
                    "reason": review["reason"],
                    "reviewed_at": review["reviewed_at"],
                }
                for review in reviews
            ],
        }

    def list_proposals(self, pending_only: bool = False) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT id FROM state_proposals ORDER BY created_at, rowid"
        ).fetchall()
        proposals = [self.get_proposal(row["id"]) for row in rows]
        if not pending_only:
            return proposals
        return [item for item in proposals if not item["reviews"] or item["reviews"][-1]["action"] == "defer"]

    def review_inbox(self) -> dict[str, Any]:
        """Aggregate everything waiting for a human decision into one inbox.

        This is the projection behind the ledger's review inbox: a small,
        high-value to-do list rather than a raw event dump.  It joins pending
        durable-state proposals (canon and task state) with pending adopted
        taste candidates, and annotates each entry with its impact scope,
        source events and conflicts so a reviewer can judge, not just click.

        The inbox is derived from append-only tables and can be rebuilt at
        any time; it never stores facts of its own.
        """

        from .taste import TasteService

        canon_items = []
        for proposal in self.list_proposals(pending_only=True):
            impact = "project canon (long-term)" if proposal["layer"] == "high" else "current task state"
            canon_items.append(
                {
                    "kind": "state_proposal",
                    "id": proposal["id"],
                    "layer": proposal["layer"],
                    "logical_key": proposal["logical_key"],
                    "summary": proposal["content"],
                    "impact": impact,
                    "proposed_by": proposal["proposed_by"],
                    "confidence": proposal["confidence"],
                    "reason": proposal["reason"],
                    "source_event_ids": proposal["source_event_ids"],
                    "conflict_with_ids": proposal["conflict_with_ids"],
                    "created_at": proposal["created_at"],
                }
            )

        from .taste_cards import TasteCardService

        card_items = []
        for card in TasteCardService(self).by_status("candidate"):
            card_items.append(
                {
                    "kind": "taste_card_candidate",
                    "id": card["id"],
                    "title": card["title"],
                    "summary": card["attitude"],
                    "impact": f"taste card ({card['scope']} scope, attitude only)",
                    "track": card["track"],
                    "taste_ids": card["taste_ids"],
                    "stale": card["stale"],
                    "created_at": card["recorded_at"],
                }
            )

        taste_items = []
        for record in TasteService(self).pending():
            taste_items.append(
                {
                    "kind": "taste_candidate",
                    "id": record["id"],
                    "track": record["track"],
                    "scope": record["scope"],
                    "summary": record["content"],
                    "impact": f"taste ({record['scope']} scope, attitude only)",
                    "proposed_by": record["actor_id"],
                    "reason": record["reason"],
                    "source_event_ids": record["source_event_ids"],
                    "created_at": record["recorded_at"],
                }
            )

        return {
            "canon_pending": [item for item in canon_items if item["layer"] == "high"],
            "task_pending": [item for item in canon_items if item["layer"] == "mid"],
            "taste_pending": taste_items,
            "card_pending": card_items,
            "counts": {
                "canon": sum(1 for item in canon_items if item["layer"] == "high"),
                "task": sum(1 for item in canon_items if item["layer"] == "mid"),
                "taste": len(taste_items),
                "card": len(card_items),
                "total": len(canon_items) + len(taste_items) + len(card_items),
            },
        }

    def _latest_revision(self, layer: str, logical_key: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self._active_revisions(layer, apply_validity=False)
                if item["logical_key"] == logical_key
            ),
            None,
        )

    def review_proposal(
        self,
        proposal_id: str,
        action: str,
        reviewer_id: str,
        *,
        edited_content: Any | None = None,
        reason: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> dict[str, Any]:
        if action not in VALID_REVIEW_ACTIONS:
            raise ValueError(f"invalid review action: {action}")
        if not isinstance(reviewer_id, str):
            raise TypeError(
                f"reviewer_id must be a str, got {type(reviewer_id).__name__}"
            )
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        proposal = self.get_proposal(proposal_id)
        if action == "edit" and edited_content is None:
            raise ValueError("edited_content is required for edit")
        if valid_from is not None:
            valid_from = _parse_instant(valid_from, "valid_from")
        if valid_to is not None:
            valid_to = _parse_instant(valid_to, "valid_to")
        if valid_from is not None and valid_to is not None and valid_to < valid_from:
            raise ValueError("valid_to cannot precede valid_from")
        review_id = _id("rev")
        reviewed_at = _now()
        content = edited_content if action == "edit" else proposal["content"]
        revision_id: str | None = None
        supersedes_id: str | None = None
        revision_status: str | None = None
        with self._transaction() as connection:
            # Serialise concurrent reviewers under the write lock.  BEGIN
            # IMMEDIATE takes the write lock before this re-read, so a reviewer
            # always sees the latest committed decisions.  Repeating the *same*
            # terminal action (accept/reject) on an already-decided proposal is
            # rejected as a no-op conflict rather than silently re-written; the
            # legitimate lifecycle transition accept -> retire stays allowed.
            prior_actions = {
                row["action"]
                for row in connection.execute(
                    "SELECT action FROM proposal_reviews WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchall()
            }
            if action in {"accept", "reject"} and prior_actions & {"accept", "reject"}:
                raise ValueError(
                    f"proposal {proposal_id} already has a decision "
                    f"({sorted(prior_actions)}); refusing to overwrite it with '{action}'"
                )
            connection.execute(
                "INSERT INTO proposal_reviews "
                "(id, proposal_id, action, reviewer_id, edited_content_json, reason, reviewed_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    proposal_id,
                    action,
                    reviewer_id,
                    _json(edited_content) if action == "edit" else None,
                    reason,
                    reviewed_at,
                ),
            )
            if action in {"accept", "edit", "retire"}:
                previous = self._latest_revision(proposal["layer"], proposal["logical_key"])
                status = "retired" if action == "retire" else "active"
                if action == "retire":
                    # Retiring invalidates a fact: close its valid interval at
                    # the review moment (unless the reviewer said when it
                    # stopped being true), and inherit the predecessor's
                    # valid_from so the retired revision still records when the
                    # fact had been true.  An explicit --valid-from wins.
                    if valid_to is None:
                        valid_to = reviewed_at
                    if valid_from is None and previous is not None:
                        valid_from = previous["valid_from"]
                revision_id = _id("stt")
                supersedes_id = previous["id"] if previous else None
                revision_status = status
                connection.execute(
                    "INSERT INTO state_revisions "
                    "(id, layer, logical_key, kind, content_json, status, source_event_ids_json, "
                    "origin_proposal_id, supersedes_id, approved_by, valid_from, valid_to, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        proposal["layer"],
                        proposal["logical_key"],
                        proposal["kind"],
                        _json(content),
                        status,
                        _json(proposal["source_event_ids"]),
                        proposal_id,
                        supersedes_id,
                        reviewer_id,
                        valid_from,
                        valid_to,
                        reviewed_at,
                    ),
                )
            source_session = connection.execute(
                "SELECT session_id FROM session_events WHERE id = ?",
                (proposal["source_event_ids"][0],),
            ).fetchone()["session_id"]
            self._insert_event(
                connection,
                source_session,
                "memory.reviewed",
                {
                    "proposal_id": proposal_id,
                    "action": action,
                    "reviewer_id": reviewer_id,
                    "reason": reason,
                    "conflict_with_ids": proposal["conflict_with_ids"],
                    "revision_id": revision_id,
                    "supersedes_id": supersedes_id,
                    "revision_status": revision_status,
                },
                occurred_at=reviewed_at,
            )
        return self.get_proposal(proposal_id)

    # A declared context window below this many tokens is treated as
    # "tight": the projection assumes roughly a thousand tokens per carried
    # work event (a diff or test output is rarely smaller), so a window that
    # can hold only a handful of events tightens the low window further.  The
    # ratio is a coarse heuristic, named here so it can be revisited, not a
    # precise estimator.
    _TIGHT_CONTEXT_WINDOW_TOKENS = 16_000

    @staticmethod
    def _low_limit_for_model(model: "ModelProfile", requested: int) -> int:
        """Tighten the low evidence window for a constrained target model.

        Projection only ever *tightens* the caller's requested window -- a
        smaller budget or a tight context window narrows it, but nothing ever
        widens it beyond what the caller asked for.  A floor keeps even the
        tightest projection useful (a few recent events plus the snapshot),
        unless the caller explicitly requested fewer than the floor.
        """

        # Respect an explicit small request: the floor only protects the
        # default window, never overrides a deliberate smaller choice.
        floor = min(3, requested)
        limit = requested
        if model.budget == "low":
            limit = max(floor, limit // 4)
        # "medium" and "high" keep the requested window: a larger budget does
        # not widen it beyond the caller's ask.
        window = model.capability.context_window
        if window is not None and window < HarnessStore._TIGHT_CONTEXT_WINDOW_TOKENS:
            limit = max(floor, limit // 2)
        return limit

    def active_state(
        self, layer: str | None = None, as_of: str | None = None
    ) -> list[dict[str, Any]]:
        return sorted(
            self._active_revisions(layer, as_of=as_of),
            key=lambda item: (item["layer"], item["logical_key"]),
        )

    def assemble_context_package(
        self,
        task: str,
        *,
        session_id: str | None = None,
        low_limit: int = 20,
        model: "ModelProfile | None" = None,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        if not task.strip():
            raise ValueError("task cannot be empty")
        if low_limit < 1:
            raise ValueError("low_limit must be positive")
        # Projecting for a specific model never changes the facts -- reviewed
        # canon, task state, taste and provenance are identical for every
        # target.  The model's capability only biases the *low evidence
        # window*: a smaller budget or context window tightens how much recent
        # work evidence is carried, nothing else.
        if model is not None:
            low_limit = self._low_limit_for_model(model, low_limit)
        # Resolve the role chain for this task type.  It is advisory in the
        # prototype -- recorded for audit, surfaced in assembly metadata -- but
        # resolving it here keeps routing explainable from the start.
        recipe_description = None
        if task_type is not None:
            from .recipes import resolve_recipe

            recipe_description = resolve_recipe(task_type).describe()
        project = self.project()
        snapshot_event: Event | None = None
        if session_id is not None:
            # A handoff should not depend on the user remembering to ask for
            # a git-status update.  This is read-only and becomes low-layer
            # evidence for the package being assembled.  It is kept as an
            # explicit handoff marker below so it cannot displace a recent
            # failure or artifact change from the low-layer work window.
            snapshot_event = self.append_workspace_snapshot(session_id)
        active_high = self.active_state("high")
        mid_state = self.active_state("mid")
        pending = self.list_proposals(pending_only=True)
        promoted_event_ids = {
            event_id
            for item in active_high + mid_state
            for event_id in item["source_event_ids"]
        }
        # Review/audit events remain visible in the ledger, but the handoff's
        # low layer is meant to describe the work itself rather than repeat
        # projection bookkeeping.
        # ``low_limit`` applies to work evidence.  The current workspace
        # snapshot is an additional, clearly typed marker: it answers “what
        # did the workspace look like at handoff?” without crowding out the
        # test/error/diff events that explain why the handoff exists.
        # Fetch a little beyond the requested window because source events
        # already promoted to high/mid state are intentionally omitted.
        # Two distinct windows are derived from the same work history:
        #
        # * the *display* window (``recent_events``) is what the target model's
        #   budget trims -- it is the only model-dependent part of the package;
        # * the *inference* window (``inference_events``) is model-independent,
        #   so advisory next steps never change just because the target model
        #   has a smaller context budget.
        #
        # The inference window is deliberately generous and independent of
        # ``low_limit``: advisory reasoning should see the same recent history
        # regardless of which model the package is projected for.
        inference_window = 100
        work_limit = low_limit + len(promoted_event_ids)
        fetch_limit = max(work_limit, low_limit, inference_window + len(promoted_event_ids))
        inference_events: list[Event] = []
        recent_events = []
        for event in self.list_work_events(
            session_id=session_id,
            limit=fetch_limit,
            include_workspace_snapshots=False,
        ):
            if event.id in promoted_event_ids:
                continue
            if len(inference_events) < inference_window:
                inference_events.append(event)
            if len(recent_events) < low_limit:
                recent_events.append(event)
        if snapshot_event is not None:
            recent_events.append(snapshot_event)
            inference_events.append(snapshot_event)
        def _low_item(event: Event) -> dict[str, Any]:
            return {
                "event_id": event.id,
                "session_id": event.session_id,
                "seq": event.seq,
                "event_type": event.event_type,
                "payload": event.payload,
                "occurred_at": event.occurred_at,
                "content_hash": event.content_hash,
                "evidence": self.evidence_for_event(event.id),
            }

        low = [_low_item(event) for event in recent_events]
        # Next steps are advisory, but they must not depend on the target
        # model's budget: inference runs over the model-independent window.
        next_steps = infer_next_steps(
            [_low_item(event) for event in inference_events], mid_state
        )
        # Taste is projected independently from facts and merged only here, at
        # the assembly layer, so an attitude is never mistaken for a fact.
        # Active user-scope and project-scope tastes are both eligible; the
        # consumer sees them in an explicit, soft-influence section.
        from .taste import TasteService

        taste_projection = TasteService(self).active()
        # Provenance is split into a model-independent state part and a
        # model-dependent evidence part, so the invariant "reviewed state is
        # traceable identically for every target model" is actually true.
        #
        # State provenance covers reviewed canon, task state, pending proposals
        # and taste -- it is identical for every projection of the same ledger.
        state_event_ids = list(dict.fromkeys(
            [event_id for item in active_high + mid_state for event_id in item["source_event_ids"]]
            + [event_id for item in pending for event_id in item["source_event_ids"]]
            # An adopted taste cites the "model moment" that justified it; those
            # source events must be part of the package provenance too, or a
            # taste could point at evidence the package cannot account for.
            + [event_id for item in taste_projection for event_id in item["source_event_ids"]]
        ))
        # Evidence provenance additionally covers the low events this
        # projection chose to carry; it legitimately varies with the target
        # model's budget, and is kept separate so that variation is explicit.
        source_event_ids = list(dict.fromkeys(
            [item["event_id"] for item in low] + state_event_ids
        ))
        evidence_ids = list(dict.fromkeys(
            evidence["id"]
            for event_id in source_event_ids
            for evidence in self.evidence_for_event(event_id)
        ))
        package_id = _id("pkg")
        created_at = _now()
        package: dict[str, Any] = {
            "schema_version": 1,
            "package_id": package_id,
            "created_at": created_at,
            "project": {
                "name": project["name"],
                "workspace_root": project["workspace_root"],
            },
            "task": task,
            "assembly": {
                "projection_version": 1,
                "model_independent": True,
                "low_event_limit": low_limit,
                "workspace_snapshot_included": snapshot_event is not None,
                # The package is always model-independent in its facts; this
                # only records which target's budget shaped the evidence window,
                # so a handoff remains auditable.
                "projected_for_model": (
                    {
                        "id": model.id,
                        "budget": model.budget,
                        "context_window": model.capability.context_window,
                    }
                    if model is not None
                    else None
                ),
                "recipe": recipe_description,
            },
            "layers": {
                "high": active_high,
                "mid": mid_state,
                "low": low,
            },
            "next_step_candidates": next_steps,
            "pending_review": pending,
            # Taste lives in its own section, clearly marked as a soft
            # influence on attitude (ordering, trade-offs, expression), never
            # as evidence for facts or a reason to lower verification.
            "preference": {
                "influence": "soft",
                "note": (
                    "Taste shapes attitude only: option ordering, trade-offs, "
                    "expression and exploration direction. It must not rewrite "
                    "facts, lower verification standards or override the task."
                ),
                "tracks": {
                    "authored": [
                        item for item in taste_projection if item["track"] == "authored"
                    ],
                    "adopted": [
                        item for item in taste_projection if item["track"] == "adopted"
                    ],
                },
            },
            "guardrails": {
                "source_of_truth": "append-only events and evidence",
                "durable_state_requires_review": True,
                "workspace_root": project["workspace_root"],
                "workspace_policy": "do not modify files outside the configured workspace root",
            },
            "provenance": {
                "event_ids": source_event_ids,
                "evidence_ids": evidence_ids,
                "state_revision_ids": [
                    item["id"]
                    for item in active_high + mid_state
                ],
                "taste_ids": [item["id"] for item in taste_projection],
                # Model-independent: identical for every projection of the same
                # ledger, regardless of the target model's evidence budget.
                "state_event_ids": state_event_ids,
            },
        }
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO context_packages(id, task, session_id, package_json, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (package_id, task, session_id, _json(package), created_at),
            )
            self._insert_event(
                connection,
                session_id or "system",
                "context.assembled",
                {
                    "package_id": package_id,
                    "task": task,
                    "task_type": task_type,
                    "recipe_id": recipe_description["id"] if recipe_description else None,
                    "model_id": model.id if model is not None else None,
                    "low_event_limit": low_limit,
                    "source_event_ids": source_event_ids,
                },
                occurred_at=created_at,
            )
        return package

    def verify_integrity(self) -> dict[str, Any]:
        """Recompute hashes for stored events and evidence."""

        bad_events: list[str] = []
        for row in self._connection.execute("SELECT * FROM session_events ORDER BY rowid"):
            expected = _hash(
                {"event_type": row["event_type"], "payload": _decode(row["payload_json"])}
            )
            if expected != row["content_hash"]:
                bad_events.append(row["id"])

        bad_evidence: list[str] = []
        for row in self._connection.execute("SELECT * FROM evidence_spans ORDER BY rowid"):
            expected = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
            if expected != row["content_hash"]:
                bad_evidence.append(row["id"])

        return {
            "ok": not bad_events and not bad_evidence,
            "bad_event_ids": bad_events,
            "bad_evidence_ids": bad_evidence,
        }

    def write_bytes_nofollow(
        self, path: str | Path, content: bytes, *, media_suffix: str | None = None
    ) -> Path:
        """Write raw bytes inside the workspace without following a final symlink.

        Binary-safe sibling of :meth:`write_text_nofollow`, for generated image
        bytes (PNG/JPEG) that are not UTF-8 text.  ``media_suffix`` (e.g.
        ".png") replaces the path's suffix so a binary payload is never saved
        under a misleading ".svg" name.
        """

        destination = self.validate_output_path(path)
        if media_suffix is not None:
            destination = destination.with_suffix(media_suffix)
            destination = self.validate_output_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        else:
            raise WorkspaceBoundaryError(
                "O_NOFOLLOW is unavailable; refusing to write without symlink protection"
            )
        try:
            fd = os.open(str(destination), flags, 0o644)
        except OSError as exc:
            raise WorkspaceBoundaryError(
                f"refusing to write through a symlink or unreadable path: {destination}"
            ) from exc
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        return destination

    def get_context_package(self, package_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT package_json FROM context_packages WHERE id = ?", (package_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown context package: {package_id}")
        return _decode(row["package_json"])

    def write_context_package(
        self,
        path: str | Path,
        package: dict[str, Any],
        *,
        overwrite: bool = False,
        rendered: str | None = None,
    ) -> Path:
        """Write a context package to a workspace file.

        ``rendered`` lets the caller supply the exact text (for example the
        Markdown view); otherwise the canonical JSON form is written.  The
        output path is always validated against the workspace boundary and the
        database sidecar guard.
        """

        destination = self.validate_output_path(path)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite existing file: {destination}")
        text = rendered if rendered is not None else json.dumps(package, ensure_ascii=False, indent=2) + "\n"
        return self.write_text_nofollow(destination, text)
