"""Adapter that writes to Notion through the official MCP server.

Key difference from `NotionApiWriter`: the MCP server exposes
`API-update-page-markdown`, which accepts **raw Markdown**. So this adapter
does not use `notion/compiler.py` — the draft's `body_md` is sent as-is, and
translation to blocks happens on the server side.

This is a trade-off, not an upgrade:
  - in favor: much less code, and syntax our compiler does not implement
    (equations, columns) starts working;
  - against: we lose fine-grained control over the blocks and the guarantee
    that the UI preview matches the result, since the parser is now the
    server's, not ours.

That is why `NOTION_WRITER=api` remains the default.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import threading
from typing import Any

from ..models import Destination, DestinationKind, NoteDraft, PageRef
from .base import NotionWriteError, page_destination
from .browser import PageNode
from .metadata import metadata_callout_md
from .schema import (
    DATABASE_PROPERTIES,
    DATABASE_TITLE,
    page_properties,
    page_title_properties,
    title_properties,
)

MCP_COMMAND = "npx"
MCP_ARGS = ("-y", "@notionhq/notion-mcp-server")
STARTUP_TIMEOUT = 90.0
"""npx may download the package on the first run, so this is generous."""
CALL_TIMEOUT = 120.0


class _McpSession:
    """Persistent MCP session, running its own event loop on a daemon thread.

    The `NotionWriter` port is synchronous (the CLI is synchronous, and the
    UI already runs the writer on a QThreadPool thread), but the MCP SDK is
    async. Instead of one `asyncio.run()` per call — which would spin up and
    tear down the `npx` process every time, ~2-4s of overhead each — the
    loop stays alive and calls enter it via `run_coroutine_threadsafe`.
    """

    def __init__(self, token: str):
        self._token = token
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stop: asyncio.Event | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="notemcp-mcp", daemon=True
            )
            self._thread.start()

        if not self._ready.wait(STARTUP_TIMEOUT):
            raise NotionWriteError(
                f"the MCP server did not respond within {STARTUP_TIMEOUT:.0f}s. "
                f"Check whether Node is installed (`node --version`) — on the first "
                f"run npx downloads @notionhq/notion-mcp-server."
            )
        if self._error is not None:
            raise NotionWriteError(f"failed to start the MCP server: {self._error}")

    def _run(self) -> None:
        """Entry point for the daemon thread; owns the event loop's lifetime.

        Catches `BaseException`, not just `Exception`: this runs on a
        background thread, so any failure must be captured here and
        surfaced to the calling thread via `self._error` instead of being
        lost when the thread dies.
        """
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self._error = exc
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        """Open the stdio session and keep the owning task alive until `close()`.

        Forwards the full `os.environ`, not just `NOTION_TOKEN`, because
        `npx` needs `PATH`/`HOME` to find node. `stdio_client` and
        `ClientSession` must be closed in the same task that opened them, so
        this coroutine blocks on `self._stop` instead of returning right
        after `initialize()` — that keeps the owning task alive until
        `close()` asks it to stop.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=MCP_COMMAND,
            args=list(MCP_ARGS),
            env={**os.environ, "NOTION_TOKEN": self._token},
        )
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()

    def close(self) -> None:
        if self._loop and self._stop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=10)

    def call(self, tool: str, arguments: dict) -> dict:
        """Call a tool and return its response already parsed as JSON."""
        self.start()
        if self._session is None or self._loop is None:
            raise NotionWriteError("MCP session unavailable")

        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, arguments), self._loop
        )
        try:
            result = future.result(timeout=CALL_TIMEOUT)
        except TimeoutError as exc:
            raise NotionWriteError(f"{CALL_TIMEOUT:.0f}s timeout calling tool {tool}") from exc
        except Exception as exc:
            raise NotionWriteError(f"error calling {tool}: {exc}") from exc

        text = _first_text(result)
        if result.is_error:
            raise NotionWriteError(f"{tool} failed: {text[:500]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise NotionWriteError(
                f"{tool} returned something that is not JSON: {text[:300]}"
            ) from exc


def _first_text(result: Any) -> str:
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            return text
    return ""


class NotionMcpWriter:
    def __init__(self, token: str, session: Any | None = None):
        if not token and session is None:
            raise NotionWriteError("NOTION_TOKEN is not set")
        self.session = session or _McpSession(token)
        atexit.register(self.close)

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if close:
            close()

    def ensure_destination(self, parent_page_id: str, kind: DestinationKind) -> Destination:
        if kind is DestinationKind.PAGE:
            return page_destination(parent_page_id)

        database_id = self._find_database(parent_page_id)
        if database_id:
            db = self.session.call("API-retrieve-a-database", {"database_id": database_id})
            sources = db.get("data_sources") or []
            if sources:
                return Destination(kind=DestinationKind.DATABASE, id=sources[0]["id"])
            raise NotionWriteError(f"database {database_id!r} has no data source")

        created = self.session.call(
            "API-create-a-data-source",
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": DATABASE_TITLE}}],
                "properties": DATABASE_PROPERTIES,
            },
        )
        return Destination(kind=DestinationKind.DATABASE, id=created["id"])

    def _find_database(self, parent_page_id: str) -> str | None:
        """Same strategy as the API writer: scan the page's children instead
        of using search, because the Notion index is slow to update and a
        freshly created database would not show up in it (bug 4)."""
        cursor = None
        while True:
            args: dict = {"block_id": parent_page_id}
            if cursor:
                args["start_cursor"] = cursor
            page = self.session.call("API-get-block-children", args)

            for block in page.get("results", []):
                if block.get("type") != "child_database":
                    continue
                if block["child_database"].get("title", "").strip() == DATABASE_TITLE:
                    return block["id"]

            if not page.get("has_more"):
                return None
            cursor = page.get("next_cursor")

    def list_child_pages(self, page_id: str) -> list[PageNode]:
        """Direct subpages of `page_id`, in document order.

        `NotionBrowser` port implementation (Phase 6). Same pagination
        strategy as `_find_database`: drains `has_more` to the end (risk 4).
        `child_database` is ignored (risk 5).
        """
        nodes: list[PageNode] = []
        cursor = None
        while True:
            args: dict = {"block_id": page_id}
            if cursor:
                args["start_cursor"] = cursor
            page = self.session.call("API-get-block-children", args)

            for block in page.get("results", []):
                if block.get("type") != "child_page":
                    continue
                title = block.get("child_page", {}).get("title", "")
                nodes.append(PageNode(id=block["id"], title=title))

            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        return nodes

    def create_child_page(self, parent_page_id: str, title: str) -> PageNode:
        """Create an empty (title-only) page as a child of `parent_page_id`.

        A single call, no `API-update-page-markdown` — a parent page has no
        body, so there is nothing left to write after creation.
        """
        page = self.session.call(
            "API-post-page",
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "properties": title_properties(title),
            },
        )
        return PageNode(id=page["id"], title=title)

    def create_note(self, destination: Destination, draft: NoteDraft) -> PageRef:
        """Create the page, then write its body as Markdown.

        The body goes as raw Markdown, without passing through the
        compiler — the key difference from the API writer.

        For a PAGE destination, the metadata callout header is concatenated
        into a single string ahead of the body rather than sent as a
        separate call: `API-post-page` with `children=[callout]` would be
        wiped out by the `replace_content` call that follows it, and the
        REST equivalent of `API-patch-block-children` only supports
        appending `after` an existing block — the callout would land at the
        end, not the top.
        """
        if destination.kind is DestinationKind.PAGE:
            parent = {"type": "page_id", "page_id": destination.id}
            properties = page_title_properties(draft)
            body = f"{metadata_callout_md(draft)}\n\n{draft.body_md}"
        else:
            parent = {"type": "data_source_id", "data_source_id": destination.id}
            properties = page_properties(draft)
            body = draft.body_md

        page = self.session.call(
            "API-post-page",
            {"parent": parent, "properties": properties},
        )
        page_id = page["id"]

        self.session.call(
            "API-update-page-markdown",
            {
                "page_id": page_id,
                "type": "replace_content",
                "replace_content": {"new_str": body},
            },
        )
        return PageRef(id=page_id, url=page.get("url", ""))
