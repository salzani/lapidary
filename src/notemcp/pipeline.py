"""Orchestration: raw text -> draft -> Notion page.

The steps are deliberately separate. The UI calls `format_note`, shows the
preview, lets you edit it, and only then calls `publish`. The CLI chains
both.

When a `NoteStore` is present, each step is recorded: the raw note enters the
database *before* the model call, and the draft is saved *before* the write
to Notion. A network failure while publishing then costs a retry, not the
whole note plus the ~40s of inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .llm.base import LLMProvider, build_provider
from .models import (
    Destination,
    DestinationChoice,
    DestinationKind,
    FormatContext,
    NoteDraft,
    NoteRecord,
    PageRef,
)
from .notion.base import NotionWriter
from .notion.browser import build_browser
from .store.db import NoteStore


@dataclass
class Pipeline:
    provider: LLMProvider
    writer: NotionWriter
    settings: Settings
    store: NoteStore | None = None
    _note_id: int | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        provider_spec: str | None = None,
        store: NoteStore | None = None,
        writer: NotionWriter | None = None,
    ) -> "Pipeline":
        """`writer` is optional: when omitted, it is built from
        `NOTION_WRITER` (the usual behavior). Passing a ready-made writer is
        what lets the UI bridge hold **one** instance per session instead of
        creating a new `NotionMcpWriter` — and a new `npx` process — on every
        publish/retry (Phase 6, step 14: fixes risk 14)."""
        provider = build_provider(provider_spec or settings.llm_provider, settings)
        return cls(
            provider=provider,
            writer=writer or _build_writer(settings),
            settings=settings,
            store=store,
        )

    def format_note(
        self,
        raw: str,
        ctx: FormatContext | None = None,
        destination: DestinationChoice = DestinationChoice.database(),
    ) -> NoteDraft:
        """Format `raw` into a `NoteDraft` via the configured LLM provider.

        `destination` here is only the best estimate at capture time (the
        UI's current selection) — if the note never reaches `publish`, retry
        still has a destination instead of silently falling back to the
        default. `publish()` writes the destination again, with the one
        *actually* chosen at publish time.
        """
        ctx = ctx or FormatContext()
        if self.store:
            self._note_id = self.store.capture(raw, ctx.hint, destination)

        draft = self.provider.format_note(raw, ctx)

        if self.store and self._note_id is not None:
            self.store.save_draft(self._note_id, draft, self.provider.id)
        return draft

    def publish(
        self,
        draft: NoteDraft,
        note_id: int | None = None,
        destination: DestinationChoice = DestinationChoice.database(),
    ) -> PageRef:
        """Write `draft` to Notion at `destination` and record the outcome.

        The destination is persisted BEFORE the write: the user may have
        changed it after formatting, and a retry needs to target the
        destination actually attempted, not the one estimated at capture
        time.
        """
        note_id = note_id if note_id is not None else self._note_id
        if self.store and note_id is not None:
            self.store.set_destination(note_id, destination)

        resolved = self._resolve_destination(destination)
        try:
            ref = self.writer.create_note(resolved, draft)
        except Exception as exc:
            if self.store and note_id is not None:
                self.store.mark_failed(note_id, str(exc))
            raise

        if self.store and note_id is not None:
            self.store.mark_published(note_id, ref)
        return ref

    def run(
        self,
        raw: str,
        ctx: FormatContext | None = None,
        destination: DestinationChoice = DestinationChoice.database(),
    ) -> tuple[NoteDraft, PageRef]:
        draft = self.format_note(raw, ctx, destination)
        return draft, self.publish(draft, destination=destination)

    def _resolve_destination(self, choice: DestinationChoice) -> Destination:
        """Resolve the `Destination` for `choice`, avoiding the network when possible.

        Four cases:
          - DATABASE with `NOTION_DATA_SOURCE_ID` set -> shortcut, no network.
            This guard from Phase 5 is KEPT deliberately: the shortcut never
            applies to PAGE — risk 2. Without it, choosing "loose page" would
            silently publish into the database and return a valid link, with
            no sign of the error.
          - PAGE with a concrete `page_id` -> still goes through
            `ensure_destination` (not a direct shortcut): it is simply the
            same path, and the empty-id guard in `page_destination` is
            already tested there. Since `kind is PAGE` never touches the
            network in any adapter, this remains free of network cost.
          - PAGE without `page_id` -> root (`NOTION_PARENT_PAGE_ID`), the
            usual behavior.
          - DATABASE without the shortcut -> discovery/creation via
            `ensure_destination`.
        """
        if choice.kind is DestinationKind.DATABASE:
            if self.settings.notion_data_source_id:
                return Destination(
                    kind=DestinationKind.DATABASE, id=self.settings.notion_data_source_id
                )
            return self.writer.ensure_destination(
                self.settings.notion_parent_page_id, DestinationKind.DATABASE
            )

        parent_page_id = (
            choice.page_id if choice.page_id is not None else self.settings.notion_parent_page_id
        )
        return self.writer.ensure_destination(parent_page_id, DestinationKind.PAGE)

    def retry_pending(
        self, override: DestinationChoice | None = None
    ) -> list[tuple[NoteRecord, PageRef | None, str | None]]:
        """Resend to Notion everything that already has a draft and was not published.

        Does not call the model again — the draft is already saved. Without
        `override`, uses the destination persisted on each record
        (`record.destination_choice`; a row predating this feature becomes
        `DestinationChoice.database()`).

        `override`, when given, RE-TARGETS every pending note to the same
        explicit destination — never a silent fallback. This is the escape
        hatch for risk 15: a `failed` note whose parent page has disappeared
        from Notion would stay stuck forever if retry kept insisting on the
        same dead id; `override` lets the caller (CLI `--retry --parent-id`)
        point it elsewhere. Because `publish()` persists the destination
        BEFORE writing, the override is also persisted — it is not a
        one-time use.

        Returns `(record, ref, error)` per note for the caller to report.
        A single note's failure is caught and reported rather than raised,
        so it cannot abort the rest of the queue.
        """
        if not self.store:
            return []

        results = []
        for record in self.store.pending():
            if record.draft is None:
                continue
            destination = override if override is not None else record.destination_choice
            try:
                ref = self.publish(record.draft, note_id=record.id, destination=destination)
            except Exception as exc:
                results.append((record, None, str(exc)))
            else:
                results.append((record, ref, None))
        return results


def _build_writer(settings: Settings) -> NotionWriter:
    """Pick the adapter from `NOTION_WRITER` (consolidation B1: the same
    logic `notion.browser.build_browser` implements, formerly duplicated
    here — the two ports (`NotionWriter`, `NotionBrowser`) are satisfied by
    the same concrete instance, so "write" and "browse" always resolve to the
    same adapter). Kept as its own function (instead of having call sites
    import `build_browser` directly) only so callers of
    `pipeline._build_writer` don't need to know the implementation actually
    lives in `notion/browser.py`.
    """
    return build_browser(settings)
