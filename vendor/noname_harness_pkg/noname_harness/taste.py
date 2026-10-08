"""Taste layer service: authored and adopted tracks, never a fact.

Taste is a first-class citizen in NoName, but it is strictly separated from
memory/facts.  It lives in its own tables, is reviewed through its own
actions, and only ever appears in a context package inside an explicit
``preference`` section marked as a soft influence.  It must never be
presented as evidence for a factual claim.

Two source tracks are honoured:

- **authored**: attitudes the user wrote, confirmed or maintains directly.
  Writing one is itself the act of confirmation, so it becomes active
  immediately, but it is still versioned and traceable.
- **adopted**: a tendency the model expressed that the user explicitly chose
  to keep.  It always starts as a ``candidate`` and only becomes ``active``
  after an explicit ``adopt`` review, so an adopted taste can never pretend
  to be the user's own words.

All taste rows are append-only.  Correction writes a new row that supersedes
the old one; nothing is hard-deleted.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .store import (
    VALID_TASTE_ACTIONS,
    VALID_TASTE_SCOPES,
    VALID_TASTE_STATUSES,
    VALID_TASTE_TRACKS,
    HarnessStore,
    _decode,
    _id,
    _json,
    _now,
)


class TasteService:
    """Manage the taste layer without ever touching fact projections."""

    def __init__(self, store: HarnessStore):
        self.store = store

    # ------------------------------------------------------------------
    # authoring / proposing
    # ------------------------------------------------------------------
    def record_authored(
        self,
        content: Any,
        *,
        scope: str = "user",
        source_event_ids: Sequence[str] | None = None,
        actor_id: str = "user",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record an authored taste; writing it is the act of confirmation.

        Authored taste carries the highest authority, so it is created
        ``active`` directly.  Source events are optional for authored taste
        (the user *is* the source), but providing them keeps provenance
        complete when the attitude was first expressed in a session.
        """

        self._validate(scope=scope, track="authored")
        # Reject every falsy JSON value (None, "", 0, 0.0, False, {}, []) -- an
        # "authored" attitude must be a substantive example or judgement, never
        # an empty or degenerate value that would render as a meaningless
        # preference.  Non-JSON primitives that are truthy (e.g. a bare number)
        # are still better expressed as a structured judgement, but are stored.
        if not content:
            raise ValueError("authored taste content cannot be empty")
        if source_event_ids:
            self.store.check_event_ids(source_event_ids)
        return self._insert_taste(
            track="authored",
            scope=scope,
            content=content,
            status="active",
            source_event_ids=list(source_event_ids or []),
            origin="authored",
            actor_id=actor_id,
            reason=reason,
        )

    def propose_adopted(
        self,
        content: Any,
        *,
        scope: str = "user",
        source_event_ids: Sequence[str],
        proposed_by: str = "model",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Propose an adopted taste candidate from a model-observed moment.

        Adopted taste must cite at least one source event (the answer, work
        or emergence that caught the user's eye) and always enters as a
        ``candidate``.  It only becomes active after an explicit ``adopt``.
        """

        self._validate(scope=scope, track="adopted")
        if not content:
            raise ValueError("adopted taste content cannot be empty")
        if not source_event_ids:
            raise ValueError("adopted taste must cite at least one source event")
        self.store.check_event_ids(source_event_ids)
        return self._insert_taste(
            track="adopted",
            scope=scope,
            content=content,
            status="candidate",
            source_event_ids=list(source_event_ids),
            origin="adopted",
            actor_id=proposed_by,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # review
    # ------------------------------------------------------------------
    def review(
        self,
        taste_id: str,
        action: str,
        reviewer_id: str,
        *,
        edited_content: Any | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a review action to a taste record, writing a new version.

        Actions:
        - ``adopt``: candidate -> active (adopted track only);
        - ``edit``: any status -> active with new content (new version);
        - ``pause`` / ``resume`` / ``retire``: lifecycle transitions.
        """

        if action not in VALID_TASTE_ACTIONS:
            raise ValueError(f"invalid taste action: {action}")
        if not isinstance(reviewer_id, str):
            raise TypeError(f"reviewer_id must be str, got {type(reviewer_id).__name__}")
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        row = self._get_row(taste_id)

        # Reviews only ever apply to the current head of a lineage.  Acting on
        # a superseded record would fork the chain, so refuse it up front.
        if self._has_child(taste_id):
            raise ValueError(
                f"taste record {taste_id} has been superseded; review the current head"
            )

        # A strict lifecycle machine.  This is the enforcement point for the
        # invariant that an adopted candidate becomes active *only* through an
        # explicit ``adopt`` -- ``edit`` can never silently activate one, and
        # ``retired`` is terminal.
        transitions: dict[str, dict[str, str]] = {
            "candidate": {"adopt": "active", "retire": "retired"},
            "active": {"edit": "active", "pause": "paused", "retire": "retired"},
            "paused": {"edit": "active", "resume": "active", "retire": "retired"},
            "retired": {},
        }
        allowed = transitions.get(row["status"], {})
        if action not in allowed:
            raise ValueError(
                f"cannot '{action}' a taste in status '{row['status']}'"
            )
        if action == "adopt" and row["track"] != "adopted":
            raise ValueError("only adopted taste can be adopted")
        if action == "edit" and edited_content is None:
            raise ValueError("edited_content is required for edit")

        new_content = edited_content if action == "edit" else _decode(row["content_json"])
        new_status = allowed[action]

        review_id = _id("trv")
        reviewed_at = _now()
        with self.store.transaction() as connection:
            # Re-check under the write lock.  The pre-transaction validation is
            # a fast path for clear errors, but two concurrent reviewers could
            # both pass it; BEGIN IMMEDIATE serialises them here, and the loser
            # must see that the head moved (a child now exists) and abort
            # rather than fork the lineage.
            still_head = connection.execute(
                "SELECT 1 FROM taste_records WHERE supersedes_id = ? LIMIT 1",
                (taste_id,),
            ).fetchone()
            if still_head is not None:
                raise ValueError(
                    f"taste record {taste_id} was superseded concurrently; "
                    "review the current head"
                )
            current = connection.execute(
                "SELECT status FROM taste_records WHERE id = ?", (taste_id,)
            ).fetchone()
            if current is None or current["status"] != row["status"]:
                raise ValueError(
                    f"taste record {taste_id} changed status concurrently; re-read and retry"
                )
            connection.execute(
                "INSERT INTO taste_reviews "
                "(id, taste_id, action, reviewer_id, edited_content_json, reason, reviewed_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    taste_id,
                    action,
                    reviewer_id,
                    _json(edited_content) if action == "edit" else None,
                    reason,
                    reviewed_at,
                ),
            )
            # A review produces a new immutable record that supersedes the old
            # one; the previous row stays in place for provenance.
            new_id = _id("tst")
            connection.execute(
                "INSERT INTO taste_records "
                "(id, track, scope, content_json, status, source_event_ids_json, "
                "supersedes_id, origin, actor_id, reason, recorded_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    row["track"],
                    row["scope"],
                    _json(new_content),
                    new_status,
                    row["source_event_ids_json"],
                    taste_id,
                    row["origin"],
                    reviewer_id,
                    reason,
                    reviewed_at,
                ),
            )
            # Keep the ledger honest: every taste transition is also an event.
            source_session = self._source_session(connection, row)
            self.store.record_event(
                connection,
                source_session,
                "taste.reviewed",
                {
                    "taste_id": taste_id,
                    "new_taste_id": new_id,
                    "track": row["track"],
                    "action": action,
                    "reviewer_id": reviewer_id,
                    "new_status": new_status,
                    "reason": reason,
                },
                occurred_at=reviewed_at,
            )
        return self.get(new_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get(self, taste_id: str) -> dict[str, Any]:
        row = self._get_row(taste_id)
        # Reviews are written against the version they acted on, so the full
        # decision history of a taste lives across its supersedes chain.  Walk
        # it so a reviewer sees the lineage's whole audit trail, not just the
        # latest version's single review.
        lineage_ids = self._lineage_ids(row)
        placeholders = ",".join("?" for _ in lineage_ids)
        reviews = self.store.query(
            f"SELECT * FROM taste_reviews WHERE taste_id IN ({placeholders}) "
            "ORDER BY reviewed_at, rowid",
            tuple(lineage_ids),
        )
        return {
            "id": row["id"],
            "track": row["track"],
            "scope": row["scope"],
            "content": _decode(row["content_json"]),
            "status": row["status"],
            "source_event_ids": _decode(row["source_event_ids_json"]),
            "supersedes_id": row["supersedes_id"],
            "origin": row["origin"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "recorded_at": row["recorded_at"],
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

    def active(self, scope: str | None = None) -> list[dict[str, Any]]:
        """Return the current active taste projection, optionally by scope."""

        return self._latest_by_status("active", scope=scope)

    def pending(self) -> list[dict[str, Any]]:
        """Return adopted candidates still waiting for an explicit adopt."""

        return self._latest_by_status("candidate", scope=None)

    def by_status(self, status: str, scope: str | None = None) -> list[dict[str, Any]]:
        """Return the projection for any lifecycle status.

        ``paused`` and ``retired`` records are first-class states, so they must
        be auditable, not just writable.
        """

        if status not in VALID_TASTE_STATUSES:
            raise ValueError(f"invalid taste status: {status}")
        return self._latest_by_status(status, scope=scope)

    def _latest_by_status(
        self, status: str, scope: str | None
    ) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM taste_records ORDER BY recorded_at, rowid")
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        # Reviews are restricted to chain heads, so a non-superseded row is the
        # unique head of its lineage -- no fork-merging or tie-breaking needed.
        result = []
        for row in rows:
            if row["id"] in superseded:
                continue
            if row["status"] != status:
                continue
            if scope is not None and row["scope"] != scope:
                continue
            result.append(self.get(row["id"]))
        return sorted(result, key=lambda item: (item["recorded_at"], item["track"]))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _insert_taste(
        self,
        *,
        track: str,
        scope: str,
        content: Any,
        status: str,
        source_event_ids: list[str],
        origin: str,
        actor_id: str,
        reason: str | None,
    ) -> dict[str, Any]:
        # Same str-key guard as the store's append_event: bytes-like actor
        # ids would land in the table as BLOBs and silently split the ledger
        # (str reads never match a BLOB key).  Validate at the single INSERT
        # seam so every caller (authored / adopted / review-edit) is covered.
        for field_name, value in (("actor_id", actor_id), ("scope", scope), ("track", track)):
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be str, got {type(value).__name__}")
        taste_id = _id("tst")
        recorded_at = _now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO taste_records "
                "(id, track, scope, content_json, status, source_event_ids_json, "
                "supersedes_id, origin, actor_id, reason, recorded_at) "
                "VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    taste_id,
                    track,
                    scope,
                    _json(content),
                    status,
                    _json(list(dict.fromkeys(source_event_ids))),
                    origin,
                    actor_id,
                    reason,
                    recorded_at,
                ),
            )
            source_session = (
                self._source_session_from_ids(connection, source_event_ids)
                if source_event_ids
                else "user"
            )
            self.store.record_event(
                connection,
                source_session,
                "taste.proposed",
                {
                    "taste_id": taste_id,
                    "track": track,
                    "scope": scope,
                    "status": status,
                    "source_event_ids": list(dict.fromkeys(source_event_ids)),
                    "reason": reason,
                },
                occurred_at=recorded_at,
            )
        return self.get(taste_id)

    def _get_row(self, taste_id: str) -> Any:
        row = self.store.query_one("SELECT * FROM taste_records WHERE id = ?", (taste_id,))
        if row is None:
            raise KeyError(f"unknown taste record: {taste_id}")
        return row

    def _lineage_ids(self, row: Any) -> list[str]:
        """Return this record's id plus all its ancestors', oldest first."""

        ids = [row["id"]]
        current = row
        seen = {row["id"]}
        while current["supersedes_id"] is not None:
            parent = self.store.query_one(
                "SELECT * FROM taste_records WHERE id = ?",
                (current["supersedes_id"],),
            )
            # The INSERT guard and unique-child index make a missing parent or a
            # cycle structurally impossible on any database written since v5.
            # If one is ever seen (a pre-v5 file, or manual tampering), the
            # worst possible response is to silently truncate the audit trail --
            # evidence must never quietly disappear.  Fail loudly instead.
            if parent is None:
                raise RuntimeError(
                    f"taste lineage is broken: {current['id']} supersedes missing "
                    f"record {current['supersedes_id']}"
                )
            if parent["id"] in seen:
                raise RuntimeError(
                    f"taste lineage contains a cycle at {parent['id']}; "
                    "the database needs repair before it can be projected"
                )
            seen.add(parent["id"])
            ids.append(parent["id"])
            current = parent
        ids.reverse()
        return ids

    def _has_child(self, taste_id: str) -> bool:
        """Return whether any record supersedes ``taste_id`` (i.e. it is not a head)."""

        return (
            self.store.query_one(
                "SELECT 1 FROM taste_records WHERE supersedes_id = ? LIMIT 1", (taste_id,)
            )
            is not None
        )

    @staticmethod
    def _validate(*, scope: str, track: str) -> None:
        if track not in VALID_TASTE_TRACKS:
            raise ValueError(f"invalid taste track: {track}")
        if scope not in VALID_TASTE_SCOPES:
            raise ValueError(f"invalid taste scope: {scope}")

    @staticmethod
    def _source_session(connection: Any, row: Any) -> str:
        ids = _decode(row["source_event_ids_json"])
        return TasteService._source_session_from_ids(connection, ids)

    @staticmethod
    def _source_session_from_ids(connection: Any, ids: Iterable[str]) -> str:
        ids = list(ids)
        if not ids:
            return "user"
        row = connection.execute(
            "SELECT session_id FROM session_events WHERE id = ?", (ids[0],)
        ).fetchone()
        return row["session_id"] if row else "user"
