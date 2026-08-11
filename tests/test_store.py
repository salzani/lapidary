"""SQLite queue: the point is that a write failure costs a retry, not the
note.

Covers the note lifecycle (capture -> draft -> publish/failed), retrying
pending notes, reading recent/pending notes, the `destination_kind` column
migration for pre-existing databases, and `destination_page_id`
round-tripping (Phase 6).
"""

import sqlite3

import pytest

from notemcp.config import Settings
from notemcp.models import Destination, DestinationChoice, DestinationKind, NoteDraft, PageRef
from notemcp.notion.base import NotionWriteError
from notemcp.pipeline import Pipeline
from notemcp.store.db import SCHEMA, NoteStore

DRAFT = NoteDraft(
    title="Cache de embeddings", doc_type="ideia", tags=["cache"],
    summary="Guardar em disco.", body_md="## Motivação\n\n- não recalcular",
)


@pytest.fixture
def store(tmp_path):
    return NoteStore(tmp_path / "notes.db")


class FakeProvider:
    id, label = "fake:test", "Fake"

    def __init__(self):
        self.calls = 0

    def format_note(self, raw, ctx):
        self.calls += 1
        return DRAFT


class FlakyWriter:
    """Fails on the first `fail_times` writes, then works."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.attempts = 0
        self.ensured = []

    def ensure_destination(self, parent_page_id, kind):
        self.ensured.append((parent_page_id, kind))
        return Destination(kind=kind, id="ds-1")

    def create_note(self, destination, draft):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise NotionWriteError("connection reset by peer")
        return PageRef(id="page-1", url="https://notion.so/page-1")


def make_pipeline(store, writer=None, provider=None):
    return Pipeline(
        provider=provider or FakeProvider(),
        writer=writer or FlakyWriter(),
        settings=Settings(notion_token="t", notion_parent_page_id="p"),
        store=store,
    )


def test_successful_run_ends_published(store):
    pipeline = make_pipeline(store)
    pipeline.run("anotação crua")

    record = store.recent()[0]
    assert record.status == "published"
    assert record.notion_url == "https://notion.so/page-1"
    assert record.raw_text == "anotação crua"
    assert record.provider == "fake:test"
    assert store.pending() == []


def test_raw_text_is_saved_before_the_model_is_called(store):
    """If the model hangs or the process dies midway, the note is already
    saved."""
    class Hangs:
        id, label = "fake:hang", "Hang"

        def format_note(self, raw, ctx):
            raise RuntimeError("modelo caiu")

    pipeline = make_pipeline(store, provider=Hangs())
    with pytest.raises(RuntimeError):
        pipeline.run("anotação que não pode sumir")

    record = store.recent()[0]
    assert record.status == "captured"
    assert record.raw_text == "anotação que não pode sumir"


def test_write_failure_keeps_the_draft_and_records_the_error(store):
    writer = FlakyWriter(fail_times=1)
    pipeline = make_pipeline(store, writer=writer)

    with pytest.raises(NotionWriteError):
        pipeline.run("nota")

    record = store.recent()[0]
    assert record.status == "failed"
    assert "connection reset" in record.error
    assert record.draft.title == DRAFT.title, "o rascunho não pode se perder"
    assert [r.id for r in store.pending()] == [record.id]


def test_retry_publishes_without_calling_the_model_again(store):
    """Re-inferring would cost 40s on local Ollama — the draft is already
    ready."""
    provider = FakeProvider()
    writer = FlakyWriter(fail_times=1)
    pipeline = make_pipeline(store, writer=writer, provider=provider)

    with pytest.raises(NotionWriteError):
        pipeline.run("nota")
    assert provider.calls == 1

    results = pipeline.retry_pending()

    assert len(results) == 1
    record, ref, error = results[0]
    assert error is None and ref.url == "https://notion.so/page-1"
    assert provider.calls == 1, "o modelo NÃO pode ser chamado de novo"
    assert store.get(record.id).status == "published"
    assert store.pending() == []


def test_retry_clears_the_previous_error(store):
    pipeline = make_pipeline(store, writer=FlakyWriter(fail_times=1))
    with pytest.raises(NotionWriteError):
        pipeline.run("nota")

    pipeline.retry_pending()
    assert store.recent()[0].error is None


def test_one_failure_does_not_stop_the_queue(store):
    """A single problematic note must not block the others."""
    class SecondFails:
        def ensure_destination(self, parent_page_id, kind):
            return Destination(kind=kind, id="ds-1")

        def create_note(self, destination, draft):
            if draft.title == "ruim":
                raise NotionWriteError("propriedade inválida")
            return PageRef(id="p", url="https://notion.so/p")

    for title in ("boa 1", "ruim", "boa 2"):
        nid = store.capture(f"cru {title}")
        store.save_draft(nid, DRAFT.model_copy(update={"title": title}), "fake:test")

    pipeline = make_pipeline(store, writer=SecondFails())
    results = pipeline.retry_pending()

    assert len(results) == 3
    assert [r[2] is None for r in results] == [True, False, True]
    assert len(store.pending()) == 1, "só a problemática segue pendente"


def test_captured_notes_are_not_retried(store):
    """Without a draft, retry would have to call the model — a very
    different operation."""
    store.capture("só o texto cru, sem rascunho")

    pipeline = make_pipeline(store)
    assert pipeline.retry_pending() == []
    assert store.recent()[0].status == "captured"


def test_retry_reuses_the_persisted_destination(store):
    """Retry uses the destination persisted from the original attempt, not
    the default."""
    writer = FlakyWriter(fail_times=1)
    pipeline = make_pipeline(store, writer=writer)

    with pytest.raises(NotionWriteError):
        pipeline.run("nota", destination=DestinationChoice.page())

    writer.ensured.clear()
    results = pipeline.retry_pending()

    assert len(results) == 1 and results[0][2] is None
    assert writer.ensured == [("p", DestinationKind.PAGE)]


def test_retry_override_repoints_every_pending_note_to_a_new_destination(store):
    """`override` repoints ALL pending notes, ignoring the persisted
    destination — the way out of risk #15 when the original parent page
    disappeared from Notion (cli.py `--retry --parent-id`). Zero coverage
    before this test (D6)."""
    writer = FlakyWriter(fail_times=1)
    pipeline = make_pipeline(store, writer=writer)

    with pytest.raises(NotionWriteError):
        pipeline.run("nota", destination=DestinationChoice.page("pagina-morta"))

    writer.ensured.clear()
    results = pipeline.retry_pending(override=DestinationChoice.page("pagina-nova"))

    assert len(results) == 1 and results[0][2] is None
    assert writer.ensured == [("pagina-nova", DestinationKind.PAGE)]
    assert store.get(results[0][0].id).destination_page_id == "pagina-nova", (
        "override fica persistido (publish() grava antes de escrever), não é usado só uma vez"
    )


def test_retry_of_a_legacy_row_goes_to_the_database(store):
    """A row predating this feature (`destination_kind` NULL) goes to the
    long-standing default destination — the database."""
    nid = store.capture("cru")
    store.save_draft(nid, DRAFT, "fake:test")
    assert store.get(nid).destination_kind == DestinationKind.DATABASE

    writer = FlakyWriter()
    pipeline = make_pipeline(store, writer=writer)
    results = pipeline.retry_pending()

    assert len(results) == 1 and results[0][2] is None
    assert writer.ensured == [("p", DestinationKind.DATABASE)]


def test_retry_of_a_page_with_a_concrete_parent_targets_that_page(store):
    """Retrying a note whose persisted destination is a concrete parent
    page (Phase 6) calls `ensure_destination` with THAT PAGE'S ID, not with
    the root (`NOTION_PARENT_PAGE_ID`)."""
    writer = FlakyWriter(fail_times=1)
    pipeline = make_pipeline(store, writer=writer)

    with pytest.raises(NotionWriteError):
        pipeline.run("nota", destination=DestinationChoice.page("materia-3b34"))

    writer.ensured.clear()
    results = pipeline.retry_pending()

    assert len(results) == 1 and results[0][2] is None
    assert writer.ensured == [("materia-3b34", DestinationKind.PAGE)]


def test_recent_is_newest_first(store):
    for i in range(3):
        store.capture(f"nota {i}")
    assert [r.raw_text for r in store.recent()] == ["nota 2", "nota 1", "nota 0"]


def test_title_falls_back_to_first_line_when_there_is_no_draft(store):
    nid = store.capture("primeira linha aqui\nsegunda linha")
    assert store.get(nid).title == "primeira linha aqui"


def test_store_survives_reopening(tmp_path):
    path = tmp_path / "notes.db"
    nid = NoteStore(path).capture("persistida")
    assert NoteStore(path).get(nid).raw_text == "persistida"


def _make_legacy_db(path):
    """Database in the pre-feature schema: without the `destination_kind`
    column."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO notes (raw_text, status, created_at, updated_at) "
        "VALUES (?, 'captured', ?, ?)",
        ("nota antiga", "2024-01-01T00:00:00", "2024-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()


def test_legacy_database_gains_the_column(tmp_path):
    """Literal SQL for the old schema — opening it with `NoteStore` must
    migrate it without dropping the rows that already existed. Opening the
    store already applies the migration."""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    NoteStore(path)

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    conn.close()
    assert "destination_kind" in columns
    assert "destination_page_id" in columns, "Fase 6: coluna nova também é migrada"


def test_legacy_rows_default_to_database(tmp_path):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)

    record = NoteStore(path).recent()[0]
    assert record.raw_text == "nota antiga"
    assert record.destination_kind == DestinationKind.DATABASE


def test_migration_is_idempotent(tmp_path):
    """The second open must not raise."""
    path = tmp_path / "notes.db"
    NoteStore(path)
    NoteStore(path)


def test_unknown_destination_kind_in_db_falls_back_to_database(tmp_path):
    """A database hand-corrupted, or from a future version not yet
    supported — must not raise `ValueError` when read."""
    path = tmp_path / "notes.db"
    store = NoteStore(path)
    nid = store.capture("cru")

    conn = sqlite3.connect(path)
    conn.execute("UPDATE notes SET destination_kind = 'xpto' WHERE id = ?", (nid,))
    conn.commit()
    conn.close()

    assert store.get(nid).destination_kind == DestinationKind.DATABASE


def test_destination_survives_reopening(tmp_path):
    path = tmp_path / "notes.db"
    nid = NoteStore(path).capture("cru", destination=DestinationChoice.page())
    assert NoteStore(path).get(nid).destination_kind == DestinationKind.PAGE


def test_capture_without_destination_kind_defaults_to_database(store):
    nid = store.capture("cru")
    assert store.get(nid).destination_kind == DestinationKind.DATABASE


def test_set_destination_updates_an_existing_row(store):
    nid = store.capture("cru", destination=DestinationChoice.database())
    store.set_destination(nid, DestinationChoice.page())
    assert store.get(nid).destination_kind == DestinationKind.PAGE


def test_page_id_roundtrips_through_capture(store):
    nid = store.capture("cru", destination=DestinationChoice.page("3b34-abc"))
    record = store.get(nid)
    assert record.destination_kind == DestinationKind.PAGE
    assert record.destination_page_id == "3b34-abc"
    assert record.destination_choice == DestinationChoice.page("3b34-abc")


def test_page_id_roundtrips_through_set_destination(store):
    nid = store.capture("cru")
    store.set_destination(nid, DestinationChoice.page("3b34-abc"))
    record = store.get(nid)
    assert record.destination_page_id == "3b34-abc"


def test_set_destination_to_database_clears_the_parent_page():
    """Switching from `page` (with an id) to `database` must not leave the
    old `destination_page_id` in the row — a future retry rereading that
    column would target the wrong page (risk #3 of Phase 6)."""
    store = NoteStore(":memory:")
    nid = store.capture("cru", destination=DestinationChoice.page("3b34-abc"))
    store.set_destination(nid, DestinationChoice.database())

    record = store.get(nid)
    assert record.destination_kind == DestinationKind.DATABASE
    assert record.destination_page_id is None


def test_inconsistent_database_row_with_a_page_id_reads_as_database(store):
    """A `database` row with `destination_page_id` set should never exist
    (only manual corruption) — reading it discards the id instead of
    propagating an impossible state."""
    nid = store.capture("cru", destination=DestinationChoice.page("3b34-abc"))

    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE notes SET destination_kind = 'database' WHERE id = ?", (nid,))
    conn.commit()
    conn.close()

    record = store.get(nid)
    assert record.destination_choice == DestinationChoice.database()
