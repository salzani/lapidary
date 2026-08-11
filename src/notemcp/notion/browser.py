"""`NotionBrowser` port: navigation of the Notion page tree (Phase 6).

Kept separate from `NotionWriter` through genuine Interface Segregation:
code that only browses (the UI opening the parent-page picker, the CLI
resolving `--parent`) does not need to see `create_note`, and a publish test
fake does not need to implement navigation.

The concrete implementations, however, are the SAME classes that already
implement `NotionWriter` (`NotionApiWriter`, `NotionMcpWriter`) — a separate
browser class would require a second transport (another
`_McpSession`/`npx`, or another `httpx.Client`) for nothing. See
`notion/base.py` for the write port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from ..config import Settings
from ..models import title_slug

ROOT_LABEL = "Root page"


@dataclass(frozen=True)
class PageNode:
    id: str
    title: str


class NotionBrowseError(RuntimeError):
    """NAVIGATION/resolution failure: path not found, ambiguous title.

    Distinct from `NotionWriteError` (transport/permission) — the CLI
    catches both, but with different cause messages.
    """


@runtime_checkable
class NotionBrowser(Protocol):
    def list_child_pages(self, page_id: str) -> list[PageNode]:
        """Direct subpages of `page_id`, IN DOCUMENT ORDER.

        Only `child_page` blocks: `child_database` is excluded on purpose —
        a database cannot be the parent of a regular page, so offering it as
        a parent-page choice would look legitimate and only fail at publish
        time (Phase 6 risk 5).

        Paginates to the end (`has_more`). A truncated list is a silent
        lie: it would hide a subject/course and the user would create a
        duplicate (risk 4).

        Does NOT return `has_children`: for a `child_page`, that field is
        True when the page has ANY block, not subpages — using it to decide
        whether to allow drilling in would show "expandable" on a page of
        plain text. It is always possible to drill in; an empty level just
        shows "no subpages".
        """
        ...

    def create_child_page(self, parent_page_id: str, title: str) -> PageNode:
        """Create an empty (title-only) page as a child of `parent_page_id`.

        NOT idempotent: Notion accepts two pages with the same title under
        the same parent. The duplicate-handling policy (ask, refuse, create
        anyway) belongs to the caller — see `ui/bridge.py:checkChildTitle`.
        """
        ...


def build_browser(settings: Settings) -> NotionBrowser:
    """Build the adapter configured via `NOTION_WRITER`.

    `pipeline._build_writer` delegates to this function (consolidation B1:
    they used to be two identical copies) — the two ports (`NotionWriter`,
    `NotionBrowser`) are satisfied by the same concrete instance, so
    "browse" and "write" always use the same transport. Local import:
    `notion_client`/`mcp` are optional extras, and this module must not
    force installing both for someone who only uses one.
    """
    kind = settings.notion_writer.strip().lower()
    if kind == "api":
        from .api_writer import NotionApiWriter

        return NotionApiWriter(token=settings.notion_token)
    if kind == "mcp":
        try:
            from .mcp_writer import NotionMcpWriter
        except ImportError as exc:
            raise ValueError(
                "NOTION_WRITER=mcp requires the MCP SDK — "
                "`pip install 'notemcp[mcp]'` (and Node installed, for npx)."
            ) from exc

        return NotionMcpWriter(token=settings.notion_token)
    raise ValueError(f"invalid NOTION_WRITER: {kind!r} (use 'api' or 'mcp')")


def match_by_title(nodes: Sequence[PageNode], title: str) -> list[PageNode]:
    """Return children whose normalized title matches `title`.

    Compared via `title_slug` (NFKD, no accents/case/punctuation) — the SAME
    rule that detects a duplicated H1 in `models.py`, so the project does not
    end up with two different notions of "equal titles" (risk 8: divergence
    between JS and Python).
    """
    target = title_slug(title)
    return [node for node in nodes if title_slug(node.title) == target]


def resolve_page_path(browser: NotionBrowser, root_id: str, parts: Sequence[str]) -> PageNode:
    """Descend from `root_id`, matching each part of `parts` by title.

    An empty path returns the root itself. Each level is resolved with an
    EXACT slug match — never "best match" (risk 10):

      - title absent at that level -> `NotionBrowseError` listing the
        titles available there;
      - ambiguous title (two pages with the same slug) -> `NotionBrowseError`
        listing the candidate ids, so the caller can use `--parent-id`.
    """
    node = PageNode(id=root_id, title=ROOT_LABEL)
    for part in parts:
        children = browser.list_child_pages(node.id)
        matches = match_by_title(children, part)

        if not matches:
            available = ", ".join(c.title for c in children) or "(no subpages)"
            raise NotionBrowseError(
                f"could not find '{part}' under '{node.title}'. "
                f"Available at that level: {available}"
            )
        if len(matches) > 1:
            candidates = ", ".join(f"{m.title!r} ({m.id})" for m in matches)
            raise NotionBrowseError(
                f"'{part}' is ambiguous under '{node.title}': {candidates}. "
                "Use --parent-id to choose unambiguously."
            )
        node = matches[0]
    return node
