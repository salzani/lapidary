"""Page properties, for both destinations: page (`title` key) and database
(`PROP_TITLE`/"Name" key), plus `title_properties` (Phase 6), which applies
the same rule to a bare title instead of a full `NoteDraft`."""

from notemcp.models import NoteDraft
from notemcp.notion.schema import PROP_TITLE, page_title_properties, title_properties

DRAFT = NoteDraft(
    title="Cache de embeddings",
    doc_type="ideia",
    tags=["cache"],
    summary="Guardar embeddings em disco.",
    body_md="corpo",
)


def test_page_title_properties_uses_title_key_not_name():
    """A plain child page is not a database row: the key is `title`, not
    `PROP_TITLE` ("Name") — that is the name the "Notas" database gives its
    column."""
    assert page_title_properties(DRAFT) == {
        "title": {"title": [{"text": {"content": "Cache de embeddings"}}]}
    }
    assert PROP_TITLE not in page_title_properties(DRAFT)


def test_page_title_properties_truncates_at_100_chars():
    draft = DRAFT.model_copy(update={"title": "x" * 150})
    props = page_title_properties(draft)
    content = props["title"]["title"][0]["text"]["content"]
    assert len(content) == 100


def test_page_title_properties_delegates_to_title_properties():
    assert page_title_properties(DRAFT) == title_properties(DRAFT.title)


def test_title_properties_truncates_at_max_title_len():
    props = title_properties("y" * 150)
    content = props["title"]["title"][0]["text"]["content"]
    assert len(content) == 100
