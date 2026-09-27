"""Durable synthetic pagination contract. No network or platform credentials."""
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
from typing import Protocol


@dataclass(frozen=True)
class Item:
    item_id: str
    author_id: str
    published_at: str


@dataclass(frozen=True)
class Page:
    items: tuple[Item, ...]
    next_cursor: str | None
    has_more: bool
    terminal_evidence: str | None = None


class AdapterFailure(Exception):
    def __init__(self, category: str, retry_after: float = 0):
        super().__init__(category)
        if category not in {"needs_login", "rate_limited", "timeout", "unavailable", "invalid_cursor", "reference_missing", "item_unavailable", "media_failed", "verification_required",
                            "stale_checkpoint", "invalid_page", "missing_cursor", "repeated_cursor", "empty_nonterminal_page", "missing_terminal_evidence", "identity_mismatch", "invalid_stable_id", "invalid_response"}:
            raise ValueError("unknown failure category")
        self.category = category
        self.retry_after = retry_after


class HistoryAdapter(Protocol):
    version: str

    def page(self, author_id: str, cursor: str | None) -> Page: ...


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, author_id TEXT NOT NULL,
                    adapter_version TEXT NOT NULL, cursor TEXT, pages INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'queued', coverage TEXT NOT NULL DEFAULT 'unknown',
                    reason TEXT, terminal_evidence TEXT, retry_at REAL NOT NULL DEFAULT 0,
                    started_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(platform, author_id));
                CREATE TABLE IF NOT EXISTS items (
                    platform TEXT NOT NULL, item_id TEXT NOT NULL, author_id TEXT NOT NULL,
                    published_at TEXT NOT NULL, PRIMARY KEY(platform, item_id));
                CREATE TABLE IF NOT EXISTS page_evidence (
                    run_id INTEGER NOT NULL, page_number INTEGER NOT NULL,
                    request_cursor TEXT NOT NULL, next_cursor TEXT, ids TEXT NOT NULL,
                    terminal_evidence TEXT, observed_at REAL NOT NULL,
                    PRIMARY KEY(run_id, page_number), UNIQUE(run_id, request_cursor));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, platform: str, author_id: str, version: str) -> int:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO runs(platform,author_id,adapter_version,started_at,updated_at) VALUES(?,?,?,?,?)",
                       (platform, author_id, version, time.time(), time.time()))
            return db.execute("SELECT id FROM runs WHERE platform=? AND author_id=?", (platform, author_id)).fetchone()[0]

    def get(self, run_id: int) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            result = dict(row)
            stats = db.execute("SELECT count(*),min(published_at),max(published_at) FROM items WHERE platform=? AND author_id=?",
                               (row["platform"], row["author_id"])).fetchone()
            result.update(item_count=stats[0], earliest=stats[1], latest=stats[2],
                          evidence_level="synthetic", detail_state="not_tested", media_state="not_tested")
            return result

    def all(self) -> list[dict]:
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY id")]
        return [self.get(i) for i in ids]

    def stop(self, run_id: int, state: str, reason: str, retry_at: float = 0):
        coverage = "blocked" if state in {"needs_login", "rate_limited", "failed"} else "partial"
        with self.connect() as db:
            db.execute("UPDATE runs SET state=?,coverage=?,reason=?,retry_at=?,updated_at=? WHERE id=?",
                       (state, coverage, reason, retry_at, time.time(), run_id))

    def commit_page(self, run_id: int, expected_cursor: str | None, page: Page):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row["cursor"] != expected_cursor or row["state"] == "succeeded":
                raise ValueError("stale_checkpoint")
            requested = expected_cursor or ""
            seen = {r[0] for r in db.execute("SELECT request_cursor FROM page_evidence WHERE run_id=?", (run_id,))}
            if type(page.has_more) is not bool:
                raise ValueError("invalid_page")
            if page.has_more:
                if not isinstance(page.next_cursor, str) or not page.next_cursor:
                    raise ValueError("missing_cursor")
                if page.next_cursor == requested or page.next_cursor in seen:
                    raise ValueError("repeated_cursor")
                if not page.items:
                    raise ValueError("empty_nonterminal_page")
            elif page.next_cursor is not None or not page.terminal_evidence:
                raise ValueError("missing_terminal_evidence")
            for item in page.items:
                if not item.item_id or item.author_id != row["author_id"]:
                    raise ValueError("identity_mismatch")
                old = db.execute("SELECT author_id FROM items WHERE platform=? AND item_id=?",
                                 (row["platform"], item.item_id)).fetchone()
                if old and old[0] != item.author_id:
                    raise ValueError("identity_mismatch")
                db.execute("INSERT INTO items VALUES(?,?,?,?) ON CONFLICT(platform,item_id) DO UPDATE SET published_at=excluded.published_at",
                           (row["platform"], item.item_id, item.author_id, item.published_at))
            db.execute("INSERT INTO page_evidence VALUES(?,?,?,?,?,?,?)",
                       (run_id, row["pages"] + 1, requested, page.next_cursor,
                        json.dumps([i.item_id for i in page.items]), page.terminal_evidence, time.time()))
            db.execute("UPDATE runs SET cursor=?,pages=pages+1,state=?,coverage=?,reason=NULL,terminal_evidence=?,retry_at=0,updated_at=? WHERE id=?",
                       (page.next_cursor, "running" if page.has_more else "succeeded",
                        "scanning" if page.has_more else "complete_for_accessible_scope",
                        page.terminal_evidence, time.time(), run_id))


def scan(store: Store, run_id: int, adapter: HistoryAdapter, *, max_pages=100, login_confirmed=False, now=None) -> dict:
    """Only safe typed errors persist. Unexpected exception details may contain secrets."""
    row = store.get(run_id)
    now = time.time() if now is None else now
    if row["state"] == "succeeded" or row["retry_at"] > now:
        return row
    if row["state"] == "needs_login" and not login_confirmed:
        return row
    if row["adapter_version"] != adapter.version:
        store.stop(run_id, "partial", "adapter_version_changed")
        return store.get(run_id)
    for _ in range(max_pages):
        row = store.get(run_id)
        try:
            page = adapter.page(row["author_id"], row["cursor"])
            store.commit_page(run_id, row["cursor"], page)
        except AdapterFailure as error:
            state = error.category if error.category in {"needs_login", "rate_limited"} else "partial"
            # This harness performs no automatic retry; a click cannot bypass cooldown.
            retry_at = now + max(error.retry_after, 5) if state == "rate_limited" else 0
            store.stop(run_id, state, error.category, retry_at)
            break
        except ValueError as error:
            allowed = {"stale_checkpoint", "invalid_page", "missing_cursor", "repeated_cursor",
                       "empty_nonterminal_page", "missing_terminal_evidence", "identity_mismatch"}
            store.stop(run_id, "partial", str(error) if str(error) in allowed else "invalid_response")
            break
        except Exception:
            store.stop(run_id, "failed", "unexpected_adapter_error")
            break
        if not page.has_more:
            break
    else:
        store.stop(run_id, "partial", "page_budget_reached")
    return store.get(run_id)


class SyntheticAdapter:
    version = "synthetic-v1"

    def __init__(self, failure_at: int | None = None, failure="timeout"):
        self.failure_at, self.failure = failure_at, failure

    def page(self, author_id: str, cursor: str | None) -> Page:
        number = int(cursor) if cursor else 0
        if number == self.failure_at:
            raise AdapterFailure(self.failure, retry_after=5)
        if not 0 <= number < 4:
            raise AdapterFailure("invalid_cursor")
        items = tuple(Item(f"{author_id}-{n:03}", author_id, f"2026-08-{n % 28 + 1:02}")
                      for n in range(number * 15, (number + 1) * 15))
        if number:
            items += (Item(f"{author_id}-000", author_id, "2026-08-01"),)  # Pinned duplicate.
        more = number < 3
        return Page(items, str(number + 1) if more else None, more,
                    None if more else "synthetic: explicit has_more=false on page 4")
