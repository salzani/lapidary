"""Contract tests for the `NotionWriter` port.

The contract tests run against BOTH adapters with the same transport fake.
The point of the port is that the pipeline and the UI don't know which one
is active — if either adapter diverges in observable behavior, switching
between them via config stops being safe and these tests break.

Also covers the `NotionBrowser` port (Phase 6) for both adapters, the
places where the two adapters deliberately DIVERGE (block compilation vs.
raw markdown), and API-writer-specific transport error handling.
"""

import pytest

from notemcp.models import Destination, DestinationKind, NoteDraft
from notemcp.notion.api_writer import NotionApiWriter
from notemcp.notion.base import NotionWriteError, NotionWriter
from notemcp.notion.metadata import metadata_callout_md
from notemcp.notion.schema import PROP_TAGS, PROP_TITLE, PROP_TYPE

pytest.importorskip("mcp")
from notemcp.notion.mcp_writer import NotionMcpWriter  # noqa: E402

DRAFT = NoteDraft(
    title="Cache de embeddings",
    doc_type="ideia",
    tags=["cache", "sqlite"],
    summary="Guardar embeddings em disco.",
    body_md="## Motivação\n\n- não recalcular a cada boot\n\n```python\nx = 1\n```",
)

DB_ID = "db-1"
DS_ID = "ds-1"


def child_database(title):
    return {"id": DB_ID, "type": "child_database", "child_database": {"title": title}}


def child_page(page_id, title):
    return {"id": page_id, "type": "child_page", "child_page": {"title": title}}


def _paginate(children, pages):
    """`pages` (a list of block lists) wins over `children` (a single list,
    returned as one page) — used by tests that need to simulate `has_more`
    (risk #4: pagination not fully drained)."""
    return list(pages) if pages is not None else [list(children)]


class FakeApiClient:
    """Faithful to the notion-client SDK on the 2025-09-03 API."""

    def __init__(self, children=(), pages=None):
        self.calls = []
        self._pages = _paginate(children, pages)
        outer = self

        class Pages:
            def create(self, parent, properties, children=()):
                outer.calls.append((
                    "create_page",
                    {"parent": parent, "properties": properties, "children": children},
                ))
                return {"id": "page-1", "url": "https://notion.so/page-1"}

        class Databases:
            def create(self, parent, title, initial_data_source):
                outer.calls.append(("create_db", initial_data_source))
                return {"id": DB_ID, "data_sources": [{"id": DS_ID}]}

            def retrieve(self, database_id):
                return {"id": database_id, "data_sources": [{"id": DS_ID}]}

        class DataSources:
            def retrieve(self, data_source_id):
                return {"id": data_source_id, "properties": dict(_full_props())}

            def update(self, data_source_id, properties):
                return {}

        class Children:
            def append(self, block_id, children):
                outer.calls.append(("append", len(children)))
                return {}

            def list(self, block_id, start_cursor=None):
                index = int(start_cursor) if start_cursor is not None else 0
                results = outer._pages[index]
                next_index = index + 1
                has_more = next_index < len(outer._pages)
                return {
                    "results": results,
                    "has_more": has_more,
                    "next_cursor": str(next_index) if has_more else None,
                }

        class Blocks:
            children = Children()

        self.pages, self.databases = Pages(), Databases()
        self.data_sources, self.blocks = DataSources(), Blocks()


class FakeMcpSession:
    """Faithful to the MCP server: returns the Notion API's raw JSON."""

    def __init__(self, children=(), pages=None):
        self.calls = []
        self._pages = _paginate(children, pages)

    def close(self):
        pass

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        if tool == "API-get-block-children":
            index = int(arguments["start_cursor"]) if "start_cursor" in arguments else 0
            results = self._pages[index]
            next_index = index + 1
            has_more = next_index < len(self._pages)
            return {
                "results": results,
                "has_more": has_more,
                "next_cursor": str(next_index) if has_more else None,
            }
        if tool == "API-retrieve-a-database":
            return {"id": DB_ID, "data_sources": [{"id": DS_ID}]}
        if tool == "API-create-a-data-source":
            return {"id": DS_ID}
        if tool == "API-post-page":
            return {"id": "page-1", "url": "https://notion.so/page-1"}
        if tool == "API-update-page-markdown":
            return {"ok": True}
        raise AssertionError(f"unexpected tool: {tool}")


def _full_props():
    from notemcp.notion.schema import DATABASE_PROPERTIES

    return DATABASE_PROPERTIES


def make_api(children=(), pages=None):
    return NotionApiWriter(token="t", client=FakeApiClient(children, pages=pages))


def make_mcp(children=(), pages=None):
    return NotionMcpWriter(token="t", session=FakeMcpSession(children, pages=pages))


BUILDERS = {"api": make_api, "mcp": make_mcp}


@pytest.mark.parametrize("kind", BUILDERS)
def test_satisfies_the_port(kind):
    assert isinstance(BUILDERS[kind](), NotionWriter)


@pytest.mark.parametrize("kind", BUILDERS)
def test_existing_database_is_reused(kind):
    writer = BUILDERS[kind]([child_database("Notas")])
    destination = writer.ensure_destination("parent-1", DestinationKind.DATABASE)
    assert destination == Destination(kind=DestinationKind.DATABASE, id=DS_ID)


@pytest.mark.parametrize("kind", BUILDERS)
def test_missing_database_is_created(kind):
    writer = BUILDERS[kind]([])
    destination = writer.ensure_destination("parent-1", DestinationKind.DATABASE)
    assert destination == Destination(kind=DestinationKind.DATABASE, id=DS_ID)


@pytest.mark.parametrize("kind", BUILDERS)
def test_unrelated_child_database_is_ignored(kind):
    """Migrated from `test_pipeline.py::test_unrelated_child_database_is_ignored`
    (B10): without the assertion below, this test would pass even if
    "ignore unrelated databases" were broken, as long as the returned id
    happened to match DS_ID by accident — it confirms that a NEW database
    was actually created, not reused."""
    writer = BUILDERS[kind]([child_database("Outra coisa")])
    destination = writer.ensure_destination("parent-1", DestinationKind.DATABASE)
    assert destination == Destination(kind=DestinationKind.DATABASE, id=DS_ID)

    calls = writer.client.calls if kind == "api" else writer.session.calls
    created_tool = "create_db" if kind == "api" else "API-create-a-data-source"
    assert any(call[0] == created_tool for call in calls)


@pytest.mark.parametrize("kind", BUILDERS)
def test_create_note_returns_page_ref(kind):
    destination = Destination(kind=DestinationKind.DATABASE, id=DS_ID)
    ref = BUILDERS[kind]().create_note(destination, DRAFT)
    assert ref.id == "page-1"
    assert ref.url == "https://notion.so/page-1"


@pytest.mark.parametrize("kind", BUILDERS)
def test_create_note_sends_the_draft_properties(kind):
    writer = BUILDERS[kind]()
    destination = Destination(kind=DestinationKind.DATABASE, id=DS_ID)
    writer.create_note(destination, DRAFT)

    if kind == "api":
        props = dict(writer.client.calls)["create_page"]["properties"]
    else:
        props = dict(writer.session.calls)["API-post-page"]["properties"]

    assert props[PROP_TITLE]["title"][0]["text"]["content"] == DRAFT.title
    assert props[PROP_TYPE]["select"]["name"] == "ideia"
    assert [t["name"] for t in props[PROP_TAGS]["multi_select"]] == ["cache", "sqlite"]


@pytest.mark.parametrize("kind", BUILDERS)
def test_page_destination_makes_no_network_calls(kind):
    """`PAGE` is `parent_page_id` itself: no database discovery needed."""
    writer = BUILDERS[kind]()
    destination = writer.ensure_destination("parent-1", DestinationKind.PAGE)

    assert destination == Destination(kind=DestinationKind.PAGE, id="parent-1")
    calls = writer.client.calls if kind == "api" else writer.session.calls
    assert calls == [], "destino PAGE não pode tocar a rede"


@pytest.mark.parametrize("kind", BUILDERS)
def test_page_destination_requires_parent_page_id(kind):
    writer = BUILDERS[kind]()
    with pytest.raises(NotionWriteError, match="NOTION_PARENT_PAGE_ID"):
        writer.ensure_destination("", DestinationKind.PAGE)


@pytest.mark.parametrize("kind", BUILDERS)
def test_page_note_uses_page_id_parent(kind):
    writer = BUILDERS[kind]()
    destination = Destination(kind=DestinationKind.PAGE, id="parent-1")
    writer.create_note(destination, DRAFT)

    if kind == "api":
        parent = dict(writer.client.calls)["create_page"]["parent"]
    else:
        parent = dict(writer.session.calls)["API-post-page"]["parent"]
    assert parent == {"type": "page_id", "page_id": "parent-1"}


@pytest.mark.parametrize("kind", BUILDERS)
def test_page_note_uses_title_key_not_name(kind):
    """A plain child page has no "Name" database column — the key is
    `title`."""
    writer = BUILDERS[kind]()
    destination = Destination(kind=DestinationKind.PAGE, id="parent-1")
    writer.create_note(destination, DRAFT)

    if kind == "api":
        props = dict(writer.client.calls)["create_page"]["properties"]
    else:
        props = dict(writer.session.calls)["API-post-page"]["properties"]
    assert PROP_TITLE not in props
    assert props["title"]["title"][0]["text"]["content"] == DRAFT.title


@pytest.mark.parametrize("kind", BUILDERS)
def test_page_note_carries_the_metadata_first(kind):
    writer = BUILDERS[kind]()
    destination = Destination(kind=DestinationKind.PAGE, id="parent-1")
    writer.create_note(destination, DRAFT)

    if kind == "api":
        children = dict(writer.client.calls)["create_page"]["children"]
        assert children[0]["type"] == "callout", "callout precisa ser o PRIMEIRO bloco"
    else:
        new_str = dict(writer.session.calls)["API-update-page-markdown"]
        new_str = new_str["replace_content"]["new_str"]
        assert new_str.startswith("> [!NOTE]")
        assert DRAFT.body_md in new_str


@pytest.mark.parametrize("kind", BUILDERS)
def test_database_note_is_unchanged(kind):
    """The DATABASE destination must not gain any side effect from
    PAGE."""
    writer = BUILDERS[kind]()
    destination = Destination(kind=DestinationKind.DATABASE, id=DS_ID)
    writer.create_note(destination, DRAFT)

    if kind == "api":
        call = dict(writer.client.calls)["create_page"]
        assert call["parent"] == {"type": "data_source_id", "data_source_id": DS_ID}
        assert all(c["type"] != "callout" for c in call["children"])
    else:
        new_str = dict(writer.session.calls)["API-update-page-markdown"]
        new_str = new_str["replace_content"]["new_str"]
        assert new_str == DRAFT.body_md


def test_mcp_page_body_starts_with_the_header_and_preserves_the_body():
    """Metadata header up front, the draft's body untouched behind it."""
    writer = make_mcp()
    destination = Destination(kind=DestinationKind.PAGE, id="parent-1")
    writer.create_note(destination, DRAFT)

    new_str = dict(writer.session.calls)["API-update-page-markdown"]
    new_str = new_str["replace_content"]["new_str"]

    header = metadata_callout_md(DRAFT)
    assert new_str.startswith(header)
    assert new_str == f"{header}\n\n{DRAFT.body_md}"
    assert DRAFT.body_md in new_str


def test_api_writer_compiles_markdown_into_blocks():
    writer = make_api()
    destination = Destination(kind=DestinationKind.DATABASE, id=DS_ID)
    writer.create_note(destination, DRAFT)
    parent_call = dict(writer.client.calls)["create_page"]
    assert parent_call["parent"] == {"type": "data_source_id", "data_source_id": DS_ID}


def test_mcp_writer_sends_raw_markdown_and_skips_the_compiler():
    """The MCP server accepts raw Markdown directly — that is this
    adapter's reason to exist. Page creation must also carry no blocks."""
    writer = make_mcp()
    destination = Destination(kind=DestinationKind.DATABASE, id=DS_ID)
    writer.create_note(destination, DRAFT)
    calls = dict(writer.session.calls)

    md = calls["API-update-page-markdown"]
    assert md["type"] == "replace_content"
    assert md["replace_content"]["new_str"] == DRAFT.body_md, "markdown vai literal"
    assert "children" not in calls["API-post-page"]


def test_mcp_tool_error_becomes_notion_write_error():
    class Boom:
        def close(self): pass

        def call(self, tool, arguments):
            raise NotionWriteError("API-post-page falhou: unauthorized")

    destination = Destination(kind=DestinationKind.DATABASE, id=DS_ID)
    with pytest.raises(NotionWriteError, match="unauthorized"):
        NotionMcpWriter(token="t", session=Boom()).create_note(destination, DRAFT)


def test_mcp_database_without_data_source_is_reported():
    class NoSource(FakeMcpSession):
        def call(self, tool, arguments):
            if tool == "API-retrieve-a-database":
                return {"id": DB_ID, "data_sources": []}
            return super().call(tool, arguments)

    writer = NotionMcpWriter(token="t", session=NoSource([child_database("Notas")]))
    with pytest.raises(NotionWriteError, match="não tem data source"):
        writer.ensure_destination("parent-1", DestinationKind.DATABASE)


def test_mcp_writer_requires_a_token():
    with pytest.raises(NotionWriteError, match="NOTION_TOKEN"):
        NotionMcpWriter(token="")


from notemcp.notion.browser import NotionBrowser, PageNode  # noqa: E402


@pytest.mark.parametrize("kind", BUILDERS)
def test_satisfies_the_browser_port(kind):
    assert isinstance(BUILDERS[kind](), NotionBrowser)


@pytest.mark.parametrize("kind", BUILDERS)
def test_lists_only_child_pages_in_document_order(kind):
    writer = BUILDERS[kind]([
        child_page("p-1", "Faculdade"),
        child_database("Notas"),
        child_page("p-2", "Trabalho"),
    ])
    nodes = writer.list_child_pages("parent-1")
    assert nodes == [PageNode("p-1", "Faculdade"), PageNode("p-2", "Trabalho")]


@pytest.mark.parametrize("kind", BUILDERS)
def test_list_child_pages_drains_pagination(kind):
    """A level with two result pages (`has_more`) must not lose the second
    batch — it would drop a subject from the list (risk #4)."""
    writer = BUILDERS[kind](pages=[
        [child_page("p-1", "Faculdade")],
        [child_page("p-2", "Trabalho")],
    ])
    nodes = writer.list_child_pages("parent-1")
    assert [n.id for n in nodes] == ["p-1", "p-2"]


@pytest.mark.parametrize("kind", BUILDERS)
def test_list_child_pages_of_an_empty_level_is_empty_list(kind):
    assert BUILDERS[kind]([]).list_child_pages("parent-1") == []


@pytest.mark.parametrize("kind", BUILDERS)
def test_create_child_page_returns_the_page_node(kind):
    node = BUILDERS[kind]().create_child_page("parent-1", "Cálculo I")
    assert node == PageNode("page-1", "Cálculo I")


@pytest.mark.parametrize("kind", BUILDERS)
def test_create_child_page_sends_the_title_key_not_name(kind):
    writer = BUILDERS[kind]()
    writer.create_child_page("parent-1", "Cálculo I")

    if kind == "api":
        call = dict(writer.client.calls)["create_page"]
        parent, properties = call["parent"], call["properties"]
    else:
        call = dict(writer.session.calls)["API-post-page"]
        parent, properties = call["parent"], call["properties"]

    assert parent == {"type": "page_id", "page_id": "parent-1"}
    assert properties == {"title": {"title": [{"text": {"content": "Cálculo I"}}]}}


def test_mcp_create_child_page_does_not_send_a_body():
    """A parent page is title-only: no `API-update-page-markdown` call
    (there is no body to write after creation)."""
    writer = make_mcp()
    writer.create_child_page("parent-1", "Cálculo I")
    tools_called = [tool for tool, _ in writer.session.calls]
    assert "API-update-page-markdown" not in tools_called


def test_api_writer_create_child_page_does_not_retry_a_remote_protocol_error(monkeypatch):
    """Ambiguous — the server may have already processed the request —
    retrying would duplicate the page. The message tells the user to
    reload instead of suggesting a retry."""
    import httpx

    import notemcp.notion.api_writer as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _: None)

    class Dead:
        class pages:
            @staticmethod
            def create(parent, properties, children=()):
                raise httpx.RemoteProtocolError("server disconnected")

    with pytest.raises(NotionWriteError, match="Recarregar"):
        NotionApiWriter(token="t", client=Dead()).create_child_page("parent-1", "Cálculo I")


def test_api_writer_list_child_pages_connect_error_is_a_connectivity_message(monkeypatch):
    import httpx

    import notemcp.notion.api_writer as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _: None)

    class Dead:
        class blocks:
            class children:
                @staticmethod
                def list(block_id, start_cursor=None):
                    raise httpx.ConnectError("connection reset by peer")

    with pytest.raises(NotionWriteError) as exc:
        NotionApiWriter(token="t", client=Dead()).list_child_pages("parent-1")
    msg = str(exc.value)
    assert "conectividade" in msg
    assert "Connections" not in msg


def test_api_writer_create_note_error_cites_the_page_id_for_page_destination():
    """The error message when publishing to a page must cite the id and
    the right hypothesis (page gone/not shared), not just echo the API's
    exception — see `PLANO_PAGINA_MAE.md` §6.2."""
    class Dead:
        class pages:
            @staticmethod
            def create(parent, properties, children=()):
                raise RuntimeError("Not found")

    destination = Destination(kind=DestinationKind.PAGE, id="pagina-sumida")
    with pytest.raises(NotionWriteError) as exc:
        NotionApiWriter(token="t", client=Dead()).create_note(destination, DRAFT)
    assert "pagina-sumida" in str(exc.value)
