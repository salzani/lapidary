"""Note queue and history in SQLite.

Exists for a specific reason: between "the LLM finished formatting" and "Notion
confirmed the write" there is a network call that **fails for real** (it did,
repeatedly, during this project's development). Without persistence, a failure
there means losing both the raw note and the ~40s of inference.

A note's lifecycle:

    captured ──formatted──> drafted ──published──> published
                               │
                               └──write error──> failed ──retry──> published

`drafted` and `failed` are what `pending()` returns: they already have a
ready draft, only the write is missing, so retry does not need to call the
model again.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..models import DestinationChoice, NoteDraft, NoteRecord, PageRef

DEFAULT_DB = Path.home() / ".local" / "share" / "notemcp" / "notes.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text       TEXT NOT NULL,
    hint           TEXT,
    draft_json     TEXT,
    provider       TEXT,
    status         TEXT NOT NULL,
    notion_page_id TEXT,
    notion_url     TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);
"""

ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("destination_kind", "TEXT"),
    ("destination_page_id", "TEXT"),
)
"""Additive migrations as `(column name, SQL declaration)` pairs.

Applied by comparing against `PRAGMA table_info` at open time, which makes the
migration idempotent and order-independent — no version table, no migration
ledger. That is sufficient precisely because every entry here is "add a
column".

The day a migration is anything else — a rename, a backfill with a
transformation, a changed constraint — this scheme cannot express it, and the
right move is to switch to a `PRAGMA user_version` ladder rather than bolt
special cases onto this tuple.

`destination_kind` and `destination_page_id` record the user's chosen
destination so a retry republishes where the note was originally aimed.
"""

PENDING_STATUSES = ("drafted", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _destination_columns(destination: DestinationChoice | None) -> tuple[str | None, str | None]:
    """`(destination_kind, destination_page_id)` to write.

    `None` (no choice yet, e.g. `capture` without a destination) writes NULL
    to both columns. A `DestinationChoice` always writes BOTH columns
    together — including an explicit NULL in page_id for DATABASE — because
    writing only the kind would leave a `destination_page_id` from a
    previous choice lingering on the row (Phase 6 risk 3: retry would go to
    the wrong page)."""
    if destination is None:
        return None, None
    return destination.kind.value, destination.page_id


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns missing from an older-schema database.

    Idempotent: running it twice does not raise, because each column is only
    added if it does not exist yet. Called from `__init__`, after
    `executescript(SCHEMA)`.
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
    for name, decl in ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {name} {decl}")


class NoteStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)

    @contextmanager
    def _connect(self):
        """Yield one connection per operation.

        SQLite refuses to share a connection across threads, and the UI calls
        this from QThreadPool workers. Opening per operation is simpler — and
        safer — than holding one connection behind a lock.

        `:memory:` is the exception: such a database ceases to exist when its
        connection closes, so the same connection is reused for its lifetime.
        That path exists for tests, which would otherwise get a fresh empty
        database on every call.
        """
        if str(self.path) == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            yield self._memory_conn
            self._memory_conn.commit()
            return

        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def capture(
        self,
        raw_text: str,
        hint: str | None = None,
        destination: DestinationChoice | None = None,
    ) -> int:
        """Register the raw note BEFORE calling the model.

        `destination` is optional here on purpose: if the note never reaches
        publish, retry still needs a destination — see `set_destination`,
        called again at the start of `publish()` to persist the destination
        **actually attempted** (the user may change the selection after
        formatting).
        """
        kind_value, page_id_value = _destination_columns(destination)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notes (raw_text, hint, status, destination_kind, "
                "destination_page_id, created_at, updated_at) "
                "VALUES (?, ?, 'captured', ?, ?, ?, ?)",
                (raw_text, hint, kind_value, page_id_value, _now(), _now()),
            )
            return int(cur.lastrowid)

    def set_destination(self, note_id: int, destination: DestinationChoice) -> None:
        """Persist the chosen destination, called BEFORE the write to Notion.

        This way a retry of a failed note goes to the same destination the
        original attempt targeted, even if the user changes the selection
        afterwards. Always writes BOTH columns, including an explicit NULL
        in `destination_page_id` when the destination is DATABASE — see
        `_destination_columns`.
        """
        kind_value, page_id_value = _destination_columns(destination)
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET destination_kind = ?, destination_page_id = ?, "
                "updated_at = ? WHERE id = ?",
                (kind_value, page_id_value, _now(), note_id),
            )

    def save_draft(self, note_id: int, draft: NoteDraft, provider: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET draft_json = ?, provider = ?, status = 'drafted', "
                "error = NULL, updated_at = ? WHERE id = ?",
                (draft.model_dump_json(), provider, _now(), note_id),
            )

    def mark_published(self, note_id: int, ref: PageRef) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET status = 'published', notion_page_id = ?, "
                "notion_url = ?, error = NULL, updated_at = ? WHERE id = ?",
                (ref.id, ref.url, _now(), note_id),
            )

    def mark_failed(self, note_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (error, _now(), note_id),
            )

    def get(self, note_id: int) -> NoteRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return NoteRecord.from_row(dict(row)) if row else None

    def pending(self) -> list[NoteRecord]:
        """Notes with a ready draft that have not reached Notion yet.

        `captured` is left out: without a draft, retry would have to call the
        model again, which is a very different operation (slow and possibly
        paid).
        """
        placeholders = ",".join("?" * len(PENDING_STATUSES))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM notes WHERE status IN ({placeholders}) ORDER BY id",
                PENDING_STATUSES,
            ).fetchall()
        return [NoteRecord.from_row(dict(r)) for r in rows]

    def recent(self, limit: int = 20) -> list[NoteRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [NoteRecord.from_row(dict(r)) for r in rows]
