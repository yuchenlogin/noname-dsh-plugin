"""Command line interface for the local prototype."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .context import render_markdown
from .curator import CuratorService
from .models import EvidenceInput, ModelCapability, ModelProfile
from .recipes import DEFAULT_RECIPES, resolve_recipe
from .router import Router
from .ledger_view import build_ledger_model, render_ledger_html
from .extractor import MemoryExtractor
from .taste import TasteService
from .taste_cards import TasteCardService
from .store import HarnessStore, WorkspaceBoundaryError


DEFAULT_DB = ".noname/harness.db"


def _json_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _db_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noname-harness",
        description="Local-first evidence-backed context handoff prototype.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", parents=[_db_parent()], help="initialize a project database")
    init.add_argument("--root", default=".", help="workspace root")
    init.add_argument("--name", default="NoName project")

    event = sub.add_parser("event", parents=[_db_parent()], help="append an evidence event")
    event.add_argument("--session", required=True, dest="session_id")
    event.add_argument("--type", required=True, dest="event_type")
    event.add_argument("--payload", default="{}", help="JSON event payload")
    event.add_argument("--evidence", action="append", default=[])
    event.add_argument("--artifact-uri")

    snapshot = sub.add_parser("snapshot", parents=[_db_parent()], help="record a read-only workspace/git snapshot")
    snapshot.add_argument("--session", required=True, dest="session_id")

    extract = sub.add_parser("extract", parents=[_db_parent()], help="run memory extraction over a session's events")
    extract.add_argument("--session", required=True, dest="session_id")
    extract.add_argument("--limit", type=int, default=200)
    extract.add_argument("--no-proposals", action="store_true", help="only report, don't create proposals")

    curate = sub.add_parser("curate", parents=[_db_parent()], help="turn structured hints into proposals")
    curate.add_argument("--event-id")
    curate.add_argument("--session")
    curate.add_argument("--limit", type=int, default=100)

    propose = sub.add_parser("propose", parents=[_db_parent()], help="create a durable-state proposal")
    propose.add_argument("--layer", choices=["high", "mid"], required=True)
    propose.add_argument("--key", required=True, dest="logical_key")
    propose.add_argument("--kind", default="state")
    propose.add_argument("--content", required=True, help="JSON proposal content")
    propose.add_argument("--source-event", action="append", required=True, dest="source_event_ids")
    propose.add_argument("--by", default="curator", dest="proposed_by")
    propose.add_argument("--confidence", type=float)
    propose.add_argument("--reason")

    review = sub.add_parser("review", parents=[_db_parent()], help="review a proposal")
    review.add_argument("--proposal-id", required=True)
    review.add_argument("--action", choices=["accept", "reject", "edit", "defer", "retire"], required=True)
    review.add_argument("--reviewer", required=True, dest="reviewer_id")
    review.add_argument("--content", dest="edited_content")
    review.add_argument("--reason")
    review.add_argument("--valid-from", dest="valid_from", help="when the fact becomes true in the world (ISO time)")
    review.add_argument("--valid-to", dest="valid_to", help="when the fact stops being true in the world (ISO time)")

    state = sub.add_parser("state", parents=[_db_parent()], help="show accepted durable state")
    state.add_argument("--layer", choices=["high", "mid"])

    proposals = sub.add_parser("proposals", parents=[_db_parent()], help="show reviewable state proposals")
    proposals.add_argument("--all", action="store_true", help="include already reviewed proposals")

    ledger = sub.add_parser("ledger", parents=[_db_parent()], help="show recent events")
    ledger.add_argument("--session")
    ledger.add_argument("--limit", type=int, default=50)

    ledger_html = sub.add_parser("ledger-html", parents=[_db_parent()], help="render the interactive ledger as an offline HTML page")
    ledger_html.add_argument("--session")
    ledger_html.add_argument("--limit", type=int, default=200)
    ledger_html.add_argument("--out", required=True, help="output HTML file (inside the workspace)")
    ledger_html.add_argument("--overwrite", action="store_true")

    search = sub.add_parser("search", parents=[_db_parent()], help="search event/evidence text")
    search.add_argument("query")
    search.add_argument("--session")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--semantic", action="store_true", help="use vector/semantic recall (requires an embedding index)")
    search.add_argument("--ranked", action="store_true", help="rerank semantic results by relevance/source/review/freshness (implies --semantic)")

    embed = sub.add_parser("embed", parents=[_db_parent()], help="(re)build the vector recall projection")
    embed.add_argument("--session")

    verify = sub.add_parser("verify", parents=[_db_parent()], help="verify event/evidence hashes")

    reindex = sub.add_parser("reindex", parents=[_db_parent()], help="rebuild the optional text index")

    package = sub.add_parser("package", parents=[_db_parent()], help="assemble a context package")
    package.add_argument("--task", required=True)
    package.add_argument("--session")
    package.add_argument("--low-limit", type=int, default=20)
    package.add_argument("--out")
    package.add_argument("--format", choices=["json", "markdown"], default="markdown")
    package.add_argument("--overwrite", action="store_true")
    package.add_argument("--task-type", choices=sorted(DEFAULT_RECIPES.keys()), dest="task_type",
                         help="task type used to resolve an advisory model recipe")
    package.add_argument("--model-id", dest="model_id", help="target model id to project the package for")
    package.add_argument("--budget", choices=["low", "medium", "high"], default="medium",
                         help="budget posture of the target model")
    package.add_argument("--context-window", type=int, dest="context_window",
                         help="usable context window (tokens) of the target model")

    taste_add = sub.add_parser("taste-add", parents=[_db_parent()], help="record an authored taste (active immediately)")
    taste_add.add_argument("--content", required=True, help="JSON taste content (examples and judgements, not adjectives)")
    taste_add.add_argument("--scope", choices=["user", "project"], default="user")
    taste_add.add_argument("--source-event", action="append", default=[], dest="source_event_ids")
    taste_add.add_argument("--by", default="user", dest="actor_id")
    taste_add.add_argument("--reason")

    taste_propose = sub.add_parser("taste-propose", parents=[_db_parent()], help="propose an adopted taste candidate from a model moment")
    taste_propose.add_argument("--content", required=True, help="JSON taste content")
    taste_propose.add_argument("--scope", choices=["user", "project"], default="user")
    taste_propose.add_argument("--source-event", action="append", required=True, dest="source_event_ids")
    taste_propose.add_argument("--by", default="model", dest="proposed_by")
    taste_propose.add_argument("--reason")

    taste_review = sub.add_parser("taste-review", parents=[_db_parent()], help="review a taste record")
    taste_review.add_argument("--taste-id", required=True)
    taste_review.add_argument("--action", choices=["adopt", "edit", "pause", "resume", "retire"], required=True)
    taste_review.add_argument("--reviewer", required=True, dest="reviewer_id")
    taste_review.add_argument("--content", dest="edited_content")
    taste_review.add_argument("--reason")

    cancel = sub.add_parser("cancel", parents=[_db_parent()], help="request cancellation of a session's running loop")
    cancel.add_argument("--session", required=True, dest="session_id")
    cancel.add_argument("--reason", default="user_cancelled", help="cancellation reason")

    route = sub.add_parser("route", parents=[_db_parent()], help="decide how a session's context should proceed")
    route.add_argument("--session", required=True, dest="session_id")
    route.add_argument("--task-type", dest="task_type")
    route.add_argument("--instruction", help="explicit user instruction (fork/rebirth/switch/subagent)")

    recipes = sub.add_parser("recipes", parents=[_db_parent()], help="show default model recipes by task type")
    recipes.add_argument("--task-type", choices=sorted(DEFAULT_RECIPES.keys()), dest="task_type")

    inbox = sub.add_parser("inbox", parents=[_db_parent()], help="show the review inbox (pending canon, task and taste)")

    taste_list = sub.add_parser("taste", parents=[_db_parent()], help="list taste records")
    taste_list.add_argument("--status", choices=["active", "candidate", "paused", "retired"], default="active")
    taste_list.add_argument("--scope", choices=["user", "project"])

    card_propose = sub.add_parser("card-propose", parents=[_db_parent()], help="propose taste-card clusters from active taste")
    card_propose.add_argument("--scope", choices=["user", "project"])

    card_create = sub.add_parser("card-create", parents=[_db_parent()], help="create a taste card candidate")
    card_create.add_argument("--title", required=True)
    card_create.add_argument("--attitude", required=True)
    card_create.add_argument("--track", choices=["authored", "adopted", "mixed"], required=True)
    card_create.add_argument("--scope", choices=["user", "project"], required=True)
    card_create.add_argument("--taste-id", action="append", required=True, dest="taste_ids")
    card_create.add_argument("--tensions")
    card_create.add_argument("--influence")
    card_create.add_argument("--by", default="clusterer", dest="actor_id")

    card_review = sub.add_parser("card-review", parents=[_db_parent()], help="review a taste card")
    card_review.add_argument("--card-id", required=True)
    card_review.add_argument("--action", choices=["accept", "edit", "pause", "resume", "retire", "split"], required=True)
    card_review.add_argument("--reviewer", required=True, dest="reviewer_id")
    card_review.add_argument("--edited", help="JSON edited fields, or {'cards':[...]} for split")

    card_queue = sub.add_parser("card-queue", parents=[_db_parent()], help="show the deterministic card review queue")
    card_queue.add_argument("--limit", type=int, default=5)

    card_image = sub.add_parser("card-image", parents=[_db_parent()], help="generate a card's visual metaphor image")
    card_image.add_argument("--card-id", required=True)
    card_image.add_argument("--reviewer", required=True, dest="reviewer_id")

    card_list = sub.add_parser("card", parents=[_db_parent()], help="list taste cards by status")
    card_list.add_argument("--status", choices=["candidate", "active", "paused", "retired"], default="active")
    card_list.add_argument("--scope", choices=["user", "project"])

    return parser


def _event_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "session_id": event.session_id,
        "seq": event.seq,
        "event_type": event.event_type,
        "payload": event.payload,
        "occurred_at": event.occurred_at,
        "content_hash": event.content_hash,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            with HarnessStore(args.db) as store:
                _print(store.initialize_project(args.root, args.name))
            return 0

        with HarnessStore(args.db) as store:
            if args.command == "event":
                payload = _json_value(args.payload)
                evidence = [
                    EvidenceInput(content=text, artifact_uri=args.artifact_uri)
                    for text in args.evidence
                ]
                event = store.append_event(args.session_id, args.event_type, payload, evidence)
                _print(_event_dict(event))
            elif args.command == "snapshot":
                _print(_event_dict(store.append_workspace_snapshot(args.session_id)))
            elif args.command == "extract":
                extractor = MemoryExtractor(store)
                report = extractor.scan(
                    session_id=args.session_id,
                    limit=args.limit,
                    create_proposals=not args.no_proposals,
                )
                _print(report.describe())
            elif args.command == "curate":
                curator = CuratorService(store)
                if args.event_id:
                    proposal = curator.propose_from_event(args.event_id)
                    _print(proposal or {"proposal": None})
                else:
                    _print(curator.scan(args.session, args.limit))
            elif args.command == "propose":
                proposal = store.create_proposal(
                    args.layer,
                    args.logical_key,
                    _json_value(args.content),
                    args.source_event_ids,
                    kind=args.kind,
                    proposed_by=args.proposed_by,
                    confidence=args.confidence,
                    reason=args.reason,
                )
                _print(proposal)
            elif args.command == "review":
                edited = _json_value(args.edited_content) if args.edited_content is not None else None
                # valid bounds only make sense when a revision is written;
                # silently dropping them on reject/defer would mislead the user.
                if args.action in {"reject", "defer"} and (
                    args.valid_from is not None or args.valid_to is not None
                ):
                    raise ValueError(
                        f"--valid-from/--valid-to do not apply to action '{args.action}'"
                    )
                _print(
                    store.review_proposal(
                        args.proposal_id,
                        args.action,
                        args.reviewer_id,
                        edited_content=edited,
                        reason=args.reason,
                        valid_from=args.valid_from,
                        valid_to=args.valid_to,
                    )
                )
            elif args.command == "state":
                _print(store.active_state(args.layer))
            elif args.command == "proposals":
                _print(store.list_proposals(pending_only=not args.all))
            elif args.command == "ledger":
                _print([_event_dict(event) for event in store.list_events(args.session, args.limit)])
            elif args.command == "ledger-html":
                model = build_ledger_model(store, session_id=args.session, limit=args.limit)
                html_text = render_ledger_html(model)
                destination = store.validate_output_path(args.out)
                if destination.exists() and not args.overwrite:
                    raise FileExistsError(f"refusing to overwrite existing file: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(html_text, encoding="utf-8")
                _print({"path": str(destination), "events": model["counts"]["events"]})
            elif args.command == "search":
                if args.ranked:
                    from .embeddings import local_hash_embedding

                    if not store.query("SELECT 1 FROM event_embeddings LIMIT 1"):
                        print(
                            "hint: embedding index is empty; run `embed` first for semantic/ranked search",
                            file=sys.stderr,
                        )
                    _print(
                        [
                            {
                                **_event_dict(hit["event"]),
                                "similarity": hit["similarity"],
                                "ref_id": hit["ref_id"],
                                "score": hit["score"],
                                "rerank_reasons": hit["rerank_reasons"],
                            }
                            for hit in store.search_events_ranked(
                                args.query,
                                local_hash_embedding,
                                session_id=args.session,
                                limit=args.limit,
                            )
                        ]
                    )
                elif args.semantic:
                    from .embeddings import local_hash_embedding

                    _print(
                        [
                            {**_event_dict(hit["event"]), "similarity": hit["similarity"], "ref_id": hit["ref_id"]}
                            for hit in store.search_events_semantic(
                                args.query,
                                local_hash_embedding,
                                session_id=args.session,
                                limit=args.limit,
                            )
                        ]
                    )
                else:
                    _print(
                        [
                            _event_dict(event)
                            for event in store.search_events(
                                args.query,
                                session_id=args.session,
                                limit=args.limit,
                            )
                        ]
                    )
            elif args.command == "embed":
                from .embeddings import local_hash_embedding

                _print(store.build_embedding_index(local_hash_embedding, session_id=args.session))
            elif args.command == "verify":
                result = store.verify_integrity()
                _print(result)
                if not result["ok"]:
                    return 1
            elif args.command == "reindex":
                _print({"rebuilt": store.rebuild_search_index()})
            elif args.command == "package":
                destination = None
                if args.out:
                    destination = store.validate_output_path(Path(args.out))
                    if destination.exists() and not args.overwrite:
                        raise FileExistsError(f"refusing to overwrite existing file: {destination}")
                # --budget/--context-window only mean something with a target
                # model; fail fast rather than silently drop them.
                if (args.context_window is not None or args.budget != "medium") and not args.model_id:
                    raise ValueError("--budget/--context-window require --model-id")
                model_profile = None
                if args.model_id:
                    model_profile = ModelProfile(
                        id=args.model_id,
                        capability=ModelCapability(context_window=args.context_window),
                        budget=args.budget,
                    )
                package_value = store.assemble_context_package(
                    args.task,
                    session_id=args.session,
                    low_limit=args.low_limit,
                    model=model_profile,
                    task_type=args.task_type,
                )
                if args.format == "json":
                    rendered = json.dumps(package_value, ensure_ascii=False, indent=2) + "\n"
                else:
                    rendered = render_markdown(package_value)
                if destination is not None:
                    # Delegate the write to the store's sanctioned path so the
                    # boundary/sidecar validation lives in exactly one place.
                    written = store.write_context_package(
                        destination, package_value, overwrite=True, rendered=rendered
                    )
                    _print({"package_id": package_value["package_id"], "path": str(written)})
                else:
                    print(rendered, end="")
            elif args.command == "taste-add":
                service = TasteService(store)
                _print(
                    service.record_authored(
                        _json_value(args.content),
                        scope=args.scope,
                        source_event_ids=args.source_event_ids,
                        actor_id=args.actor_id,
                        reason=args.reason,
                    )
                )
            elif args.command == "taste-propose":
                service = TasteService(store)
                _print(
                    service.propose_adopted(
                        _json_value(args.content),
                        scope=args.scope,
                        source_event_ids=args.source_event_ids,
                        proposed_by=args.proposed_by,
                        reason=args.reason,
                    )
                )
            elif args.command == "taste-review":
                service = TasteService(store)
                edited = _json_value(args.edited_content) if args.edited_content is not None else None
                _print(
                    service.review(
                        args.taste_id,
                        args.action,
                        args.reviewer_id,
                        edited_content=edited,
                        reason=args.reason,
                    )
                )
            elif args.command == "cancel":
                # Route through the same validation as AgentLoop.cancel so the
                # ledger stays clean whichever door the request comes through.
                from .agent_loop import AgentLoop

                if not args.reason.strip():
                    raise ValueError("cancel reason cannot be empty")
                store.append_event(
                    args.session_id,
                    "loop.cancel_requested",
                    {"reason": args.reason},
                )
                _print({"cancel_requested": args.session_id, "reason": args.reason})
            elif args.command == "route":
                router = Router(store)
                _print(
                    router.decide(
                        session_id=args.session_id,
                        task_type=args.task_type,
                        user_instruction=args.instruction,
                    ).describe()
                )
            elif args.command == "recipes":
                if args.task_type:
                    _print(resolve_recipe(args.task_type).describe())
                else:
                    _print({key: recipe.describe() for key, recipe in DEFAULT_RECIPES.items()})
            elif args.command == "inbox":
                _print(store.review_inbox())
            elif args.command == "taste":
                service = TasteService(store)
                _print(service.by_status(args.status, scope=args.scope))
            elif args.command == "card-propose":
                service = TasteCardService(store)
                clusters = service.propose_clusters(scope=args.scope)
                _print([
                    {k: v for k, v in c.items() if k != "records"} for c in clusters
                ])
            elif args.command == "card-create":
                service = TasteCardService(store)
                _print(
                    service.create_card(
                        title=args.title,
                        attitude=args.attitude,
                        track=args.track,
                        scope=args.scope,
                        taste_ids=args.taste_ids,
                        tensions=args.tensions,
                        influence=args.influence,
                        actor_id=args.actor_id,
                    )
                )
            elif args.command == "card-review":
                service = TasteCardService(store)
                edited = _json_value(args.edited) if args.edited else None
                _print(
                    service.review(
                        args.card_id,
                        args.action,
                        args.reviewer_id,
                        edited=edited,
                    )
                )
            elif args.command == "card-queue":
                service = TasteCardService(store)
                _print(service.review_queue(limit=args.limit))
            elif args.command == "card-image":
                service = TasteCardService(store)
                _print(service.generate_image(args.card_id, args.reviewer_id))
            elif args.command == "card":
                service = TasteCardService(store)
                _print(service.by_status(args.status, scope=args.scope))
            else:  # pragma: no cover - argparse guarantees a known command
                raise AssertionError(args.command)
        return 0
    except sqlite3.OperationalError as exc:
        # Database-environment failures (unopenable/read-only --db path, --db
        # pointing at a directory, malformed db file) surface from sqlite as
        # OperationalError -- a *user-facing* condition, reported like every
        # other CLI error.  Per PEP 249 OperationalError is itself a
        # DatabaseError subclass (database is locked / malformed file lands
        # here too, which is acceptable -- those are environment, not code),
        # but the *other* DatabaseError siblings stay loud: programming
        # errors such as a malformed SQL statement
        # (sqlite3.ProgrammingError/InternalError/IntegrityError) are bugs
        # and must keep surfacing as tracebacks.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError, RuntimeError, FileExistsError, WorkspaceBoundaryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
