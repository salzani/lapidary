"""Domain models shared by every layer of the application."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DocType = Literal["nota", "reuniao", "ideia", "tarefa", "referencia", "resumo"]


class DestinationKind(StrEnum):
    """Where a note can be published."""

    DATABASE = "database"
    """A row in the "Notas" database (data source)."""
    PAGE = "page"
    """A child page of NOTION_PARENT_PAGE_ID."""

    @classmethod
    def coerce(cls, value: str | None) -> "DestinationKind":
        """Unknown, empty, or `None` value coerces to DATABASE, never raises.

        Mechanical consolidation (B2) of three previously identical
        implementations — `ui/bridge.py:_kind_or_default`,
        `NoteRecord.from_row` (a hand-corrupted database, or a column left
        empty by an older migration) and `Settings.destination_kind`
        (`.env`/CLI) — each containing the same deliberate rule: an
        unrecognized value must never raise `ValueError`, it must fall back
        to the default destination instead. Does not normalize
        (`strip`/`lower`) itself: the caller decides that beforehand, because
        not every call site used to normalize (behavior preserved point by
        point; only the "unknown -> DATABASE" rule moved from three places
        into one).
        """
        if not value:
            return cls.DATABASE
        try:
            return cls(value)
        except ValueError:
            return cls.DATABASE


@dataclass(frozen=True)
class Destination:
    """The already-resolved destination: `kind` plus the concrete id to write to.

    `id` is a data_source_id when `kind` is DATABASE, or a page_id when
    `kind` is PAGE.
    """

    kind: DestinationKind
    id: str


@dataclass(frozen=True)
class DestinationChoice:
    """The user's CHOICE — what gets persisted per note (Phase 6).

    Distinct from `Destination`, which is the RESOLVED target: for DATABASE,
    `Destination.id` is a discovered data_source_id (it can change between
    capture and retry); `DestinationChoice.page_id` is an id the user
    explicitly pointed at (stable, it is intent, not discovery).

    `page_id is None` means root (`NOTION_PARENT_PAGE_ID`). `page_id == ""`
    is REJECTED on purpose: an empty string is a bug, and a bug that falls
    through to `or root` would silently publish to the wrong place (Phase 6
    risk 2). This is the opposite rule from `NoteRecord.from_row`, which
    coalesces `NULL` and `""` into the same "not chosen" value at the
    SQLite boundary — both are correct in their own place, see `from_row`.
    """

    kind: DestinationKind
    page_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind is DestinationKind.DATABASE and self.page_id is not None:
            raise ValueError("DestinationChoice(DATABASE, ...) cannot carry a page_id")
        if self.page_id is not None and not self.page_id.strip():
            raise ValueError(
                "an empty or whitespace-only page_id is not a valid choice — use None for the root"
            )

    @classmethod
    def database(cls) -> "DestinationChoice":
        return cls(kind=DestinationKind.DATABASE, page_id=None)

    @classmethod
    def page(cls, page_id: str | None = None) -> "DestinationChoice":
        return cls(kind=DestinationKind.PAGE, page_id=page_id)

    @property
    def is_root_page(self) -> bool:
        return self.kind is DestinationKind.PAGE and self.page_id is None


PARENT_PAGE_ID_REQUIRED_MESSAGE = (
    "the 'loose page' destination requires NOTION_PARENT_PAGE_ID in .env — "
    "NOTION_DATA_SOURCE_ID alone only works for the 'database' destination"
)
"""Message shared verbatim by `config.py` (RuntimeError, boot-time guard) and
`notion/base.py` (NotionWriteError, runtime guard) — consolidation B3: same
text, now a single constant; the two exceptions remain DIFFERENT on purpose
(one is a configuration error raised before anything is attempted, the other
is a failure while resolving the destination itself), only the text stopped
being duplicated."""

DOC_TYPES: tuple[str, ...] = (
    "nota",
    "reuniao",
    "ideia",
    "tarefa",
    "referencia",
    "resumo",
)
"""Values stored verbatim as the Notion select property on every published
page (see `notion/schema.py`'s `PROP_TYPE`) and referenced verbatim in the
prompt (`llm/prompts.py`) that instructs the model to pick one of them. NOT
UI copy — do not translate. Renaming any entry here would leave every
already-published page's "Tipo" value pointing at a select option this
project no longer writes, and would desync the prompt from what the select
actually accepts."""

MAX_TITLE_LEN = 100
MAX_SUMMARY_LEN = 400
MAX_TAGS = 5


def _require_text(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be empty")
    return v


def title_slug(text: str) -> str:
    """Canonical form for comparing two titles: no accents, no punctuation,
    no case, no duplicate whitespace.

    Public on purpose (Phase 6): it is the same rule `notion/browser.py` uses
    to match a parent-page name typed into the CLI, or to detect a duplicate
    title when creating a parent page from the UI — a single rule for the
    whole project, never reimplemented in JS (Phase 6 risk 8: normalization
    diverging between languages)."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class NoteDraft(BaseModel):
    """The LLM's output contract.

    This becomes a JSON Schema handed to providers as structured output, so
    any change here changes what the model is required to return. `body_md`
    must stay within the subset described in ``llm/prompts.py`` — that is
    exactly what ``notion/compiler.py`` knows how to translate.
    """

    title: str = Field(description="Short, descriptive title of the note")
    doc_type: DocType = Field(description="Category of the note")
    tags: list[str] = Field(default_factory=list)
    summary: str = Field(description="1-2 sentence summary")
    body_md: str = Field(description="Body of the note in Markdown")

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        """Truncate rather than reject an over-long title.

        Overflow is truncated instead of rejected on purpose: a second call
        to the model just because the title came back with 105 characters
        would double the latency, and the round trip is the slow part of the
        whole pipeline. `_clean_summary` below applies the same reasoning.
        """
        return _require_text(v)[:MAX_TITLE_LEN]

    @field_validator("summary")
    @classmethod
    def _clean_summary(cls, v: str) -> str:
        return _require_text(v)[:MAX_SUMMARY_LEN]

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        """Deduplicate, lowercase, and strip commas from tags.

        Commas are stripped because a comma inside a tag value breaks
        Notion's multi-select property.
        """
        seen: list[str] = []
        for tag in v:
            tag = tag.strip().lower()
            tag = tag.replace(",", " ").strip()
            if tag and tag not in seen:
                seen.append(tag)
        return seen[:MAX_TAGS]

    @field_validator("body_md")
    @classmethod
    def _body_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body_md must not be empty")
        return v.strip()

    @model_validator(mode="after")
    def _drop_duplicated_title(self) -> "NoteDraft":
        """Strip a leading H1 that merely repeats the title.

        Small models ignore the instruction not to repeat the title, and the
        result is a Notion page with the same text in the title and in the
        first heading. Fixing it deterministically here is more reliable
        than insisting harder in the prompt.
        """
        lines = self.body_md.split("\n")
        if lines and lines[0].startswith("# "):
            if title_slug(lines[0][2:]) == title_slug(self.title):
                self.body_md = "\n".join(lines[1:]).strip()
        return self


class FormatContext(BaseModel):
    """Optional hints passed to the provider alongside the raw text."""

    hint: str | None = None
    """Free-form user instruction, e.g. 'this is a meeting minute, extract action items'."""

    today: date = Field(default_factory=date.today)

    language: str | None = None
    """Force a specific output language, overriding the source note's own.

    `None` — the default — means the model mirrors whatever language the raw
    text was written in. That is almost always what a note-improving tool
    should do: silently translating someone's note is a change they did not
    ask for and cannot easily undo.

    Set it only when the caller genuinely wants a fixed output language.
    """


class PageRef(BaseModel):
    """What every NotionWriter returns after publishing."""

    id: str
    url: str


class NoteRecord(BaseModel):
    """A row of the `notes` table (see ``store/db.py``)."""

    id: int
    raw_text: str
    hint: str | None = None
    draft: NoteDraft | None = None
    provider: str | None = None
    status: Literal["captured", "drafted", "published", "failed"]
    destination_kind: DestinationKind = DestinationKind.DATABASE
    destination_page_id: str | None = None
    notion_page_id: str | None = None
    notion_url: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict) -> "NoteRecord":
        """Build a `NoteRecord` from a raw SQLite row.

        Two boundary rules live here, both deliberate:

        - Pydantic treats an explicit `None` as invalid, not as "use the
          default" — without coercing it, every pre-existing row (a column
          added by a migration, left with no value) would break the history
          view the first time it is opened. `DestinationKind.coerce` also
          covers an unrecognized value (a hand-corrupted database, or a
          future version not yet supported) without raising.
        - `NULL` and `''` are coalesced into the same "not chosen" value for
          `destination_page_id`. This is deliberate and the ONLY place in the
          project that does it: SQLite does not distinguish a TEXT column
          that never received a value from one that received an empty
          string, so this is the one honest place to treat both as the same
          thing. This is the OPPOSITE rule from `DestinationChoice`, which
          never accepts `""` — see Phase 6 risk 2. Both are correct in their
          own context: this method is absorbing an ambiguous storage
          boundary, `DestinationChoice` is guarding against a bug.
        """
        data = dict(row)
        draft_json = data.pop("draft_json", None)
        data["draft"] = NoteDraft.model_validate_json(draft_json) if draft_json else None

        data["destination_kind"] = DestinationKind.coerce(data.get("destination_kind"))

        raw_page_id = data.get("destination_page_id")
        data["destination_page_id"] = (raw_page_id or "").strip() or None

        return cls.model_validate(data)

    @property
    def destination_choice(self) -> "DestinationChoice":
        """The `DestinationChoice` equivalent of this row.

        A `database` row with `destination_page_id` set should never exist
        (it could only happen through manual database corruption) — the
        property discards the id in that case instead of raising, the same
        philosophy `from_row` applies to an unrecognized `destination_kind`.
        """
        if self.destination_kind is DestinationKind.DATABASE:
            return DestinationChoice.database()
        return DestinationChoice.page(self.destination_page_id)

    @property
    def title(self) -> str:
        if self.draft:
            return self.draft.title
        head = self.raw_text.strip().splitlines()[0] if self.raw_text.strip() else ""
        return (head[:60] + "…") if len(head) > 60 else head or "(empty)"
