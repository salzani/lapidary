"""Pipeline and writer, tested offline, with fakes on both ends.

Covers: the pipeline's format/publish split and destination resolution
(including the `NOTION_DATA_SOURCE_ID` shortcut guard, risk #2), provider
failure propagation (no silent fallback), the API writer's request
batching/property handling against the 2025-09-03 API shape, and transport
retry semantics for connection vs. protocol errors.

`test_unrelated_child_database_is_ignored` used to live in this file (right
before `test_existing_database_missing_properties_is_patched`) — B10:
it had the same coverage as the parametrized (api+mcp) version in
`test_notion_writers.py::test_unrelated_child_database_is_ignored`, so it
moved there; the extra assertion that only existed in this version
(`client.created_database is not None`, confirming a NEW database was
actually created) moved along with it.
"""

import httpx
import pytest

from notemcp.config import Settings
from notemcp.llm.base import LLMError, LLMProvider, generate_draft
from notemcp.models import Destination, DestinationChoice, DestinationKind, FormatContext, NoteDraft, PageRef
from notemcp.notion.api_writer import MAX_CHILDREN_PER_REQUEST, NotionApiWriter, chunk_children
from notemcp.notion.base import NotionWriteError
from notemcp.notion.schema import PROP_TAGS, PROP_TITLE, PROP_TYPE
from notemcp.pipeline import Pipeline

DRAFT = NoteDraft(
    title="Reunião de planejamento",
    doc_type="reuniao",
    tags=["sprint", "backend"],
    summary="Decisões da reunião de planejamento da sprint 12.",
    body_md="## Decisões\n\n- migrar o worker\n- adiar o cache",
)


class FakeProvider:
    id = "fake:test"
    label = "Fake"

    def __init__(self, draft=DRAFT):
        self.draft = draft
        self.calls = []

    def format_note(self, raw, ctx):
        self.calls.append((raw, ctx))
        return self.draft


class FakeWriter:
    def __init__(self):
        self.ensured = []
        self.created = []

    def ensure_destination(self, parent_page_id, kind):
        self.ensured.append((parent_page_id, kind))
        return Destination(kind=kind, id=f"ds-from-{parent_page_id}")

    def create_note(self, destination, draft):
        self.created.append((destination, draft))
        return PageRef(id="page-1", url="https://notion.so/page-1")


def make_pipeline(**overrides):
    settings = Settings(
        notion_token="t", notion_parent_page_id="parent-1", **overrides
    )
    writer = FakeWriter()
    return Pipeline(provider=FakeProvider(), writer=writer, settings=settings), writer


def test_run_formats_then_publishes():
    pipeline, writer = make_pipeline()
    draft, ref = pipeline.run("anotação crua")
    assert draft.title == "Reunião de planejamento"
    assert ref.url == "https://notion.so/page-1"
    assert writer.created[0][0] == Destination(kind=DestinationKind.DATABASE, id="ds-from-parent-1")


def test_configured_data_source_id_skips_ensure_destination():
    pipeline, writer = make_pipeline(notion_data_source_id="ds-fixa")
    pipeline.publish(DRAFT)
    assert writer.ensured == []
    assert writer.created[0][0] == Destination(kind=DestinationKind.DATABASE, id="ds-fixa")


def test_format_and_publish_are_separable():
    """The UI needs to format, allow editing, and only then publish."""
    pipeline, writer = make_pipeline()
    draft = pipeline.format_note("cru")
    assert writer.created == []

    edited = draft.model_copy(update={"title": "Título que eu editei"})
    pipeline.publish(edited)
    assert writer.created[0][1].title == "Título que eu editei"


def test_data_source_shortcut_does_not_apply_to_page():
    """A populated `NOTION_DATA_SOURCE_ID` must not divert the "loose page"
    destination to the database — this is the user's real `.env` (risk
    #2)."""
    pipeline, writer = make_pipeline(notion_data_source_id="ds-fixa")
    pipeline.publish(DRAFT, destination=DestinationChoice.page())

    assert writer.ensured == [("parent-1", DestinationKind.PAGE)]
    destination = writer.created[0][0]
    assert destination.kind is DestinationKind.PAGE
    assert destination.id != "ds-fixa"


def test_resolve_destination_database_with_shortcut_skips_ensure_destination():
    """Case 1/4: DATABASE + NOTION_DATA_SOURCE_ID -> shortcut, no
    network."""
    pipeline, writer = make_pipeline(notion_data_source_id="ds-fixa")
    destination = pipeline._resolve_destination(DestinationChoice.database())
    assert destination == Destination(kind=DestinationKind.DATABASE, id="ds-fixa")
    assert writer.ensured == []


def test_resolve_destination_page_with_concrete_id_is_a_passthrough():
    """Case 2/4: PAGE + a concrete page_id -> ensure_destination(page_id,
    PAGE), which touches no network on any adapter (only the root changes,
    not the mechanism)."""
    pipeline, writer = make_pipeline()
    destination = pipeline._resolve_destination(DestinationChoice.page("materia-x"))
    assert destination == Destination(kind=DestinationKind.PAGE, id="ds-from-materia-x")
    assert writer.ensured == [("materia-x", DestinationKind.PAGE)]


def test_resolve_destination_page_without_id_uses_the_root():
    """Case 3/4: PAGE without a page_id -> root (NOTION_PARENT_PAGE_ID)."""
    pipeline, writer = make_pipeline()
    destination = pipeline._resolve_destination(DestinationChoice.page())
    assert destination == Destination(kind=DestinationKind.PAGE, id="ds-from-parent-1")
    assert writer.ensured == [("parent-1", DestinationKind.PAGE)]


def test_resolve_destination_database_without_shortcut_discovers_it():
    """Case 4/4: DATABASE without NOTION_DATA_SOURCE_ID -> discovery via
    ensure_destination, at the root."""
    pipeline, writer = make_pipeline()
    destination = pipeline._resolve_destination(DestinationChoice.database())
    assert destination == Destination(kind=DestinationKind.DATABASE, id="ds-from-parent-1")
    assert writer.ensured == [("parent-1", DestinationKind.DATABASE)]


def test_from_settings_reuses_an_explicit_writer_instead_of_building_one():
    """An explicit `writer=` (Phase 6, step 14) avoids rebuilding the
    writer on every call — this is what lets the UI's bridge hold a single
    instance per session instead of a new `NotionMcpWriter`/`npx` per
    click."""
    settings = Settings(notion_token="t", notion_parent_page_id="parent-1")
    writer = FakeWriter()
    pipeline = Pipeline.from_settings(settings, store=None, writer=writer)
    assert pipeline.writer is writer


def test_publish_persists_the_destination_before_writing(tmp_path):
    """The destination is saved BEFORE the write: even if it fails, retry
    still knows where to send the note again."""
    from notemcp.store.db import NoteStore

    store = NoteStore(tmp_path / "notes.db")
    settings = Settings(notion_token="t", notion_parent_page_id="parent-1")

    class BoomWriter:
        def ensure_destination(self, parent_page_id, kind):
            return Destination(kind=kind, id="whatever")

        def create_note(self, destination, draft):
            raise NotionWriteError("falha de rede")

    pipeline = Pipeline(provider=FakeProvider(), writer=BoomWriter(), settings=settings, store=store)
    note_id = store.capture("cru")

    with pytest.raises(NotionWriteError):
        pipeline.publish(DRAFT, note_id=note_id, destination=DestinationChoice.page())

    record = store.get(note_id)
    assert record.destination_kind == DestinationKind.PAGE
    assert record.status == "failed"


def test_provider_failure_propagates_without_fallback():
    class Broken:
        id, label = "broken:x", "Broken"

        def format_note(self, raw, ctx):
            raise LLMError(self.id, "modelo fora do ar")

    settings = Settings(notion_token="t", notion_parent_page_id="p")
    pipeline = Pipeline(provider=Broken(), writer=FakeWriter(), settings=settings)
    with pytest.raises(LLMError, match=r"\[broken:x\] modelo fora do ar"):
        pipeline.run("cru")


def test_generate_draft_repairs_invalid_json_once():
    attempts = []

    def call(system, user, repair):
        attempts.append(repair)
        if repair is None:
            return "isso não é json"
        return DRAFT.model_dump_json()

    draft = generate_draft("p:1", "cru", FormatContext(), call)
    assert draft.title == DRAFT.title
    assert len(attempts) == 2 and attempts[0] is None and attempts[1] is not None


def test_generate_draft_gives_up_after_one_repair():
    def call(system, user, repair):
        return "sempre lixo"

    with pytest.raises(LLMError, match="não devolveu JSON válido"):
        generate_draft("p:1", "cru", FormatContext(), call)


def test_fenced_json_is_still_accepted():
    def call(system, user, repair):
        return "```json\n" + DRAFT.model_dump_json() + "\n```"

    assert generate_draft("p:1", "cru", FormatContext(), call).title == DRAFT.title


def test_build_provider_rejects_ollama_with_a_migration_message():
    """Ollama was removed (Fase 3 of the Gemini-only migration). A `.env`
    left over from before must not fail with a generic "unknown backend" —
    it needs to say exactly what to change."""
    from notemcp.llm.base import build_provider

    settings = Settings(gemini_api_key="k")
    with pytest.raises(ValueError, match=r"provedor local \(Ollama\) foi removido"):
        build_provider("ollama:qwen3:8b", settings)
    with pytest.raises(ValueError, match="gemini:<modelo>"):
        build_provider("ollama:qwen3:8b", settings)


def test_build_provider_rejects_an_unknown_backend():
    from notemcp.llm.base import build_provider

    settings = Settings(gemini_api_key="k")
    with pytest.raises(ValueError, match="backend desconhecido"):
        build_provider("claude:opus", settings)


def test_build_provider_builds_a_gemini_provider():
    from notemcp.llm.base import build_provider
    from notemcp.llm.gemini import GeminiProvider

    settings = Settings(gemini_api_key="k", llm_timeout=42.0)
    provider = build_provider("gemini:gemini-3.5-flash-lite", settings)

    assert isinstance(provider, GeminiProvider)
    assert provider.model == "gemini-3.5-flash-lite"
    assert provider.timeout == 42.0


def test_fake_provider_satisfies_the_protocol():
    """`LLMProvider` is also `@runtime_checkable`, but never had a contract
    test via `isinstance` — only `NotionWriter`/`NotionBrowser` did (D11)."""
    assert isinstance(FakeProvider(), LLMProvider)


class FakeNotionClient:
    """Fake faithful to the 2025-09-03 API shape (databases -> data
    sources).

    It **rejects** the old API's formats on purpose: that exact silent
    mismatch is what created databases without properties and made
    `pages.create` fail.
    """

    def __init__(self, children=(), properties=None):
        self.created_page = None
        self.created_database = None
        self.updated_properties = None
        self.appends = []
        self._children = list(children)
        self._properties = properties if properties is not None else {"Name": {"title": {}}}
        outer = self

        class Pages:
            def create(self, parent, properties, children=()):
                """The parent's shape depends on the destination:
                data_source_id for the database, page_id for a loose page
                — asserting only one of the two unconditionally would hide
                the other path."""
                assert parent.get("type") in ("data_source_id", "page_id"), (
                    f"parent precisa ser data_source_id ou page_id, veio {parent!r}"
                )
                outer.created_page = {
                    "parent": parent, "properties": properties, "children": children
                }
                return {"id": "page-1", "url": "https://notion.so/page-1"}

        class Databases:
            def create(self, parent, title, initial_data_source):
                outer.created_database = {
                    "parent": parent, "title": title,
                    "initial_data_source": initial_data_source,
                }
                return {"id": "db-1", "data_sources": [{"id": "ds-1", "name": "Notas"}]}

            def retrieve(self, database_id):
                return {"id": database_id, "data_sources": [{"id": "ds-1", "name": "Notas"}]}

        class DataSources:
            def retrieve(self, data_source_id):
                return {"id": data_source_id, "properties": dict(outer._properties)}

            def update(self, data_source_id, properties):
                outer.updated_properties = properties
                outer._properties.update(properties)
                return {}

        class Children:
            def append(self, block_id, children):
                outer.appends.append((block_id, children))
                return {}

            def list(self, block_id, start_cursor=None):
                return {"results": outer._children, "has_more": False, "next_cursor": None}

        class Blocks:
            children = Children()

        self.pages = Pages()
        self.databases = Databases()
        self.data_sources = DataSources()
        self.blocks = Blocks()


def child_database_block(block_id, title):
    return {"id": block_id, "type": "child_database", "child_database": {"title": title}}


def test_new_database_carries_properties_in_initial_data_source():
    """Top-level `properties=` is discarded by the SDK — the database
    would come out with only the Name field, and create_note would fail
    later."""
    client = FakeNotionClient()
    destination = NotionApiWriter(token="t", client=client).ensure_destination(
        "parent-1", DestinationKind.DATABASE
    )

    assert destination == Destination(kind=DestinationKind.DATABASE, id="ds-1")
    props = client.created_database["initial_data_source"]["properties"]
    assert {PROP_TYPE, PROP_TAGS} <= set(props)


def test_existing_database_is_reused_instead_of_duplicated():
    client = FakeNotionClient(children=[child_database_block("db-existente", "Notas")])
    destination = NotionApiWriter(token="t", client=client).ensure_destination(
        "parent-1", DestinationKind.DATABASE
    )

    assert destination.id == "ds-1"
    assert client.created_database is None, "não pode criar uma segunda database"


def test_existing_database_missing_properties_is_patched():
    """`properties` below simulates a database created before this
    feature existed, still missing the Type/Tags/etc. properties."""
    client = FakeNotionClient(
        children=[child_database_block("db-velha", "Notas")],
        properties={"Name": {"title": {}}},
    )
    NotionApiWriter(token="t", client=client).ensure_destination(
        "parent-1", DestinationKind.DATABASE
    )

    assert client.updated_properties is not None
    assert {PROP_TYPE, PROP_TAGS} <= set(client.updated_properties)
    assert PROP_TITLE not in client.updated_properties, "title não pode ser re-adicionado"


def test_chunk_children_respects_limit():
    blocks = [{"i": i} for i in range(250)]
    batches = list(chunk_children(blocks))
    assert [len(b) for b in batches] == [100, 100, 50]
    assert [b["i"] for batch in batches for b in batch] == list(range(250))


def test_create_note_appends_overflow_blocks_in_order():
    body = "\n\n".join(f"parágrafo {i}" for i in range(250))
    client = FakeNotionClient()
    writer = NotionApiWriter(token="t", client=client)

    destination = Destination(kind=DestinationKind.DATABASE, id="ds-1")
    ref = writer.create_note(destination, DRAFT.model_copy(update={"body_md": body}))

    assert len(client.created_page["children"]) == MAX_CHILDREN_PER_REQUEST
    assert [len(c) for _, c in client.appends] == [100, 50]
    assert all(block_id == "page-1" for block_id, _ in client.appends)
    assert ref.url == "https://notion.so/page-1"


def test_page_note_with_100_blocks_does_not_exceed_the_batch():
    """The callout is inserted BEFORE chunk_children runs: 100 body blocks
    + 1 callout is 101 blocks, and the first batch must not exceed 100
    (risk #9)."""
    body = "\n\n".join(f"parágrafo {i}" for i in range(100))
    client = FakeNotionClient()
    writer = NotionApiWriter(token="t", client=client)

    destination = Destination(kind=DestinationKind.PAGE, id="parent-1")
    writer.create_note(destination, DRAFT.model_copy(update={"body_md": body}))

    assert len(client.created_page["children"]) == MAX_CHILDREN_PER_REQUEST
    assert client.created_page["children"][0]["type"] == "callout"
    assert [len(c) for _, c in client.appends] == [1], "o callout empurrou 1 bloco pro segundo lote"


def test_create_note_sets_properties_from_draft():
    client = FakeNotionClient()
    destination = Destination(kind=DestinationKind.DATABASE, id="ds-1")
    NotionApiWriter(token="t", client=client).create_note(destination, DRAFT)
    assert client.created_page["parent"] == {
        "type": "data_source_id", "data_source_id": "ds-1"
    }
    props = client.created_page["properties"]
    assert props[PROP_TITLE]["title"][0]["text"]["content"] == DRAFT.title
    assert props[PROP_TYPE]["select"]["name"] == "reuniao"
    assert [t["name"] for t in props[PROP_TAGS]["multi_select"]] == ["sprint", "backend"]


def test_transport_retry_recovers_from_connect_error(monkeypatch):
    """notion-client only retries *API* errors; a ConnectError propagates
    directly."""
    import notemcp.notion.api_writer as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("connection reset by peer")
        return "ok"

    assert mod._retry(flaky, idempotent=True) == "ok"
    assert len(calls) == 3


def test_ambiguous_transport_error_is_not_retried_on_writes(monkeypatch):
    """`RemoteProtocolError` means the server may have already processed
    the request — retrying a pages.create would create a duplicate
    page."""
    import notemcp.notion.api_writer as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    calls = []

    def flaky():
        calls.append(1)
        raise httpx.RemoteProtocolError("server disconnected")

    with pytest.raises(httpx.RemoteProtocolError):
        mod._retry(flaky, idempotent=False)
    assert len(calls) == 1, "escrita ambígua não pode ser repetida"


def test_connect_error_is_retried_even_on_writes(monkeypatch):
    """ConnectError = the connection was never established, so the
    request never arrived."""
    import notemcp.notion.api_writer as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("reset")
        return "ok"

    assert mod._retry(flaky, idempotent=False) == "ok"


def test_network_failure_does_not_blame_permissions(monkeypatch):
    """Telling the user to debug permissions because of a downed network
    wastes time on the wrong problem."""
    import notemcp.notion.api_writer as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _: None)

    class Dead:
        class blocks:
            class children:
                @staticmethod
                def list(block_id, start_cursor=None):
                    raise httpx.ConnectError("connection reset by peer")

    with pytest.raises(NotionWriteError) as e:
        NotionApiWriter(token="t", client=Dead()).ensure_destination(
            "parent-1", DestinationKind.DATABASE
        )

    msg = str(e.value)
    assert "conectividade" in msg
    assert "Connections" not in msg, "não pode sugerir problema de permissão"


