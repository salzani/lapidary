"""QWebChannel bridge between the HTML frontend and the Python pipeline.

JS calls the `@Slot`s; Python answers through `Signal`. Everything that
crosses the boundary is JSON-serialized — QWebChannel converts types
automatically, but passing an explicit string keeps the contract visible on
both sides.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal, Slot

from ..config import Settings, load_settings
from ..llm import registry
from ..llm.base import build_provider
from ..models import DestinationChoice, DestinationKind, FormatContext, NoteDraft, title_slug
from ..notion.base import NotionWriter
from ..notion.browser import ROOT_LABEL, NotionBrowser, PageNode, build_browser, match_by_title
from ..notion.compiler import md_to_html
from ..notion.metadata import metadata_callout_html
from ..notion.tree_cache import CachedBrowser
from ..pipeline import Pipeline
from ..store.db import NoteStore
from . import state
from .worker import run_async


def _partial_metadata_html(data: dict) -> str:
    """Metadata callout built from a tolerant dict, without `NoteDraft`.

    `metadata_callout_html` only reads attributes (`doc_type`, `tags`,
    `summary`) — a `SimpleNamespace` works just as well as a real
    `NoteDraft`, and doesn't trigger the validation that `renderPreview`
    needs to avoid (risk #5).
    """
    tags = data.get("tags")
    fake_draft = SimpleNamespace(
        doc_type=str(data.get("doc_type", "") or ""),
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        summary=str(data.get("summary", "") or ""),
    )
    return metadata_callout_html(fake_draft)


def _kind_or_default(value: str | None) -> DestinationKind:
    """Unknown or empty ⇒ DATABASE, no exception (risk #6).

    Covers `state.json` written by a future version, or hand-edited.
    Delegates to `DestinationKind.coerce` — the same rule used by
    `NoteRecord.from_row` and `Settings.destination_kind` (consolidation B2);
    the behavior here hasn't changed, it just stopped being implemented
    three times.
    """
    return DestinationKind.coerce(value)


def _first_line(text: str | None, limit: int = 160) -> str:
    """First line of an error, or empty.

    `"".splitlines()` returns `[]`, not `[""]` — indexing directly blows up
    with an IndexError right when the note was published successfully and
    the previous error was cleared.
    """
    lines = (text or "").splitlines()
    return lines[0][:limit] if lines else ""


class Bridge(QObject):
    """The Python half of the UI, exposed to `app.js` over QWebChannel.

    Every signal below carries a JSON string, because QWebChannel marshals
    only primitives. The payload shapes are part of the contract with
    `app.js` — changing one without changing the other breaks the interface
    silently, since QWebChannel reports neither a shape nor an arity
    mismatch:

    - `providersReady`     `{providers: [...], selected: "..."}`
    - `destinationsReady`  `{destinations: [...], selected: "..."}`
    - `draftReady`         a serialized `NoteDraft`
    - `published`          `{title, url}`
    - `historyReady`       `{notes: [...], pending: n}`
    - `pageTreeReady`      `{node, path, children, selected_id, can_go_up}`
    - `parentPageChanged`  `{id, title, path, is_root}`

    `busy` is the exception, carrying `(active, label)` directly.

    The rule that governs this class: **what the user selected travels as an
    argument of the action**, never read back from the bridge's own state at
    execution time. Saved state restores the initial selection and nothing
    more. Otherwise a slow worker could publish to whatever the dropdown was
    changed to while it ran, rather than to what was on screen when the user
    clicked.
    """

    providersReady = Signal(str)
    destinationsReady = Signal(str)
    draftReady = Signal(str)
    published = Signal(str)
    historyReady = Signal(str)
    failed = Signal(str)
    busy = Signal(bool, str)
    configWarning = Signal(str)
    pageTreeReady = Signal(str)
    parentPageChanged = Signal(str)

    def __init__(self, settings: Settings | None = None, store: NoteStore | None = None):
        super().__init__()
        self.settings = settings or load_settings()
        self.store = store or NoteStore(self.settings.notes_db or None)
        saved_provider = state.load().get("provider") or ""
        if not saved_provider.startswith("gemini:"):
            # Asymmetric on purpose. `LLM_PROVIDER` (`settings.llm_provider`)
            # is a CHOICE the user made in `.env`, and a bad one fails loud
            # via `build_provider` at format time. `state.json`'s `provider`
            # key is a REMEMBERED PREFERENCE from a previous session, not a
            # choice made now — a leftover `"ollama:..."` from before Ollama
            # was removed (or any other non-Gemini value, hand-edited or from
            # a future version) is discarded silently here, falling back to
            # `settings.llm_provider`, rather than surfacing an error for a
            # value the user never actively picked today.
            saved_provider = ""
        self._provider_id = saved_provider or self.settings.llm_provider
        self._destination_kind = _kind_or_default(state.load().get("destination"))
        self._note_id: int | None = None

        self._writer: NotionWriter | None = None
        """Raw adapter, built once and shared by publishing and browsing.

        One instance per session on purpose: under `NOTION_WRITER=mcp` every
        construction spawns an `npx` subprocess that stays alive until exit,
        so building one per action leaked a process per publish.
        """

        self._browser: NotionBrowser | None = None
        """`CachedBrowser` built on demand, one per session (never per call)."""

        self._root_node = PageNode(id=self.settings.notion_parent_page_id or "", title=ROOT_LABEL)

        self._browse_path: list[PageNode] = [self._root_node]
        """Ancestors of the level currently shown, committed only in `done`.

        Writing this before the response arrives would let a failed or late
        navigation leave the UI showing one level's breadcrumb beside another
        level's children.
        """

        self._browse_children: list[PageNode] = []
        self._browse_seq = 0
        """Monotonic token that lets a late response be recognised and dropped."""

        self._parent_page: PageNode | None = None
        """The selected publish target; `None` means the root page."""

        self._parent_page_path: list[PageNode] = []
        """Ancestors of the target, excluding the target itself.

        Persisted so the frontend can rebuild the full dropdown chain at
        startup. Without it, a target nested more than one level deep would
        restore as a label with no matching selection on screen.
        """

        restored, malformed = state.load_parent_page()
        self._parent_page_malformed = malformed
        if restored is not None:
            self._parent_page = PageNode(id=restored["id"], title=restored["title"])
            self._parent_page_path = [
                PageNode(id=p["id"], title=p["title"]) for p in restored.get("path", [])
            ]

    @Slot()
    def ready(self) -> None:
        """Push the initial state to the frontend once its page has loaded.

        Everything the UI needs to render before the user touches anything is
        emitted from here, never from `__init__`: signals connected after
        construction would otherwise be missed entirely.

        That includes the parent-page label. A target restored from a previous
        session has to be visible without opening the picker — a target that
        is set but unlabelled is how the screen ends up disagreeing with where
        the note will actually go.
        """
        def work():
            return registry.discover(self.settings)

        def done(providers):
            available = [p for p in providers if p.available]
            selected = self._provider_id
            if selected not in {p.id for p in available}:
                selected = available[0].id if available else ""
                self._provider_id = selected
            self.providersReady.emit(
                json.dumps({
                    "providers": [p.as_dict() for p in providers],
                    "selected": selected,
                })
            )

        run_async(work, done, self.failed.emit)
        self._emit_destinations()

        self._emit_parent_page_changed(persist=False)
        if self._parent_page_malformed:
            self.configWarning.emit(
                "não consegui restaurar a página escolhida; publicando na raiz"
            )

        try:
            self.settings.require_notion()
        except RuntimeError as exc:
            self.configWarning.emit(str(exc))

    @Slot(str)
    def selectProvider(self, provider_id: str) -> None:
        self._provider_id = provider_id
        state.save(provider=provider_id)

    @Slot(str)
    def selectDestination(self, kind: str) -> None:
        """Remember the destination the user picked.

        What is saved here only restores the initial selection on the next
        launch. The destination actually used when publishing travels as an
        argument of `publish`, so this value is never read back at write time.
        """
        self._destination_kind = _kind_or_default(kind)
        state.save(destination=self._destination_kind.value)

    def _emit_destinations(self) -> None:
        """Emit both destinations, following the provider-dropdown pattern.

        An unavailable option stays in the list, disabled and carrying the
        reason. Hiding it instead would leave the user wondering whether the
        feature exists; showing it enabled would let them pick something that
        fails later.
        """
        destinations = [
            {
                "kind": DestinationKind.DATABASE.value,
                "label": "Database Notas",
                "available": True,
                "reason": "",
            },
            {
                "kind": DestinationKind.PAGE.value,
                "label": "Página solta",
                "available": bool(self.settings.notion_parent_page_id),
                "reason": (
                    "" if self.settings.notion_parent_page_id
                    else "defina NOTION_PARENT_PAGE_ID no .env"
                ),
            },
        ]
        available_kinds = {d["kind"] for d in destinations if d["available"]}
        if self._destination_kind.value not in available_kinds:
            self._destination_kind = DestinationKind.DATABASE

        self.destinationsReady.emit(json.dumps({
            "destinations": destinations,
            "selected": self._destination_kind.value,
        }))

    def _ensure_writer(self) -> NotionWriter:
        """Return the session's single raw adapter, building it on first use.

        Shared between navigation and publishing. Constructing one per action
        would spawn a fresh `NotionMcpWriter` — and with it a fresh `npx`
        process that lives until interpreter exit — on every click under
        `NOTION_WRITER=mcp`.
        """
        if self._writer is None:
            self._writer = build_browser(self.settings)
        return self._writer

    def _ensure_browser(self) -> NotionBrowser:
        if self._browser is None:
            self._browser = CachedBrowser(self._ensure_writer())
        return self._browser

    def _children_payload(self, children: list[PageNode]) -> list[dict]:
        """Serialize children, flagging title collisions within the level.

        `ambiguous: true` marks any child whose normalized title matches
        another child of the *same* parent, which lets the UI append an id
        suffix. Notion happily accepts two sibling pages with identical
        titles, so without the flag the user would face two indistinguishable
        options and no way to tell which one they picked.
        """
        slugs: dict[str, int] = {}
        for child in children:
            slug = title_slug(child.title)
            slugs[slug] = slugs.get(slug, 0) + 1
        return [
            {"id": c.id, "title": c.title, "ambiguous": slugs[title_slug(c.title)] > 1}
            for c in children
        ]

    def _path_to(self, node: PageNode) -> list[PageNode]:
        """Return the full path to `node`: its ancestors plus itself.

        Derived from the level currently on screen, so `node` must be either
        that level or one of its children — which is exactly how
        `selectParentPage` and `createChildPage` reach this method.
        """
        if self._browse_path and self._browse_path[-1].id == node.id:
            return self._browse_path
        return [*self._browse_path, node]

    def _emit_page_tree(self) -> None:
        path = self._browse_path
        node = path[-1]
        self.pageTreeReady.emit(json.dumps({
            "node": {"id": node.id, "title": node.title},
            "path": [{"id": p.id, "title": p.title} for p in path],
            "children": self._children_payload(self._browse_children),
            "selected_id": self._parent_page.id if self._parent_page else None,
            "can_go_up": len(path) > 1,
        }))

    def _emit_parent_page_changed(self, persist: bool = True) -> None:
        """Emit `parentPageChanged` and persist the choice — the only place
        that does either.

        Keeping both in one method is what stops the on-screen target label
        from drifting away from the value actually published: the label the
        user reads and the id sent to Notion have to originate from the same
        variable, `self._parent_page`. Two writers of that state would
        eventually disagree, and the disagreement would be invisible until a
        note landed in the wrong place.
        """
        if self._parent_page is None:
            payload = {"id": None, "title": ROOT_LABEL, "path": [], "is_root": True}
            if persist:
                state.save_parent_page(None)
        else:
            path_payload = [{"id": p.id, "title": p.title} for p in self._parent_page_path]
            payload = {
                "id": self._parent_page.id,
                "title": self._parent_page.title,
                "path": path_payload,
                "is_root": False,
            }
            if persist:
                state.save_parent_page({
                    "id": self._parent_page.id,
                    "title": self._parent_page.title,
                    "path": path_payload,
                })
        self.parentPageChanged.emit(json.dumps(payload))

    def _browse(self, path: list[PageNode]) -> None:
        """Fetch the children of `path`'s last node and, if nothing moved in
        the meantime, commit the level and emit `pageTreeReady`.

        `_browse_seq` guards against out-of-order responses. Without it,
        clicking into a slow page and navigating away before it answers would
        paint one level's children under another level's position — and the
        user would publish somewhere they never chose. The guard is why
        `_browse_path` is assigned inside `done` and never before dispatching
        the worker: a navigation that fails, or that arrives late, must leave
        the interface exactly as it was.
        """
        self._browse_seq += 1
        seq = self._browse_seq
        target = path[-1]

        def work():
            return self._ensure_browser().list_child_pages(target.id)

        def done(children):
            if seq != self._browse_seq:
                return
            self._browse_path = path
            self._browse_children = children
            self._emit_page_tree()

        def error(msg: str):
            if seq != self._browse_seq:
                return
            self.failed.emit(f"não consegui listar '{target.title}': {msg}")

        run_async(work, done, error)

    @Slot()
    def openPageTree(self) -> None:
        """Load the level the picker should show, reusing this session's position.

        Reopening where the user left off rather than resetting to the root
        avoids re-walking the same path on every interaction.
        """
        if not self.settings.notion_parent_page_id:
            self.failed.emit("defina NOTION_PARENT_PAGE_ID no .env para escolher uma página-mãe.")
            return
        self._browse(self._browse_path)

    @Slot(str)
    def browseInto(self, page_id: str) -> None:
        """Descend into a child of the level currently loaded.

        `page_id` must be among the children already fetched. An arbitrary id
        arriving from JavaScript becomes a visible error rather than silent
        navigation to somewhere the user cannot see.
        """
        match = next((c for c in self._browse_children if c.id == page_id), None)
        if match is None:
            self.failed.emit("essa página não é uma subpágina do nível atual.")
            return
        self._browse([*self._browse_path, match])

    @Slot(str)
    def browseTo(self, page_id: str) -> None:
        """Reposition the navigation cursor onto an ancestor already in the path.

        Truncates `_browse_path` at that node. The dropdown chain uses this to
        move back up before descending a different branch, since changing a
        mid-chain selection means re-rooting the levels below it.
        """
        for i, node in enumerate(self._browse_path):
            if node.id == page_id:
                self._browse(self._browse_path[: i + 1])
                return
        self.failed.emit("essa página não está no caminho atual.")

    @Slot()
    def reloadPageTree(self) -> None:
        """Invalidates ONLY the currently displayed node — ancestors don't
        change just because a grandchild was born (see
        `tree_cache.CachedBrowser.invalidate`)."""
        target = self._browse_path[-1]
        self._ensure_browser().invalidate(target.id)
        self._browse(self._browse_path)

    @Slot(str, result=str)
    def checkChildTitle(self, title: str) -> str:
        """SYNCHRONOUS, NO NETWORK: compares `title` by slug against the
        children ALREADY LOADED in the bridge (`self._browse_children`).
        Never raises — it's called on every keystroke, same pattern as
        `renderPreview` (risk #5)."""
        try:
            matches = match_by_title(self._browse_children, title)
        except Exception:  # noqa: BLE001 - nunca pode quebrar a digitação
            matches = []
        return json.dumps({
            "duplicate": bool(matches),
            "matches": [{"id": m.id, "title": m.title} for m in matches],
        })

    @Slot(str)
    def createChildPage(self, title: str) -> None:
        """Creates inside the node currently being VIEWED (`_browse_path[-1]`),
        not the selected target — the dropdown chain is a navigation
        surface, "here" is wherever you currently are. Success: it becomes
        the target, and the dropdown chain stays open at the same level
        (visual confirmation). Error: neither the target nor the level
        changes."""
        title = title.strip()
        if not title:
            self.failed.emit("dê um título para a página.")
            return

        parent_path = self._browse_path
        parent = parent_path[-1]
        self.busy.emit(True, f"criando '{title}'…")

        def work():
            return self._ensure_browser().create_child_page(parent.id, title)

        def done(node: PageNode):
            """Adopt the new page as the target and reflect it on screen.

            Assignment order matters: `_emit_page_tree` builds its payload
            with `selected_id = self._parent_page.id`, and that field is what
            marks the new page as chosen in the deepest dropdown. Emitting
            before the assignment listed the page but left it unselected —
            the label claimed one target while the dropdown showed another.

            The local children list is only patched when the user is still
            looking at the level they created into. The cache was already
            updated write-through; this keeps the in-memory snapshot in step
            with it.
            """
            self.busy.emit(False, "")
            self._parent_page = node
            self._parent_page_path = parent_path
            if self._browse_path is parent_path:
                self._browse_children = [*self._browse_children, node]
                self._emit_page_tree()
            self._emit_parent_page_changed()

        def error(msg: str):
            self.busy.emit(False, "")
            self.failed.emit(f"não consegui criar '{title}': {msg}")

        run_async(work, done, error)

    @Slot(str)
    def selectParentPage(self, page_id: str) -> None:
        """Validates that `page_id` is in {current node} ∪ {children of the
        current level} — an arbitrary id coming from JS becomes a visible
        error, not a silent destination (same caution as `browseInto`)."""
        node = self._browse_path[-1]
        candidates = {node.id: node, **{c.id: c for c in self._browse_children}}
        match = candidates.get(page_id)
        if match is None:
            self.failed.emit("essa página não está visível no seletor atual.")
            return

        if match.id == self._root_node.id:
            self._parent_page = None
            self._parent_page_path = []
        else:
            path = self._path_to(match)
            self._parent_page = match
            self._parent_page_path = path[:-1]
        self._emit_parent_page_changed()

    @Slot()
    def clearParentPage(self) -> None:
        self._parent_page = None
        self._parent_page_path = []
        self._emit_parent_page_changed()

    @Slot(str, str, result=str)
    def renderPreview(self, draft_json: str, kind: str) -> str:
        """Renders with the same parser as the compiler — see `md_to_html`.

        **Does NOT validate with `NoteDraft`.** This is `@Slot(result=str)`:
        an exception here turns into `""` and the preview disappears
        forever, with no error message at all (risk #5). The JS sends
        `currentDraft()` on every keystroke, including with an empty
        `title` while the user is still typing — something `NoteDraft`
        would reject. That's why the parsing is tolerant (`json.loads` +
        `.get(field, "")`) and the `except` is broad, returning at least the
        rendered body instead of nothing.
        """
        try:
            data = json.loads(draft_json)
            if not isinstance(data, dict):
                data = {}
        except (TypeError, ValueError):
            data = {}

        html = md_to_html(str(data.get("body_md", "") or ""))

        try:
            if _kind_or_default(kind) is DestinationKind.PAGE:
                html = _partial_metadata_html(data) + html
        except Exception:  # noqa: BLE001 - metadados não podem apagar o preview
            pass

        return html

    @Slot(str, str)
    def formatNote(self, raw: str, hint: str) -> None:
        """Run the raw note through the selected model and emit the draft.

        The destination snapshot taken here is only a best guess, recorded so
        a note that never reaches publish still has somewhere to go on retry.
        The destination actually written to travels as an argument of
        `publish`.

        It is captured before `run_async` deliberately: if the user changes
        the dropdown while inference runs, the stored guess stays the one that
        was on screen when they pressed Format, not whatever it became by the
        time the worker got scheduled.
        """
        raw = raw.strip()
        if not raw:
            self.failed.emit("Escreva alguma coisa antes de formatar.")
            return

        provider_id = self._provider_id
        if not provider_id:
            self.failed.emit(
                "Nenhum provedor disponível — configure `GEMINI_API_KEY` no .env e reabra o app."
            )
            return

        destination = self._current_choice()

        self.busy.emit(True, f"formatando com {provider_id}…")

        def work():
            """Persist the raw text, then call the model, then save the draft.

            The capture happens before inference so that a crash mid-inference
            costs the user the wait, never the note itself.
            """
            note_id = self.store.capture(raw, hint.strip() or None, destination)
            provider = build_provider(provider_id, self.settings)
            draft = provider.format_note(raw, FormatContext(hint=hint.strip() or None))
            self.store.save_draft(note_id, draft, provider.id)
            return note_id, draft

        def done(result):
            note_id, draft = result
            self._note_id = note_id
            self.busy.emit(False, "")
            self.draftReady.emit(draft.model_dump_json())
            self._emit_history()

        def error(msg: str):
            self.busy.emit(False, "")
            self.failed.emit(msg)
            self._emit_history()

        run_async(work, done, error)

    def _current_choice(self) -> DestinationChoice:
        """The choice reflected on screen RIGHT NOW: `self._destination_kind`
        and, if PAGE, `self._parent_page` (`None` = root). Only for
        `formatNote`'s estimate — `publish` NEVER reads this, the value
        actually published travels as an argument (risk #7)."""
        if self._destination_kind is DestinationKind.DATABASE:
            return DestinationChoice.database()
        page_id = self._parent_page.id if self._parent_page else None
        return DestinationChoice.page(page_id)

    def _make_pipeline(self) -> Pipeline:
        """`Pipeline` para `publish`/`retryPending` — as duas construções
        eram idênticas byte-a-byte (B6). `writer=self._ensure_writer()`
        reaproveita o adapter da sessão em vez de construir um novo (Fase 6,
        passo 14) — sob `NOTION_WRITER=mcp`, um `NotionMcpWriter` novo a cada
        publish/retry sobe um processo `npx` novo, vivo até o `atexit`
        (risco nº 14).
        """
        return Pipeline.from_settings(
            self.settings, self._provider_id, store=self.store, writer=self._ensure_writer()
        )

    @Slot(str, str, str)
    def publish(self, draft_json: str, kind: str, parent_page_id: str) -> None:
        """`kind` and `parent_page_id` travel as ARGUMENTS, never read back
        from `self._destination_kind`/`self._parent_page`: they are the
        values the screen showed at the moment of the click — the `<select>`
        and the chosen target may have changed since `formatNote` ran
        (risk #7), and the displayed "Página: X" label needs to be literally
        what gets sent (risk #11).

        `parent_page_id == ""` becomes root HERE, not inside
        `DestinationChoice` (which rejects an empty string — risk #2).
        QWebChannel only ever delivers `str` to `@Slot`s, never `None`, so ""
        is how JS signals "no page chosen"; the translation to `None`
        happens at this SINGLE, documented boundary — it is not the same
        coalescing as `NoteRecord.from_row` (that one is about NULL vs ''
        coming from SQLite).
        """
        try:
            draft = NoteDraft.model_validate_json(draft_json)
        except ValueError as exc:
            self.failed.emit(f"Rascunho inválido: {exc}")
            return

        destination_kind = _kind_or_default(kind)
        if destination_kind is DestinationKind.PAGE:
            page_id = parent_page_id.strip() or None
            destination = DestinationChoice.page(page_id)
        else:
            destination = DestinationChoice.database()

        note_id = self._note_id
        self.busy.emit(True, "publicando no Notion…")

        def work():
            return self._make_pipeline().publish(draft, note_id=note_id, destination=destination)

        def done(ref):
            """Announce the published page and drop the stale cached level.

            The note just became a child page of the target, so the cached
            listing for that node is now one entry short. Without the
            invalidation the picker would keep showing the pre-publish view
            until the TTL expired.
            """
            self.busy.emit(False, "")
            if destination.kind is DestinationKind.PAGE and destination.page_id is not None:
                if self._browser is not None:
                    self._browser.invalidate(destination.page_id)
            self.published.emit(json.dumps({"title": draft.title, "url": ref.url}))
            self._emit_history()

        def error(msg: str):
            self.busy.emit(False, "")
            self.failed.emit(
                f"{msg}\n\nO rascunho está salvo — use Histórico › Reenviar pendentes."
            )
            self._emit_history()

        run_async(work, done, error)

    @Slot()
    def loadHistory(self) -> None:
        """Refreshes the history drawer.

        Errors here go to the UI, not to a `lambda: None`: a refresh that
        fails silently leaves the drawer showing stale data indefinitely,
        which is worse than an error message (bug 10). `_emit_history` is
        the same body underneath (B5) — two names because only this one is
        `@Slot`, the contract called by JS.
        """
        run_async(self._history_payload, self.historyReady.emit, self.failed.emit)

    def _emit_history(self) -> None:
        self.loadHistory()

    def _history_payload(self) -> str:
        records = self.store.recent(30)
        return json.dumps({
            "notes": [
                {
                    "id": r.id,
                    "title": r.title,
                    "status": r.status,
                    "url": r.notion_url or "",
                    "error": _first_line(r.error),
                    "when": r.created_at[:16].replace("T", " "),
                    "provider": r.provider or "",
                }
                for r in records
            ],
            "pending": len(self.store.pending()),
        })

    @Slot()
    def retryPending(self) -> None:
        pending = self.store.pending()
        if not pending:
            self.failed.emit("Nada pendente.")
            return

        self.busy.emit(True, f"reenviando {len(pending)} nota(s)…")

        def work():
            return self._make_pipeline().retry_pending()

        def done(results):
            self.busy.emit(False, "")
            failures = [r for r in results if r[2]]
            if failures:
                self.failed.emit(
                    f"{len(results) - len(failures)} reenviada(s), "
                    f"{len(failures)} ainda com erro: {failures[0][2].splitlines()[0]}"
                )
            else:
                self.published.emit(json.dumps({
                    "title": f"{len(results)} nota(s) reenviada(s)",
                    "url": results[0][1].url if results else "",
                }))
            self._emit_history()

        def error(msg: str):
            self.busy.emit(False, "")
            self.failed.emit(msg)

        run_async(work, done, error)
