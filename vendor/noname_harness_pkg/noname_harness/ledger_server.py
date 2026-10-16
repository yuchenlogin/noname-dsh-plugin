"""Loopback-only human review UI; all decisions use the existing kernel gates."""

from __future__ import annotations

import hmac
import hashlib
import json
import math
import secrets
import selectors
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .ledger_review import StaleReviewError, review_version
from .ledger_view import build_ledger_model, render_ledger_html
from .store import HarnessStore
from .taste import TasteService
from .taste_cards import TasteCardService

MAX_BODY = 256 * 1024
_REVISION_TABLES = (
    "session_events", "evidence_spans", "state_proposals", "proposal_reviews",
    "state_revisions", "taste_records", "taste_reviews", "taste_cards",
)
_ACTIONS = {
    "state_proposal": {"accept", "reject", "defer", "edit"},
    "taste_candidate": {"adopt", "retire"},
    "taste_card_candidate": {"accept", "retire", "edit"},
}


def ledger_revision(db_path: Path) -> str:
    """A cheap read-only fingerprint of append-only user-facing records.

    Do not render full history for a presence check and do not infer activity
    from process liveness. A single SQLite read snapshot covers all tables.
    """
    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("BEGIN")
        heads = [connection.execute(f'SELECT coalesce(max(rowid), 0) FROM "{table}"').fetchone()[0]
                 for table in _REVISION_TABLES]
        # Current state also changes when validity boundaries pass, without a
        # write. Track crossed boundaries using the kernel's UTC-second format
        # and inclusive valid_to convention. Include old active revisions too:
        # an occasional harmless refresh avoids duplicating head selection.
        moment = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        boundaries = connection.execute(
            "SELECT coalesce(sum(valid_from IS NOT NULL AND valid_from <= ?), 0), "
            "coalesce(sum(valid_to IS NOT NULL AND valid_to < ?), 0) "
            "FROM state_revisions WHERE status = 'active'", (moment, moment),
        ).fetchone()
        return hashlib.sha256(json.dumps([heads, boundaries]).encode("ascii")).hexdigest()
    finally:
        connection.close()


class LedgerServer(ThreadingHTTPServer):
    def __init__(self, db_path: Path, port: int, session_id: str | None,
                 limit: int | None, idle_timeout: float):
        self.db_path = db_path
        self.session_id = session_id
        self.limit = limit
        self.idle_timeout = idle_timeout
        self.token = secrets.token_urlsafe(32)
        self.last_activity = time.monotonic()
        self._expiring = False
        super().__init__(("127.0.0.1", port), LedgerHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.url = f"{self.origin}/{self.token}/"

    def service_actions(self) -> None:
        if not self._expiring and time.monotonic() - self.last_activity >= self.idle_timeout:
            self._expiring = True
            threading.Thread(target=self.shutdown, daemon=True).start()


class LedgerHandler(BaseHTTPRequestHandler):
    server: LedgerServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format: str, *args: Any) -> None:
        # The capability URL and database contents must not enter access logs.
        pass

    def _reply(self, status: int, value: Any, *, html: bool = False) -> None:
        raw = (value if html else json.dumps(value, ensure_ascii=False)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized_path(self) -> str | None:
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self._reply(403, {"error": "Invalid local Host"})
            return None
        path = urlsplit(self.path).path
        prefix = f"/{self.server.token}/"
        if not path.startswith(prefix):
            self._reply(404, {"error": "账本入口不存在或已过期。"})
            return None
        return path[len(prefix):]

    def do_GET(self) -> None:
        route = self._authorized_path()
        if route is None:
            return
        if route == "api/health":
            self.server.last_activity = time.monotonic()
            self._reply(200, {"ok": True})
            return
        if route == "api/updates":
            try:
                # Polling alone must not keep an abandoned server alive.
                self._reply(200, {"revision": ledger_revision(self.server.db_path)})
            except (sqlite3.Error, OSError) as exc:
                self._reply(503, {"error": str(exc)})
            return
        if route:
            self._reply(404, {"error": "Not found"})
            return
        try:
            revision = ledger_revision(self.server.db_path)
            with HarnessStore(self.server.db_path) as store:
                model = build_ledger_model(store, session_id=self.server.session_id,
                                           limit=self.server.limit)
                page = render_ledger_html(model, review_context={"token": self.server.token, "revision": revision})
            self.server.last_activity = time.monotonic()
            self._reply(200, page, html=True)
        except (ValueError, KeyError, sqlite3.Error, OSError) as exc:
            self._reply(500, {"error": str(exc)})

    def do_POST(self) -> None:
        route = self._authorized_path()
        if route is None:
            return
        token = self.headers.get("X-NoName-Token", "")
        if self.headers.get("Origin") != self.server.origin or not token.isascii() or not hmac.compare_digest(token, self.server.token):
            self._reply(403, {"error": "请从本地账本页面提交审核。"})
            return
        if route == "api/stop":
            self._reply(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if route != "api/review":
            self._reply(404, {"error": "Not found"})
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("审核请求必须使用 application/json。")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("审核请求为空或过大。")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            body = json.loads(raw)
            result = review_candidate(self.server.db_path, body)
            self.server.last_activity = time.monotonic()
            self._reply(200, {"result": result, "message": "审核已写入账本。"})
        except StaleReviewError as exc:
            self._reply(409, {"error": str(exc)})
        except KeyError:
            self._reply(404, {"error": "候选不存在，请刷新账本。"})
        except (ValueError, TypeError, UnicodeError) as exc:
            self._reply(400, {"error": str(exc)})
        except sqlite3.OperationalError as exc:
            self._reply(503, {"error": str(exc)})


def review_candidate(db_path: str | Path, body: Any) -> Any:
    if not isinstance(body, dict):
        raise ValueError("审核请求必须是 JSON object。")
    kind, action = body.get("kind"), body.get("action")
    if not isinstance(kind, str) or kind not in _ACTIONS or action not in _ACTIONS[kind]:
        raise ValueError("不支持的候选类型或审核操作。")
    for field in ("id", "reviewer_id"):
        if not isinstance(body.get(field), str) or not body[field].strip():
            raise ValueError(f"{field} cannot be empty")
    if not isinstance(body.get("expected_version"), str) or not body["expected_version"].strip():
        raise StaleReviewError("审核缺少候选版本，请刷新账本。")
    if kind == "state_proposal" and not isinstance(body.get("expected_head_id"), str):
        raise StaleReviewError("审核缺少当前状态版本，请刷新账本。")
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")
    edited = body.get("edited_content")
    if action == "edit" and edited is None:
        raise ValueError("修改审核需要 edited_content。")
    if action != "edit" and "edited_content" in body:
        raise ValueError("edited_content only applies to edit")
    with HarnessStore(db_path) as store:
        groups = store.review_inbox()
        pending = [item for key in ("canon_pending", "task_pending", "taste_pending", "card_pending")
                   for item in groups[key]]
        target = next((item for item in pending if item["id"] == body["id"] and item["kind"] == kind), None)
        if target is None:
            # Distinguish missing IDs from a page whose candidate is no longer pending.
            getter = {"state_proposal": store.get_proposal, "taste_candidate": TasteService(store).get,
                      "taste_card_candidate": TasteCardService(store).get}[kind]
            getter(body["id"])
            raise StaleReviewError("候选已审核或被新版本取代，请刷新账本。")
        options = {"expected_version": body["expected_version"]}
        if kind == "state_proposal":
            return store.review_proposal(body["id"], action, body["reviewer_id"].strip(),
                                         reason=reason, edited_content=edited,
                                         expected_head_id=body["expected_head_id"], **options)
        if kind == "taste_candidate":
            return TasteService(store).review(body["id"], action, body["reviewer_id"].strip(),
                                               reason=reason, **options)
        if edited is not None and not isinstance(edited, dict):
            raise ValueError("品味卡修改内容必须是 JSON object。")
        return TasteCardService(store).review(body["id"], action, body["reviewer_id"].strip(),
                                               edited=edited, reason=reason, **options)


def create_ledger_server(db_path: str | Path, *, port: int = 0,
                         session_id: str | None = None, limit: int | None = 200,
                         idle_timeout: float = 3600) -> LedgerServer:
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError("idle_timeout must be finite and positive")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    path = Path(db_path).expanduser().resolve(strict=True)
    with HarnessStore(path) as store:
        store.project()  # Never create or initialize a project just by opening a page.
    return LedgerServer(path, port, session_id, limit, idle_timeout)


def start_background_ledger(db_path: str | Path, *, port: int = 0,
                            session_id: str | None = None, limit: int = 200,
                            idle_timeout: float = 3600) -> dict[str, Any]:
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError("idle_timeout must be finite and positive")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    args = [sys.executable, "-m", "noname_harness", "ledger-serve", "--db",
            str(Path(db_path).expanduser().resolve(strict=True)), "--port", str(port),
            "--limit", str(limit), "--idle-timeout", str(idle_timeout)]
    if session_id is not None:
        args.extend(["--session", session_id])
    child = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parent.parent),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                raise RuntimeError("本地账本服务启动超时。")
            result = json.loads(child.stdout.readline())
        if not isinstance(result, dict) or not result.get("url"):
            raise RuntimeError("Invalid ledger service readiness response")
        return result
    except Exception:
        child.terminate()
        child.wait(timeout=5)
        raise
    finally:
        child.stdout.close()
