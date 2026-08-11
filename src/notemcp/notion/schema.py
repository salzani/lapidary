"""Schema of the "Notas" database and assembly of each page's properties."""

from __future__ import annotations

from datetime import date
from typing import Any

from ..models import DOC_TYPES, MAX_TITLE_LEN, NoteDraft

DATABASE_TITLE = "Notas"

PROP_TITLE = "Name"
PROP_TYPE = "Tipo"
PROP_TAGS = "Tags"
PROP_DATE = "Criado em"
PROP_SUMMARY = "Resumo"

MAX_RICH_TEXT = 2000

DATABASE_PROPERTIES: dict[str, Any] = {
    PROP_TITLE: {"title": {}},
    PROP_TYPE: {"select": {"options": [{"name": t} for t in DOC_TYPES]}},
    PROP_TAGS: {"multi_select": {}},
    PROP_DATE: {"date": {}},
    PROP_SUMMARY: {"rich_text": {}},
}


def page_properties(draft: NoteDraft) -> dict[str, Any]:
    """Build page properties from the draft, for the "database" destination."""
    return {
        PROP_TITLE: {"title": [{"text": {"content": draft.title[:MAX_TITLE_LEN]}}]},
        PROP_TYPE: {"select": {"name": draft.doc_type}},
        PROP_TAGS: {"multi_select": [{"name": tag} for tag in draft.tags]},
        PROP_DATE: {"date": {"start": date.today().isoformat()}},
        PROP_SUMMARY: {
            "rich_text": [{"text": {"content": draft.summary[:MAX_RICH_TEXT]}}]
        },
    }


def title_properties(title: str) -> dict[str, Any]:
    """Title properties for a page that is NOT a database row.

    The key is literally `"title"` — not `PROP_TITLE` ("Name"), which is the
    name *we* gave the title column of the "Notas" database. Used both to
    publish a note (`page_title_properties`) and to create an empty
    parent page through the browsable tree (Phase 6:
    `notion/browser.py:create_child_page`) — both situations only have a
    title, nothing else.
    """
    return {"title": {"title": [{"text": {"content": title[:MAX_TITLE_LEN]}}]}}


def page_title_properties(draft: NoteDraft) -> dict[str, Any]:
    """Properties for a regular child page (the "page" destination).

    Tipo, Tags, and Resumo have nowhere to go here; see `notion/metadata.py`
    for the callout that carries those fields in the page body instead.
    """
    return title_properties(draft.title)
