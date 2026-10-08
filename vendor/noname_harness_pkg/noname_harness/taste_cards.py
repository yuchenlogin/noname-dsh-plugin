"""Taste cards: a review experience over taste evidence, never a new fact.

A card is a *view* that groups related taste records so a person can
periodically ask "is this still me?"  It is not a new kind of fact and never
enters the factual layers of a context package.  Cards are append-only and
versioned like everything else; correction writes a new card that supersedes
the old one.

What this prototype deliberately does and does not do:

- **Clustering** is deterministic (time-window + scope + track grouping of
  *reviewed, active* taste records).  It is a conservative placeholder for a
  future semantic/embedding clusterer, which must satisfy the same storage and
  review contract without bypassing it.  Grouping and naming stay explainable.
- **No image generation.**  A card carries an *image metadata contract*
  (model / prompt / seed / version) so a generated visual metaphor can be
  recorded and rebuilt later, but the default is pure typography (``image`` is
  ``None``).  An image is always optional decoration; it is never used to
  infer taste.
- **Review queue is deterministic.**  Cards surface by auditable priority --
  longest-unconfirmed, most-recently-changed taste evidence -- never by random
  rarity, streaks, or paid mechanics.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .store import (
    VALID_CARD_ACTIONS,
    VALID_CARD_STATUSES,
    VALID_CARD_TRACKS,
    HarnessStore,
    _decode,
    _id,
    _json,
    _now,
)
from .taste import TasteService

_MEDIA_SUFFIX = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

# Lifecycle transitions for a card.  ``split`` produces new cards and retires
# the original; it is handled specially rather than as a single status change.
_CARD_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"accept", "edit", "retire", "split"},
    "active": {"edit", "pause", "retire", "split"},
    "paused": {"edit", "resume", "retire", "split"},
    "retired": set(),
}


class TasteCardService:
    """Form, review and project taste cards on top of the taste layer."""

    def __init__(self, store: HarnessStore):
        self.store = store
        self.taste = TasteService(store)

    # ------------------------------------------------------------------
    # clustering (deterministic placeholder)
    # ------------------------------------------------------------------
    def propose_clusters(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        """Group active taste records into candidate card clusters.

        The deterministic rule groups active taste records by (scope, track).
        It is explainable and produces no surprising merges; a future semantic
        clusterer can replace it while honouring the same review contract.
        """

        active = self.taste.active(scope=scope)
        clusters: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in active:
            key = (record["scope"], record["track"])
            clusters.setdefault(key, []).append(record)
        proposals = []
        for (card_scope, track), records in sorted(clusters.items()):
            proposals.append(
                {
                    "track": track,
                    "scope": card_scope,
                    "taste_ids": [r["id"] for r in records],
                    "records": records,
                    "reason": (
                        f"{len(records)} active {track} taste record(s) in "
                        f"{card_scope} scope share a track and scope"
                    ),
                }
            )
        return proposals

    # ------------------------------------------------------------------
    # card creation & review
    # ------------------------------------------------------------------
    def create_card(
        self,
        *,
        title: str,
        attitude: str,
        track: str,
        scope: str,
        taste_ids: Sequence[str],
        representative_evidence: Sequence[str] | None = None,
        tensions: str | None = None,
        influence: str | None = None,
        image: dict[str, Any] | None = None,
        actor_id: str = "clusterer",
        status: str = "candidate",
    ) -> dict[str, Any]:
        """Create a card candidate from a cluster.  It starts unconfirmed."""

        if track not in VALID_CARD_TRACKS:
            raise ValueError(f"invalid card track: {track}")
        if scope not in {"user", "project"}:
            raise ValueError(f"invalid card scope: {scope}")
        if status not in VALID_CARD_STATUSES:
            raise ValueError(f"invalid card status: {status}")
        if not title.strip():
            raise ValueError("card title cannot be empty")
        if not attitude.strip():
            raise ValueError("card attitude cannot be empty")
        taste_ids = list(taste_ids)
        if not taste_ids:
            raise ValueError("a card must group at least one taste record")
        if len(set(taste_ids)) != len(taste_ids):
            raise ValueError("taste_ids must not contain duplicates")
        # Every grouped taste id must resolve -- a card cannot point at
        # evidence the ledger does not contain.
        for taste_id in taste_ids:
            self.taste.get(taste_id)
        if image is not None:
            self._validate_image(image)
        card_id = _id("crd")
        now = _now()
        with self.store.transaction() as connection:
            self._insert_card(
                connection,
                card_id=card_id,
                title=title,
                attitude=attitude,
                track=track,
                scope=scope,
                taste_ids=taste_ids,
                representative_evidence=list(representative_evidence or []),
                tensions=tensions,
                influence=influence,
                image=image,
                status=status,
                last_confirmed_at=now,
                supersedes_id=None,
                origin="cluster",
                actor_id=actor_id,
                recorded_at=now,
            )
        return self.get(card_id)

    def _insert_card(
        self,
        connection: Any,
        *,
        card_id: str,
        title: str,
        attitude: str,
        track: str,
        scope: str,
        taste_ids: list[str],
        representative_evidence: list[str],
        tensions: str | None,
        influence: str | None,
        image: dict[str, Any] | None,
        status: str,
        last_confirmed_at: str,
        supersedes_id: str | None,
        origin: str,
        actor_id: str,
        recorded_at: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> None:
        """Insert a card row and its ledger event inside a transaction."""

        # Same str-key guard as the store's append_event (see
        # TasteService._insert_taste): bytes-like ids become BLOBs and split
        # the ledger silently.  One seam covers create / review / split.
        for field_name, value in (
            ("actor_id", actor_id),
            ("scope", scope),
            ("track", track),
            ("card_id", card_id),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be str, got {type(value).__name__}")
        connection.execute(
            "INSERT INTO taste_cards "
            "(id, title, attitude, track, scope, taste_ids_json, "
            "representative_evidence_json, tensions, influence, image_json, "
            "status, valid_from, valid_to, last_confirmed_at, supersedes_id, "
            "origin, actor_id, recorded_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card_id,
                title,
                attitude,
                track,
                scope,
                _json(taste_ids),
                _json(representative_evidence),
                tensions,
                influence,
                _json(image) if image is not None else None,
                status,
                valid_from,
                valid_to,
                last_confirmed_at,
                supersedes_id,
                origin,
                actor_id,
                recorded_at,
            ),
        )
        self.store.record_event(
            connection,
            "system",
            "taste.card.generated",
            {
                "card_id": card_id,
                "title": title,
                "track": track,
                "scope": scope,
                "status": status,
                "taste_ids": taste_ids,
                "has_image": image is not None,
                "supersedes_id": supersedes_id,
            },
            occurred_at=recorded_at,
        )

    def review(
        self,
        card_id: str,
        action: str,
        reviewer_id: str,
        *,
        edited: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Review a card.  Confirmation and lifecycle are explicit, never silent.

        ``split`` is the only action that returns a list (the new cards); every
        other action returns the single new head card.
        """

        if action not in VALID_CARD_ACTIONS:
            raise ValueError(f"invalid card action: {action}")
        if not isinstance(reviewer_id, str):
            raise TypeError(f"reviewer_id must be str, got {type(reviewer_id).__name__}")
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        row = self._get_row(card_id)
        if self._has_child(card_id):
            raise ValueError(f"card {card_id} has been superseded; review the current head")
        allowed = _CARD_TRANSITIONS.get(row["status"], set())
        if action not in allowed:
            raise ValueError(f"cannot '{action}' a card in status '{row['status']}'")

        if action == "split":
            return self._split(row, reviewer_id, edited)

        new_status = {
            "accept": "active",
            "edit": row["status"],
            "pause": "paused",
            "resume": "active",
            "retire": "retired",
        }[action]
        # accept/edit/resume count as confirmation and bump last_confirmed_at.
        confirm = action in {"accept", "edit", "resume"}

        fields = {
            "title": row["title"],
            "attitude": row["attitude"],
            "tensions": row["tensions"],
            "influence": row["influence"],
            "image": _decode(row["image_json"]) if row["image_json"] else None,
        }
        if action == "edit":
            if edited is None:
                raise ValueError("edited content is required for edit")
            for key in ("title", "attitude", "tensions", "influence", "image"):
                if key in edited:
                    fields[key] = edited[key]
            # Re-run the same validation create_card applies -- an edit must not
            # be a way to sneak in an empty title/attitude or a bad image.
            if not str(fields["title"]).strip():
                raise ValueError("card title cannot be empty")
            if not str(fields["attitude"]).strip():
                raise ValueError("card attitude cannot be empty")
            if fields["image"] is not None:
                self._validate_image(fields["image"])

        new_id = _id("crd")
        now = _now()
        last_confirmed = now if confirm else row["last_confirmed_at"]
        with self.store.transaction() as connection:
            # Re-check under the write lock: the pre-transaction head check is a
            # fast path, but two concurrent reviewers could both pass it.  The
            # loser must see the head moved and abort rather than fork.
            still_head = connection.execute(
                "SELECT 1 FROM taste_cards WHERE supersedes_id = ? LIMIT 1", (card_id,)
            ).fetchone()
            if still_head is not None:
                raise ValueError(
                    f"card {card_id} was superseded concurrently; review the current head"
                )
            self._insert_card(
                connection,
                card_id=new_id,
                title=fields["title"],
                attitude=fields["attitude"],
                track=row["track"],
                scope=row["scope"],
                taste_ids=_decode(row["taste_ids_json"]),
                representative_evidence=_decode(row["representative_evidence_json"]),
                tensions=fields["tensions"],
                influence=fields["influence"],
                image=fields["image"],
                status=new_status,
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                last_confirmed_at=last_confirmed,
                supersedes_id=card_id,
                origin=row["origin"],
                actor_id=reviewer_id,
                recorded_at=now,
            )
            self.store.record_event(
                connection,
                "system",
                "taste.card.reviewed",
                {
                    "card_id": card_id,
                    "new_card_id": new_id,
                    "action": action,
                    "reviewer_id": reviewer_id,
                    "new_status": new_status,
                },
                occurred_at=now,
            )
        return self.get(new_id)

    def _split(
        self, row: Any, reviewer_id: str, edited: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Split a card into multiple cards, retiring the original.

        ``edited`` must be ``{"cards": [{title, attitude, taste_ids}, ...]}``.
        The original is retired; the new cards start as candidates referencing
        disjoint subsets of the original's taste records.
        """

        if not edited or "cards" not in edited or not isinstance(edited["cards"], list):
            raise ValueError("split requires edited={'cards': [...]}")
        parts = edited["cards"]
        if len(parts) < 2:
            raise ValueError("split requires at least two resulting cards")
        original_taste_ids = set(_decode(row["taste_ids_json"]))
        assigned: set[str] = set()
        new_cards = []
        for part in parts:
            part_ids = list(dict.fromkeys(part.get("taste_ids", [])))
            if not part_ids:
                raise ValueError("each split part must keep at least one taste record")
            unknown = set(part_ids) - original_taste_ids
            if unknown:
                raise ValueError(f"split part references taste records not in the card: {sorted(unknown)}")
            overlap = assigned & set(part_ids)
            if overlap:
                raise ValueError(f"split parts must be disjoint; duplicated: {sorted(overlap)}")
            assigned |= set(part_ids)
        if assigned != original_taste_ids:
            raise ValueError("split must account for every taste record in the card")
        # Validate every part up front (title/attitude/taste_ids/image), so a
        # bad part cannot abort the split half-way and leave the ledger
        # inconsistent.  This mirrors create_card's own checks.
        original_image = _decode(row["image_json"]) if row["image_json"] else None
        for part in parts:
            if not str(part.get("title", "")).strip():
                raise ValueError("each split part needs a non-empty title")
            if not str(part.get("attitude", "")).strip():
                raise ValueError("each split part needs a non-empty attitude")
            part_image = part.get("image", original_image)
            if part_image is not None:
                self._validate_image(part_image)

        # Atomic: retire the original AND create all sub-cards in one
        # transaction.  If anything fails, nothing is written -- the ledger can
        # never hold a retired original with only some of its parts.
        now = _now()
        with self.store.transaction() as connection:
            still_head = connection.execute(
                "SELECT 1 FROM taste_cards WHERE supersedes_id = ? LIMIT 1", (row["id"],)
            ).fetchone()
            if still_head is not None:
                raise ValueError(
                    f"card {row['id']} was superseded concurrently; review the current head"
                )
            # Retire the original.
            retired_id = _id("crd")
            self._insert_card(
                connection,
                card_id=retired_id,
                title=row["title"],
                attitude=row["attitude"],
                track=row["track"],
                scope=row["scope"],
                taste_ids=_decode(row["taste_ids_json"]),
                representative_evidence=_decode(row["representative_evidence_json"]),
                tensions=row["tensions"],
                influence=row["influence"],
                image=original_image,
                status="retired",
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                last_confirmed_at=row["last_confirmed_at"],
                supersedes_id=row["id"],
                origin=row["origin"],
                actor_id=reviewer_id,
                recorded_at=now,
            )
            self.store.record_event(
                connection,
                "system",
                "taste.card.reviewed",
                {
                    "card_id": row["id"],
                    "new_card_id": retired_id,
                    "action": "retire",
                    "reviewer_id": reviewer_id,
                    "new_status": "retired",
                    "via": "split",
                },
                occurred_at=now,
            )
            # Create the disjoint candidate sub-cards (inheriting the image).
            new_ids = []
            for part in parts:
                new_id = _id("crd")
                new_ids.append(new_id)
                self._insert_card(
                    connection,
                    card_id=new_id,
                    title=part["title"],
                    attitude=part["attitude"],
                    track=row["track"],
                    scope=row["scope"],
                    taste_ids=part["taste_ids"],
                    representative_evidence=part.get(
                        "representative_evidence",
                        _decode(row["representative_evidence_json"]),
                    ),
                    tensions=part.get("tensions", row["tensions"]),
                    influence=part.get("influence", row["influence"]),
                    image=part.get("image", original_image),
                    status="candidate",
                    last_confirmed_at=now,
                    supersedes_id=None,
                    origin="split",
                    actor_id=reviewer_id,
                    recorded_at=now,
                )
        return [self.get(card_id) for card_id in new_ids]

    # ------------------------------------------------------------------
    # queries & review queue
    # ------------------------------------------------------------------
    def get(self, card_id: str) -> dict[str, Any]:
        row = self._get_row(card_id)
        return self._project(row)

    def by_status(self, status: str, scope: str | None = None) -> list[dict[str, Any]]:
        if status not in VALID_CARD_STATUSES:
            raise ValueError(f"invalid card status: {status}")
        return [
            self._project(row)
            for row in self._head_rows()
            if row["status"] == status and (scope is None or row["scope"] == scope)
        ]

    def review_queue(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """A deterministic review queue, ordered by auditable priority.

        Priority: (1) candidate cards awaiting a first decision, then
        (2) active cards ordered by longest time since last confirmation.
        No randomness, no rarity -- the queue exists to prompt judgement.
        """

        heads = self._head_rows()
        # Candidates first (awaiting a first decision), ordered by an explicit,
        # auditable rule -- oldest first, id as tie-break -- never by insertion
        # accident.  Active cards follow, by longest time since confirmation.
        # Tie-break on rowid (monotonic insertion order), not the random id, so
        # the queue is deterministic even when two cards share a
        # second-resolution timestamp.
        candidates = sorted(
            (r for r in heads if r["status"] == "candidate"),
            key=lambda r: (r["recorded_at"], r["_rowid"]),
        )
        active = sorted(
            (r for r in heads if r["status"] == "active"),
            key=lambda r: (r["last_confirmed_at"], r["_rowid"]),
        )
        ordered = candidates + active
        return [self._project(row) for row in ordered[:limit]]

    def generate_image(
        self,
        card_id: str,
        reviewer_id: str,
        *,
        generator: Any = None,
    ) -> dict[str, Any]:
        """Generate (or regenerate) the card's visual metaphor and record it.

        The image is written to the workspace (``.noname/card-images/``) and the
        card is versioned to reference it with its rebuildable metadata.  This
        is a review-like action: it produces a new card head whose ``image``
        carries the generator contract, so the visual explanation is itself
        traceable and rebuildable.  The default generator is the deterministic
        typographic renderer; a real image model plugs in behind the same
        protocol.
        """

        from .card_images import card_image_for

        row = self._get_row(card_id)
        if self._has_child(card_id):
            raise ValueError(f"card {card_id} has been superseded; generate on the current head")
        card = self._project(row)
        generated = card_image_for(card, generator)

        # The abstract/no_faces attestation must come from the generator's own
        # metadata, never stamped by the service on a plugin it knows nothing
        # about (a photorealistic generator must not be mislabelled no_faces).
        metadata = dict(generated.metadata)
        metadata.setdefault("abstract", False)
        metadata.setdefault("no_faces", False)
        # Derive the file extension from the media type, so a binary payload
        # (PNG/JPEG) is never saved under a misleading ".svg" name.
        suffix = _MEDIA_SUFFIX.get(generated.media_type, ".bin")

        # Atomic persistence: record the image bytes as an append-only evidence
        # span on a card.image.generated event FIRST (inside the ledger), then
        # export a workspace file as a convenience cache.  The card version
        # references the event id + path, so the visual explanation is itself
        # traceable evidence and the filesystem can never be the source of
        # truth.  If the later review fails, the image file is just an
        # unreferenced cache entry -- the ledger stays consistent.
        from .models import EvidenceInput

        image_event = self.store.append_event(
            "system",
            "card.image.generated",
            {
                "card_id": card_id,
                "model": metadata.get("model"),
                "seed": metadata.get("seed"),
                "media_type": generated.media_type,
            },
            [EvidenceInput(
                generated.image_bytes.decode("utf-8", errors="replace")
                if generated.media_type == "image/svg+xml"
                else generated.image_bytes.hex(),
                f"card-image://{card_id}",
            )],
        )
        filename = f".noname/card-images/{card_id}{suffix}"
        written = self.store.write_bytes_nofollow(
            filename, generated.image_bytes, media_suffix=suffix
        )
        image = {
            **metadata,
            "media_type": generated.media_type,
            "path": str(written.relative_to(self.store.project()["workspace_root"]))
            if str(written).startswith(str(self.store.project()["workspace_root"]))
            else str(written),
            "event_id": image_event.id,
            "note": "visual explanation, not evidence; never used to infer taste",
        }
        return self.review(
            card_id,
            "edit",
            reviewer_id,
            edited={"image": image},
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _head_rows(self) -> list[Any]:
        rows = self.store.query(
            "SELECT rowid AS _rowid, * FROM taste_cards ORDER BY recorded_at, rowid"
        )
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        return [row for row in rows if row["id"] not in superseded]

    def _get_row(self, card_id: str) -> Any:
        row = self.store.query_one("SELECT * FROM taste_cards WHERE id = ?", (card_id,))
        if row is None:
            raise KeyError(f"unknown taste card: {card_id}")
        return row

    def _has_child(self, card_id: str) -> bool:
        return (
            self.store.query_one(
                "SELECT 1 FROM taste_cards WHERE supersedes_id = ? LIMIT 1", (card_id,)
            )
            is not None
        )

    def _project(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "attitude": row["attitude"],
            "track": row["track"],
            "scope": row["scope"],
            "taste_ids": _decode(row["taste_ids_json"]),
            "representative_evidence": _decode(row["representative_evidence_json"]),
            "tensions": row["tensions"],
            "influence": row["influence"],
            "image": _decode(row["image_json"]) if row["image_json"] else None,
            "status": row["status"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "last_confirmed_at": row["last_confirmed_at"],
            "supersedes_id": row["supersedes_id"],
            "origin": row["origin"],
            "actor_id": row["actor_id"],
            "recorded_at": row["recorded_at"],
            # Staleness is surfaced, not hidden: a card whose taste records are
            # no longer all current active heads is drifting from the taste
            # layer, and a reviewer should know before confirming "still me".
            "stale": self._is_stale(row),
        }

    def _is_stale(self, row: Any) -> bool:
        """Return whether any grouped taste record is no longer an active head.

        A card is a view over taste evidence; when a taste record is retired or
        superseded, the card's evidence drifts.  This is deterministic and
        cheap, and only *annotates* -- it never blocks or auto-updates a card.
        """

        active_head_ids = {t["id"] for t in self.taste.active()}
        return any(tid not in active_head_ids for tid in _decode(row["taste_ids_json"]))

    @staticmethod
    def _validate_image(image: dict[str, Any]) -> None:
        """Validate the image metadata contract (rebuildable, never evidentiary).

        An image is a visual metaphor only: it must record enough to be
        regenerated (model, prompt, seed, version) and is never used to infer
        taste.  This validates the contract, not the image content.
        """

        if not isinstance(image, dict):
            raise ValueError("image must be an object")
        for key in ("model", "prompt", "seed", "version"):
            if key not in image:
                raise ValueError(f"image metadata requires '{key}' for rebuildability")
