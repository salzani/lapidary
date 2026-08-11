"""UI layer: provider registry, persisted state, preview rendering, worker,
and the `Bridge` (JS<->Python contract, selectable destination, and the
Phase 6 navigable parent-page tree).

Tests that depend on Qt are skipped if PySide6 is not installed — the UI is
an optional extra (`pip install 'notemcp[ui]'`).
"""

import json

import pytest

from notemcp.config import Settings
from notemcp.llm import registry
from notemcp.notion.compiler import md_to_blocks, md_to_html
from notemcp.ui import state


def test_gemini_without_key_is_listed_but_unavailable():
    got = registry.discover(Settings(gemini_api_key=""))

    assert len(got) == len(registry.GEMINI_MODELS)
    assert all(p.backend == "gemini" for p in got)
    assert not any(p.available for p in got)
    assert all("GEMINI_API_KEY" in p.reason for p in got)


def test_gemini_with_key_lists_every_model_as_available():
    got = registry.discover(Settings(gemini_api_key="a-key"))

    assert len(got) == len(registry.GEMINI_MODELS)
    assert all(p.available for p in got)
    assert all(not p.reason for p in got)


def test_discover_never_returns_an_empty_list():
    """Gemini is the only provider (Ollama was removed): an empty list here
    is indistinguishable, at the type level, from "nothing configured yet"
    — `discover()` must always return the curated models, available or
    not."""
    assert registry.discover(Settings()) != []


def test_discover_returns_exactly_the_curated_gemini_models():
    got = registry.discover(Settings(gemini_api_key="a-key"))
    assert len(got) == len(registry.GEMINI_MODELS)
    assert {p.id for p in got} == {f"gemini:{m}" for m in registry.GEMINI_MODELS}


def test_gemini_sdk_import_failure_becomes_an_unavailable_entry_not_a_crash(monkeypatch):
    """A non-`ImportError` exception underneath `import google.genai` (the
    OpenSSL/libcrypto bug catalogued in ESTADO.md §5) must not propagate out
    of `discover()` — it becomes a reason string carrying the exception's
    own type name, not a generic "not installed" that sends whoever reads
    it chasing the wrong fix (H1)."""
    import builtins

    real_import = builtins.__import__

    def boom_import(name, *args, **kwargs):
        if name == "google.genai" or name.startswith("google.genai"):
            raise OSError("libcrypto.so.3: version `OPENSSL_3.3.0' not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom_import)

    got = registry.discover(Settings(gemini_api_key="a-key"))

    assert len(got) == len(registry.GEMINI_MODELS)
    assert not any(p.available for p in got)
    assert all("OSError" in p.reason for p in got)


def test_state_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "cfg")
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "cfg" / "state.json")

    assert state.load() == {}
    state.save(provider="gemini:gemini-3.5-flash-lite")
    assert state.load()["provider"] == "gemini:gemini-3.5-flash-lite"

    state.save(theme="light")
    assert state.load() == {"provider": "gemini:gemini-3.5-flash-lite", "theme": "light"}


def test_corrupt_state_file_does_not_crash(monkeypatch, tmp_path):
    f = tmp_path / "state.json"
    f.write_text("{isso não é json")
    monkeypatch.setattr(state, "STATE_FILE", f)
    assert state.load() == {}


def test_unwritable_state_dir_does_not_crash(monkeypatch, tmp_path):
    """A preference that fails to save must not bring down the app. The
    call below must not raise."""
    blocker = tmp_path / "arquivo"
    blocker.write_text("sou um arquivo, não um diretório")
    monkeypatch.setattr(state, "STATE_DIR", blocker / "cfg")
    monkeypatch.setattr(state, "STATE_FILE", blocker / "cfg" / "state.json")
    state.save(provider="x")


PARENT_PAGE = {
    "id": "3b34",
    "title": "Cálculo I",
    "path": [{"id": "root", "title": "Página raiz"}, {"id": "fac", "title": "Faculdade"}],
}


def test_parent_page_roundtrips(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "cfg")
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "cfg" / "state.json")

    state.save_parent_page(PARENT_PAGE)
    parent_page, malformed = state.load_parent_page()
    assert parent_page == PARENT_PAGE
    assert malformed is False


def test_absent_parent_page_is_none_without_a_warning(monkeypatch, tmp_path):
    """Normal case: never chosen — silently defaults to the root, no
    signal raised."""
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "cfg")
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "cfg" / "state.json")

    parent_page, malformed = state.load_parent_page()
    assert parent_page is None
    assert malformed is False


def test_cleared_parent_page_is_indistinguishable_from_never_chosen(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "cfg")
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "cfg" / "state.json")

    state.save_parent_page(PARENT_PAGE)
    state.save_parent_page(None)

    parent_page, malformed = state.load_parent_page()
    assert parent_page is None
    assert malformed is False


@pytest.mark.parametrize("raw", [
    "não é um dict",
    {},
    {"id": "", "title": "T", "path": []},
    {"id": "x", "title": 42, "path": []},
    {"id": "x", "title": "T", "path": "não é uma lista"},
    {"id": "x", "title": "T", "path": [{"id": "y"}]},
])
def test_malformed_parent_page_falls_back_to_the_root_and_signals_it(monkeypatch, tmp_path, raw):
    """Present but invalid must not silently fall back to the root (risk
    #7) — distinct from the 'never chosen' case."""
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "cfg")
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "cfg" / "state.json")

    state.save(parent_page=raw)
    parent_page, malformed = state.load_parent_page()
    assert parent_page is None
    assert malformed is True


def test_preview_uses_the_same_parser_as_the_compiler():
    """If the preview accepted syntax that the compiler discards, it would
    lie about what actually reaches Notion. `linkify` is disabled in the
    compiler: a bare URL stays plain text on both sides."""
    md = "# t\n\n- a\n- b\n\n```python\nx=1\n```"
    html = md_to_html(md)
    assert "<h1>" in html and "<ul>" in html and "<code" in html

    assert "<a href" not in md_to_html("veja em https://exemplo.com ok")
    blocks = md_to_blocks("veja em https://exemplo.com ok")
    assert not any(r["text"].get("link") for r in blocks[0]["paragraph"]["rich_text"])


def test_preview_of_empty_markdown_is_empty():
    assert md_to_html("").strip() == ""


def test_run_async_keeps_task_alive_until_done():
    """Without a strong reference, the GC collects the QRunnable/
    _TaskSignals and the signal never arrives — a silent failure: the UI
    spins forever."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.ui import worker

    app = QCoreApplication.instance() or QCoreApplication([])
    got = []

    worker.run_async(lambda: "resultado", got.append, got.append)
    assert len(worker._active) == 1, "task precisa estar retida enquanto roda"

    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert got == ["resultado"]
    assert not worker._active, "task precisa ser liberada ao terminar"


def test_run_async_reports_exception_as_error():
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.ui import worker

    app = QCoreApplication.instance() or QCoreApplication([])
    errors = []

    def boom():
        raise RuntimeError("modelo fora do ar")

    worker.run_async(boom, lambda _: None, errors.append)
    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert errors == ["modelo fora do ar"]
    assert not worker._active


def test_history_payload_survives_a_cleared_error():
    """`"".splitlines()` is `[]`, not `[""]`.

    Indexing directly raised IndexError exactly when a note was published
    successfully and the previous error was cleared — and since the
    refresh swallowed exceptions, the drawer froze showing stale state,
    with no sign of the problem.

    `Bridge.__new__(Bridge)` below skips `QObject.__init__` — only the
    payload built from `store` matters for this test.
    """
    pytest.importorskip("PySide6")
    from notemcp.models import NoteDraft, PageRef
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge, _first_line

    assert _first_line(None) == ""
    assert _first_line("") == ""
    assert _first_line("   ") == "   ", "espaço em branco não é None nem vazio — não deve estourar"
    assert _first_line("linha 1\nlinha 2") == "linha 1"
    assert _first_line("linha 1\nlinha 2\nlinha 3") == "linha 1", "só a primeira linha, o resto é descartado"
    assert _first_line("x" * 200) == "x" * 160, "trunca no limite mesmo sem quebra de linha"
    assert _first_line("y" * 10, limit=5) == "y" * 5, "limit é respeitado quando explícito"

    store = NoteStore(":memory:")
    nid = store.capture("cru")
    store.save_draft(nid, NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ), "fake:1")
    store.mark_failed(nid, "connection reset\nsegunda linha")

    bridge = Bridge.__new__(Bridge)
    bridge.store = store

    payload = json.loads(bridge._history_payload())
    assert payload["pending"] == 1
    assert payload["notes"][0]["error"] == "connection reset"

    store.mark_published(nid, PageRef(id="p", url="https://notion.so/p"))
    payload = json.loads(bridge._history_payload())
    assert payload["pending"] == 0
    assert payload["notes"][0]["error"] == ""
    assert payload["notes"][0]["status"] == "published"


def test_history_payload_survives_an_empty_string_error():
    """`error=""` is distinct from `error=None` in the column, but the
    payload must not differentiate — both must become `""` without
    raising `IndexError`. `mark_failed(nid, "")` below passes an explicit
    empty string, not `None`.
    """
    pytest.importorskip("PySide6")
    from notemcp.models import NoteDraft
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    store = NoteStore(":memory:")
    nid = store.capture("cru")
    store.save_draft(nid, NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ), "fake:1")
    store.mark_failed(nid, "")

    bridge = Bridge.__new__(Bridge)
    bridge.store = store

    payload = json.loads(bridge._history_payload())
    assert payload["notes"][0]["error"] == ""
    assert payload["notes"][0]["status"] == "failed"


def test_draft_json_from_ui_validates():
    """The exact format that `app.js:currentDraft()` builds."""
    from notemcp.models import NoteDraft

    payload = json.dumps({
        "title": "Cache de embeddings",
        "doc_type": "ideia",
        "tags": ["cache", "sqlite"],
        "summary": "Guardar embeddings em disco.",
        "body_md": "## Motivação\n\n- não recalcular a cada boot",
    })
    draft = NoteDraft.model_validate_json(payload)
    assert draft.tags == ["cache", "sqlite"]
    assert md_to_blocks(draft.body_md)[0]["type"] == "heading_2"


def test_render_preview_with_invalid_draft_does_not_raise():
    """`renderPreview` is `@Slot(result=str)`: an exception becomes `""`
    and the preview vanishes forever, with no message (risk #5). The JS
    sends `currentDraft()` on every keystroke, including with an empty
    `title` — something `NoteDraft` would reject.

    `renderPreview("[]", "page")` below must not raise even with malformed
    JSON, even though the metadata callout comes out with blank fields.

    The empty-`title` case simulates what the JS sends on every keystroke
    while the field is being cleared, which `NoteDraft` would reject — the
    preview must not disappear because of it.

    `Bridge.__new__(Bridge)` below skips `QObject.__init__`: `renderPreview`
    does not use `self`.
    """
    pytest.importorskip("PySide6")
    from notemcp.ui.bridge import Bridge

    bridge = Bridge.__new__(Bridge)

    assert bridge.renderPreview("isso não é json", "database") == ""
    assert bridge.renderPreview("null", "database") == ""
    assert bridge.renderPreview("[]", "database") == ""
    assert bridge.renderPreview("{}", "xpto") == ""
    bridge.renderPreview("[]", "page")

    partial = json.dumps({
        "title": "", "doc_type": "nota", "tags": [], "summary": "", "body_md": "# t",
    })
    assert "<h1>" in bridge.renderPreview(partial, "database")


def test_render_preview_includes_the_callout_only_for_page():
    """`renderPreview` receives the `kind` and prepends the callout's
    HTML — the preview must not show the body without the metadata that
    actually reaches Notion (risk #4)."""
    pytest.importorskip("PySide6")
    from notemcp.ui.bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    draft_json = json.dumps({
        "title": "T", "doc_type": "ideia", "tags": ["a"],
        "summary": "resumo do callout", "body_md": "corpo do documento",
    })

    database_html = bridge.renderPreview(draft_json, "database")
    page_html = bridge.renderPreview(draft_json, "page")

    assert "callout-meta" not in database_html
    assert "callout-meta" in page_html
    assert "resumo do callout" in page_html
    assert "corpo do documento" in page_html


def test_unknown_destination_in_state_falls_back_to_database(monkeypatch):
    """An invalid `kind` in `state.json` must not crash the boot (risk
    #6)."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from notemcp.models import DestinationKind
    from notemcp.store.db import NoteStore
    from notemcp.ui import state
    from notemcp.ui.bridge import Bridge

    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(state, "load", lambda: {"destination": "xpto"})

    bridge = Bridge(
        settings=Settings(notion_token="t", notion_parent_page_id="p"),
        store=NoteStore(":memory:"),
    )
    assert bridge._destination_kind == DestinationKind.DATABASE


def test_a_leftover_ollama_provider_in_state_json_is_silently_discarded(monkeypatch):
    """Asymmetric on purpose (R4): `LLM_PROVIDER` is a CHOICE and fails
    loud; `state.json`'s `provider` is a REMEMBERED PREFERENCE and a
    non-Gemini leftover (from before Ollama was removed, or hand-edited)
    is discarded silently, falling back to `settings.llm_provider` —
    never surfaced as an error for something the user didn't actively pick
    today."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from notemcp.store.db import NoteStore
    from notemcp.ui import state
    from notemcp.ui.bridge import Bridge

    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(state, "load", lambda: {"provider": "ollama:qwen3:8b"})

    settings = Settings(notion_token="t", llm_provider="gemini:gemini-3.5-flash-lite")
    bridge = Bridge(settings=settings, store=NoteStore(":memory:"))

    assert bridge._provider_id == "gemini:gemini-3.5-flash-lite"


def test_a_persisted_gemini_provider_is_kept(monkeypatch):
    """The other half of the same guard: a persisted value that DOES start
    with `gemini:` is a legitimate remembered preference and must survive,
    even when it differs from the current `.env` default."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from notemcp.store.db import NoteStore
    from notemcp.ui import state
    from notemcp.ui.bridge import Bridge

    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(state, "load", lambda: {"provider": "gemini:gemini-3.1-pro"})

    settings = Settings(notion_token="t", llm_provider="gemini:gemini-3.5-flash-lite")
    bridge = Bridge(settings=settings, store=NoteStore(":memory:"))

    assert bridge._provider_id == "gemini:gemini-3.1-pro"


def test_destinations_payload_reports_the_reason():
    """Same pattern as the provider dropdown: an unavailable destination
    stays in the list, disabled, with a reason — it does not disappear
    (risk #10). `Settings` below intentionally has no
    `notion_parent_page_id`, so "page" is expected to be unavailable."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    QCoreApplication.instance() or QCoreApplication([])

    bridge = Bridge(
        settings=Settings(notion_token="t"),
        store=NoteStore(":memory:"),
    )
    payloads = []
    bridge.destinationsReady.connect(payloads.append)
    bridge._emit_destinations()

    payload = json.loads(payloads[0])
    by_kind = {d["kind"]: d for d in payload["destinations"]}
    assert by_kind["database"]["available"] is True
    assert by_kind["page"]["available"] is False
    assert "NOTION_PARENT_PAGE_ID" in by_kind["page"]["reason"]
    assert payload["selected"] == "database"


@pytest.fixture
def fake_pipeline(monkeypatch):
    """`FakePipeline` that only records the `destination` received by
    `publish` — a mechanical consolidation (B9) of the 5 byte-for-byte
    copies that used to exist in the `publish` tests below. The tree fakes
    (`FakePageBrowser`) intentionally have a different shape and are not
    part of this consolidation."""
    pytest.importorskip("PySide6")
    from notemcp.models import PageRef
    from notemcp.ui import bridge as bridge_mod

    captured = {}

    class FakePipeline:
        def __init__(self, *a, **k):
            pass

        def publish(self, draft, note_id=None, destination=None):
            captured["destination"] = destination
            return PageRef(id="p", url="https://notion.so/p")

    monkeypatch.setattr(bridge_mod.Pipeline, "from_settings", lambda *a, **k: FakePipeline())
    return captured


def test_publish_uses_the_kind_from_the_call_not_the_bridge_state(fake_pipeline):
    """The destination travels as an ARGUMENT of `publish`, not through the
    bridge's internal state — the `<select>` may have changed since
    `formatNote` (risk #7). Below, the bridge's state says "database", but
    the simulated click sends "page"."""
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.models import DestinationChoice, DestinationKind, NoteDraft
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    app = QCoreApplication.instance() or QCoreApplication([])

    bridge = Bridge(settings=Settings(notion_token="t"), store=NoteStore(":memory:"))
    bridge._destination_kind = DestinationKind.DATABASE

    draft_json = NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ).model_dump_json()
    bridge.publish(draft_json, "page", "")

    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert fake_pipeline["destination"] == DestinationChoice.page()


def test_publish_uses_the_parent_page_from_the_call_not_the_bridge_state(fake_pipeline):
    """Same risk #7, but for the page id: `self._parent_page` may point to
    one place while the click points to another (the user navigated the
    selector without confirming, or clicked too fast). Below, the bridge's
    state points to "outra-pagina", but the simulated click sends
    "materia-3b34"."""
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.models import DestinationChoice, NoteDraft
    from notemcp.notion.browser import PageNode
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    app = QCoreApplication.instance() or QCoreApplication([])

    bridge = Bridge(
        settings=Settings(notion_token="t", notion_parent_page_id="root"),
        store=NoteStore(":memory:"),
    )
    bridge._parent_page = PageNode("outra-pagina", "Outra")

    draft_json = NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ).model_dump_json()
    bridge.publish(draft_json, "page", "materia-3b34")

    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert fake_pipeline["destination"] == DestinationChoice.page("materia-3b34")


def test_publish_discards_the_parent_page_id_for_database(fake_pipeline):
    """`publish(json,"database","<id>")` becomes `.database()` — a parent
    page id makes no sense for the database destination and must be
    ignored, not propagated."""
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.models import DestinationChoice, NoteDraft
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    app = QCoreApplication.instance() or QCoreApplication([])

    bridge = Bridge(settings=Settings(notion_token="t"), store=NoteStore(":memory:"))
    draft_json = NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ).model_dump_json()
    bridge.publish(draft_json, "database", "materia-3b34")

    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert fake_pipeline["destination"] == DestinationChoice.database()


def test_empty_parent_page_id_from_publish_falls_back_to_the_root(fake_pipeline):
    """`parent_page_id == ""` coming from the JS becomes root — the
    translation happens inside `publish`, before building
    `DestinationChoice` (which rejects `""` — risk #2). QWebChannel only
    ever delivers `str`, never `None`."""
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.models import DestinationChoice, NoteDraft
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    app = QCoreApplication.instance() or QCoreApplication([])

    bridge = Bridge(settings=Settings(notion_token="t"), store=NoteStore(":memory:"))
    draft_json = NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ).model_dump_json()
    bridge.publish(draft_json, "page", "")

    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert fake_pipeline["destination"] == DestinationChoice.page(None)
    assert fake_pipeline["destination"].is_root_page


def test_successful_page_publish_invalidates_the_cache_of_the_destination(fake_pipeline):
    """The note became a `child_page` of that node — the cache needs to
    forget the old listing so the tree shows the new note on the next
    open. The line below seeds the cache to simulate a level that was
    already cached before publishing."""
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.models import NoteDraft
    from notemcp.notion.tree_cache import CachedBrowser
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    app = QCoreApplication.instance() or QCoreApplication([])

    bridge = Bridge(settings=Settings(notion_token="t"), store=NoteStore(":memory:"))
    bridge._browser = CachedBrowser(FakePageBrowser())
    bridge._browser._entries["materia-3b34"] = (0.0, [])

    draft_json = NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ).model_dump_json()
    bridge.publish(draft_json, "page", "materia-3b34")

    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert not bridge._browser.is_cached("materia-3b34")


class FakePageBrowser:
    """Fixed tree: root -> Faculdade -> Cálculo I -> Prova 2."""

    def __init__(self):
        self.list_calls = []
        self.created = []
        self.tree = {
            "root": [("faculdade", "Faculdade")],
            "faculdade": [("calc-1", "Cálculo I")],
            "calc-1": [("prova-2", "Prova 2")],
            "prova-2": [],
        }

    def list_child_pages(self, page_id):
        from notemcp.notion.browser import PageNode

        self.list_calls.append(page_id)
        return [PageNode(i, t) for i, t in self.tree.get(page_id, [])]

    def create_child_page(self, parent_page_id, title):
        from notemcp.notion.browser import PageNode

        self.created.append((parent_page_id, title))
        node = PageNode(f"new-{len(self.created)}", title)
        self.tree.setdefault(parent_page_id, []).append((node.id, node.title))
        self.tree.setdefault(node.id, [])
        return node


def _make_browsable_bridge(monkeypatch, fake=None):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from notemcp.store.db import NoteStore
    from notemcp.ui import bridge as bridge_mod
    from notemcp.ui.bridge import Bridge

    QCoreApplication.instance() or QCoreApplication([])
    fake = fake or FakePageBrowser()
    monkeypatch.setattr(bridge_mod, "build_browser", lambda settings: fake)

    bridge = Bridge(
        settings=Settings(notion_token="t", notion_parent_page_id="root"),
        store=NoteStore(":memory:"),
    )
    return bridge, fake


def _run_sync(bridge_mod, monkeypatch):
    """Makes `run_async` run `work`/`on_done` immediately, without a
    thread — the navigation tests don't need QThreadPool for the happy
    path."""
    def fake_run_async(fn, on_done, on_error):
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            on_error(str(exc))
        else:
            on_done(result)

    monkeypatch.setattr(bridge_mod, "run_async", fake_run_async)


def test_open_page_tree_emits_a_breadcrumb_payload(monkeypatch):
    from notemcp.ui import bridge as bridge_mod

    bridge, fake = _make_browsable_bridge(monkeypatch)
    _run_sync(bridge_mod, monkeypatch)

    payloads = []
    bridge.pageTreeReady.connect(payloads.append)
    bridge.openPageTree()

    payload = json.loads(payloads[0])
    assert payload["node"] == {"id": "root", "title": "Página raiz"}
    assert payload["path"] == [{"id": "root", "title": "Página raiz"}]
    assert payload["children"] == [{"id": "faculdade", "title": "Faculdade", "ambiguous": False}]
    assert payload["can_go_up"] is False


def test_page_tree_below_the_root_reports_can_go_up(monkeypatch):
    """`can_go_up` (Part B, infrastructure for restoring state on frontend
    boot): must be `True` as soon as the cursor leaves the root — the JS
    uses this to distrust a response that didn't advance as expected."""
    from notemcp.ui import bridge as bridge_mod

    bridge, fake = _make_browsable_bridge(monkeypatch)
    _run_sync(bridge_mod, monkeypatch)

    payloads = []
    bridge.pageTreeReady.connect(payloads.append)
    bridge.openPageTree()
    bridge.browseInto("faculdade")

    assert json.loads(payloads[-1])["can_go_up"] is True


def test_parent_page_changed_payload_carries_the_full_ancestor_path(monkeypatch):
    """`path` (Part B): frontend-boot restoration rebuilds the whole chain
    from this, not just the target's id — without the full path, a parent
    page that is a GRANDCHILD of the root couldn't be rebuilt (risk #11:
    screen and target diverging on boot)."""
    from notemcp.ui import bridge as bridge_mod

    bridge, fake = _make_browsable_bridge(monkeypatch)
    _run_sync(bridge_mod, monkeypatch)
    bridge.openPageTree()
    bridge.browseInto("faculdade")

    payloads = []
    bridge.parentPageChanged.connect(payloads.append)
    bridge.selectParentPage("calc-1")

    payload = json.loads(payloads[-1])
    assert payload["id"] == "calc-1"
    assert payload["is_root"] is False
    assert [p["id"] for p in payload["path"]] == ["root", "faculdade"], (
        "path precisa ter a raiz E todos os ancestrais intermediários"
    )


def test_open_page_tree_without_a_configured_root_fails_instead_of_crashing(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge

    QCoreApplication.instance() or QCoreApplication([])
    bridge = Bridge(settings=Settings(notion_token="t"), store=NoteStore(":memory:"))

    errors = []
    bridge.failed.connect(errors.append)
    bridge.openPageTree()

    assert errors and "NOTION_PARENT_PAGE_ID" in errors[0]


def test_browse_into_an_id_outside_the_children_fails_visibly():
    """An arbitrary id from the JS must not silently navigate nowhere."""
    pytest.importorskip("PySide6")
    from notemcp.config import Settings as _S
    from notemcp.store.db import NoteStore
    from notemcp.ui.bridge import Bridge
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])
    bridge = Bridge(settings=_S(notion_token="t", notion_parent_page_id="root"), store=NoteStore(":memory:"))

    errors = []
    bridge.failed.connect(errors.append)
    bridge.browseInto("id-que-nao-existe")

    assert errors, "id fora dos filhos precisa virar erro, não navegação silenciosa"


def test_a_stale_browse_response_does_not_overwrite_the_current_level(monkeypatch):
    """R1: clicking a slow subject and then navigating back must not let
    the delayed response paint the wrong breadcrumb once it finally
    arrives.

    Sequence simulated below: starting from "Faculdade" (current path =
    [root, faculdade]), the user clicks "Cálculo I" (slow, not yet
    resolved) and then goes back — which acts on the path that is STILL
    COMMITTED (Faculdade), leading to the root, not to the halfway point
    of the in-flight navigation. `browseUp` was removed (Part B: the
    dropdown chain has no "back", only sequential forward navigation via
    breadcrumb) — `browseTo("root")` truncates the path the same way.

    Call #0 resolves root -> [Faculdade]; #1 resolves Faculdade ->
    [Cálculo I]; #2 (browseInto("calc-1"), Cálculo I -> [Prova 2]) is left
    pending ("slow"); #3 (browseTo("root")) is fast and resolves before
    #2.
    """
    from notemcp.ui import bridge as bridge_mod

    bridge, fake = _make_browsable_bridge(monkeypatch)

    pending = []

    def fake_run_async(fn, on_done, on_error):
        pending.append((fn, on_done, on_error))

    monkeypatch.setattr(bridge_mod, "run_async", fake_run_async)

    def resolve(index):
        fn, done, _ = pending[index]
        done(fn())

    bridge.openPageTree()
    resolve(0)
    bridge.browseInto("faculdade")
    resolve(1)
    bridge.browseInto("calc-1")
    bridge.browseTo("root")
    resolve(3)

    assert [p.id for p in bridge._browse_path] == ["root"]

    resolve(2)

    assert [p.id for p in bridge._browse_path] == ["root"], (
        "resposta obsoleta não pode sobrescrever o nível atual"
    )
    assert [c.id for c in bridge._browse_children] == ["faculdade"]


def test_reload_page_tree_invalidates_only_the_current_node(monkeypatch):
    """`openPageTree` caches "root"; `browseInto("faculdade")` caches
    "faculdade"."""
    from notemcp.ui import bridge as bridge_mod

    bridge, fake = _make_browsable_bridge(monkeypatch)
    _run_sync(bridge_mod, monkeypatch)

    bridge.openPageTree()
    bridge.browseInto("faculdade")
    assert bridge._browser.is_cached("root")
    assert bridge._browser.is_cached("faculdade")

    fake.list_calls.clear()
    bridge.reloadPageTree()

    assert bridge._browser.is_cached("root"), "ancestral não pode ser invalidado"
    assert fake.list_calls == ["faculdade"], "só o nó atual é refeito"


def test_check_child_title_finds_a_duplicate_by_slug_and_never_raises(monkeypatch):
    """`browseInto("faculdade")` loads its children ([Cálculo I]). The
    final assertion checks that an empty title never raises, even though
    it is a strange input."""
    from notemcp.ui import bridge as bridge_mod

    bridge, fake = _make_browsable_bridge(monkeypatch)
    _run_sync(bridge_mod, monkeypatch)
    bridge.openPageTree()
    bridge.browseInto("faculdade")

    result = json.loads(bridge.checkChildTitle("CALCULO i"))
    assert result["duplicate"] is True
    assert result["matches"][0]["id"] == "calc-1"

    result = json.loads(bridge.checkChildTitle("Química"))
    assert result["duplicate"] is False

    assert json.loads(bridge.checkChildTitle(""))["duplicate"] is False


def test_create_child_page_error_does_not_change_the_target(monkeypatch):
    from notemcp.ui import bridge as bridge_mod

    class BoomBrowser(FakePageBrowser):
        def create_child_page(self, parent_page_id, title):
            raise Exception("a conexão caiu")

    bridge, fake = _make_browsable_bridge(monkeypatch, fake=BoomBrowser())
    _run_sync(bridge_mod, monkeypatch)
    bridge.openPageTree()
    bridge.browseInto("faculdade")
    bridge.selectParentPage("calc-1")
    assert bridge._parent_page.id == "calc-1"

    errors = []
    bridge.failed.connect(errors.append)
    bridge.createChildPage("Cálculo II")

    assert errors
    assert bridge._parent_page.id == "calc-1", "erro na criação não pode mudar o alvo selecionado"


def test_create_child_page_emits_the_new_node_as_selected(monkeypatch):
    """Regression test for bug C2 (the half that can be tested from the
    Python side — the other half, `app.js` arming `requestPageTree` before
    calling `createChildPage`, is only verifiable by reading the source,
    see `tests/test_app_js_contract.py`).

    `_emit_page_tree()` must run AFTER `self._parent_page = node` (not
    before): this payload carries `selected_id`, which the deepest
    `<select>` uses to mark the newly created page as selected. With the
    order swapped, the new page appeared in the list but was never marked
    — the "Página: X" label said one thing and the `<select>` showed
    another (risk #11)."""
    from notemcp.ui import bridge as bridge_mod

    bridge, fake = _make_browsable_bridge(monkeypatch)
    _run_sync(bridge_mod, monkeypatch)
    bridge.openPageTree()
    bridge.browseInto("faculdade")

    tree_payloads = []
    bridge.pageTreeReady.connect(tree_payloads.append)
    bridge.createChildPage("Cálculo II")

    payload = json.loads(tree_payloads[-1])
    assert any(c["id"] == payload["selected_id"] for c in payload["children"]), (
        "selected_id precisa apontar para uma das opções da listagem recém-emitida"
    )
    assert payload["selected_id"] == bridge._parent_page.id == "new-1"


def test_two_consecutive_publishes_reuse_the_same_writer_instance(monkeypatch):
    """One UI adapter per session (Phase 6, step 14 — fixes risk #14).
    Each `publish`/`retry` building a new `NotionMcpWriter` spawns a new
    `npx` process, alive until `atexit` (pre-existing bug, risk #14). The
    bridge must hold ONE adapter per session."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QThreadPool

    from notemcp.models import NoteDraft, PageRef
    from notemcp.store.db import NoteStore
    from notemcp.ui import bridge as bridge_mod
    from notemcp.ui.bridge import Bridge

    app = QCoreApplication.instance() or QCoreApplication([])

    seen_writers = []

    class FakePipeline:
        def __init__(self, *a, writer=None, **k):
            seen_writers.append(writer)

        def publish(self, draft, note_id=None, destination=None):
            return PageRef(id="p", url="https://notion.so/p")

    monkeypatch.setattr(bridge_mod.Pipeline, "from_settings", FakePipeline)
    monkeypatch.setattr(bridge_mod, "build_browser", lambda settings: FakePageBrowser())

    bridge = Bridge(settings=Settings(notion_token="t"), store=NoteStore(":memory:"))
    draft_json = NoteDraft(
        title="T", doc_type="nota", tags=[], summary="s", body_md="corpo"
    ).model_dump_json()

    bridge.publish(draft_json, "database", "")
    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    bridge.publish(draft_json, "database", "")
    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()

    assert len(seen_writers) == 2
    assert seen_writers[0] is seen_writers[1], "as duas publicações têm que reusar o mesmo adapter"
    assert seen_writers[0] is bridge._writer


def test_browser_and_writer_are_the_same_shared_instance(monkeypatch):
    """The same concrete instance satisfies both `NotionBrowser` and
    `NotionWriter` — browsing must not instantiate a second transport."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    from notemcp.store.db import NoteStore
    from notemcp.ui import bridge as bridge_mod
    from notemcp.ui.bridge import Bridge

    QCoreApplication.instance() or QCoreApplication([])

    fake = FakePageBrowser()
    monkeypatch.setattr(bridge_mod, "build_browser", lambda settings: fake)

    bridge = Bridge(
        settings=Settings(notion_token="t", notion_parent_page_id="root"),
        store=NoteStore(":memory:"),
    )
    writer = bridge._ensure_writer()
    browser = bridge._ensure_browser()

    assert writer is fake
    assert browser._inner is fake


def test_from_settings_without_a_writer_still_builds_from_notion_writer(monkeypatch):
    """`Pipeline.from_settings` without `writer=` still builds from
    `NOTION_WRITER` — it must not require the new parameter at old call
    sites (CLI, existing tests)."""
    from notemcp.notion.api_writer import NotionApiWriter
    from notemcp.pipeline import Pipeline

    settings = Settings(notion_token="t", notion_parent_page_id="p", notion_writer="api")
    pipeline = Pipeline.from_settings(settings)
    assert isinstance(pipeline.writer, NotionApiWriter)
