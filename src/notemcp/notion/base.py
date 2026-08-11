"""`NotionWriter` port.

Two adapters implement this and are interchangeable via config
(`NOTION_WRITER=api|mcp`):

  - ``api_writer.NotionApiWriter``  — uses the Markdown->blocks compiler and
    the REST API. Fine-grained control over blocks; this is where the real
    complexity lives (batching, the 2000-char limit, nesting).
  - ``mcp_writer.NotionMcpWriter``  — talks to Notion's official MCP server
    over stdio. Its tools already accept Markdown, so this adapter **skips
    the compiler**. Simpler, less control.

Callers (pipeline, UI) do not know which one is active.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import PARENT_PAGE_ID_REQUIRED_MESSAGE, Destination, DestinationKind, NoteDraft, PageRef


class NotionWriteError(RuntimeError):
    """Failure talking to Notion, already translated into human language."""


@runtime_checkable
class NotionWriter(Protocol):
    def ensure_destination(self, parent_page_id: str, kind: DestinationKind) -> Destination:
        """Return the destination for notes, creating it if necessary.

        `kind` has no default on purpose: every call site must be explicit
        about which destination it is resolving. Deliberately called
        "destination" and not "database": in the 2025-09-03 API, a database
        is a container of *data sources*, and it is the data source id that
        receives pages — but `kind == PAGE` bypasses all of that, it is
        `parent_page_id` itself. The MCP adapter may use yet another notion
        of destination. Callers just pass the result through to
        `create_note`.
        """
        ...

    def create_note(self, destination: Destination, draft: NoteDraft) -> PageRef:
        """Publish the draft as a new page at the destination."""
        ...


def page_destination(parent_page_id: str) -> Destination:
    """Resolve the "loose page" destination: no network call, it is the parent page itself.

    Shared by both adapters because it does not depend on either — the id of
    a PAGE destination is always `parent_page_id`, no discovery needed.
    """
    if not parent_page_id:
        raise NotionWriteError(PARENT_PAGE_ID_REQUIRED_MESSAGE)
    return Destination(kind=DestinationKind.PAGE, id=parent_page_id)
