"""Notion REST API adapter (notion-client)."""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator, TypeVar

import httpx

from ..models import Destination, DestinationKind, NoteDraft, PageRef
from .base import NotionWriteError, page_destination
from .browser import PageNode
from .compiler import md_to_blocks
from .metadata import metadata_callout_block
from .schema import (
    DATABASE_PROPERTIES,
    DATABASE_TITLE,
    page_properties,
    page_title_properties,
    title_properties,
)

MAX_CHILDREN_PER_REQUEST = 100
MAX_TRANSPORT_RETRIES = 4

T = TypeVar("T")


def _network_message(exc: Exception) -> str:
    """Build a network-failure message, kept distinct from a permission failure.

    Suggesting "check whether you shared the page" for a dropped connection
    sends the reader off to debug the wrong thing (bug 6).
    """
    return (
        f"network failure talking to the Notion API after {MAX_TRANSPORT_RETRIES} "
        f"attempts: {exc}\n"
        "This is a connectivity issue, not a permission one — your configuration "
        "is fine. Try again in a moment."
    )


def _retry(fn: Callable[[], T], *, idempotent: bool) -> T:
    """Retry transport failures that notion-client does not cover (bug 5).

    The SDK's `_execute_with_retry` only retries *API* errors (429, 5xx) — an
    httpx `ConnectError` or `RemoteProtocolError` propagates straight up and
    aborts the publish.

    The `idempotent` distinction matters: `ConnectError` means the connection
    was never established, so the request never reached the server and
    retrying is safe. Other transport errors (e.g. the server disconnected
    without responding) are ambiguous — the request may have already been
    processed. Retrying a `pages.create` in that case would create a
    duplicate page, so only reads retry in that situation.
    """
    errors: tuple[type[Exception], ...] = (
        (httpx.TransportError,) if idempotent else (httpx.ConnectError,)
    )
    for attempt in range(MAX_TRANSPORT_RETRIES):
        try:
            return fn()
        except errors:
            if attempt == MAX_TRANSPORT_RETRIES - 1:
                raise
            time.sleep(1.0 * 2**attempt)
    raise AssertionError("unreachable")


def chunk_children(blocks: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """Split `blocks` into batches of at most 100, the API's request limit.

    The first batch goes along with `pages.create`; the rest become
    sequential `blocks.children.append` calls (order matters, do not
    parallelize).
    """
    for i in range(0, len(blocks), MAX_CHILDREN_PER_REQUEST):
        yield blocks[i : i + MAX_CHILDREN_PER_REQUEST]


class NotionApiWriter:
    def __init__(self, token: str, client: Any | None = None):
        if client is not None:
            self.client = client
        else:
            from notion_client import Client

            self.client = Client(auth=token)

    def ensure_destination(self, parent_page_id: str, kind: DestinationKind) -> Destination:
        """Resolve the destination for notes, creating it if necessary.

        `PAGE` never touches the network: it is `parent_page_id` itself,
        resolved by `page_destination`. Only `DATABASE` does
        discovery/creation — since the 2025-09-03 API change, a database is
        a container of data sources, and it is the data source, not the
        database, that receives pages (bug 1).

        The properties passed to `databases.create` MUST go inside
        `initial_data_source={"properties": ...}`, not as a top-level
        `properties=` argument — the SDK silently discards a top-level
        `properties=` and creates a database with only the Name field, no
        error raised (bug 1).
        """
        if kind is DestinationKind.PAGE:
            return page_destination(parent_page_id)

        database_id = self._find_database_block(parent_page_id)
        if database_id:
            data_source_id = self._first_data_source(database_id)
            self._ensure_properties(data_source_id)
            return Destination(kind=DestinationKind.DATABASE, id=data_source_id)

        try:
            created = _retry(
                lambda: self.client.databases.create(
                    parent={"type": "page_id", "page_id": parent_page_id},
                    title=[{"type": "text", "text": {"content": DATABASE_TITLE}}],
                    initial_data_source={"properties": DATABASE_PROPERTIES},
                ),
                idempotent=False,
            )
        except httpx.TransportError as exc:
            raise NotionWriteError(_network_message(exc)) from exc
        except Exception as exc:
            raise NotionWriteError(
                f"could not create the database at {parent_page_id!r}: {exc}\n"
                "Check whether the parent page was shared with the integration "
                "(··· -> Connections)."
            ) from exc

        sources = created.get("data_sources") or []
        data_source_id = sources[0]["id"] if sources else self._first_data_source(created["id"])
        return Destination(kind=DestinationKind.DATABASE, id=data_source_id)

    def _find_database_block(self, parent_page_id: str) -> str | None:
        """Look for an existing "Notas" database among the page's children.

        Done via `blocks.children.list` instead of `search` on purpose: the
        Notion search index is slow to update, and a freshly created
        database does not show up in it — which would make every run create
        one more duplicate (bug 4).
        """
        cursor = None
        try:
            while True:
                page = _retry(
                    lambda c=cursor: self.client.blocks.children.list(
                        block_id=parent_page_id, start_cursor=c
                    ),
                    idempotent=True,
                )
                for block in page.get("results", []):
                    if block.get("type") != "child_database":
                        continue
                    if block["child_database"].get("title", "").strip() == DATABASE_TITLE:
                        return block["id"]
                if not page.get("has_more"):
                    return None
                cursor = page.get("next_cursor")
        except httpx.TransportError as exc:
            raise NotionWriteError(_network_message(exc)) from exc
        except Exception as exc:
            raise NotionWriteError(
                f"could not read page {parent_page_id!r}: {exc}\n"
                "Check the id and whether the page was shared with the integration "
                "(··· -> Connections)."
            ) from exc

    def _first_data_source(self, database_id: str) -> str:
        db = _retry(
            lambda: self.client.databases.retrieve(database_id=database_id),
            idempotent=True,
        )
        sources = db.get("data_sources") or []
        if not sources:
            raise NotionWriteError(
                f"database {database_id!r} has no data source"
            )
        return sources[0]["id"]

    def _ensure_properties(self, data_source_id: str) -> None:
        """Add any properties missing from an already-existing data source.

        Covers databases created by an earlier version (or by hand) that
        lack Tipo/Tags/etc — without this, `create_note` would fail on the
        missing property. `idempotent=True` because adding a property that
        already exists is a no-op, so retrying is safe.
        """
        current = _retry(
            lambda: self.client.data_sources.retrieve(data_source_id=data_source_id),
            idempotent=True,
        )
        missing = {
            name: spec
            for name, spec in DATABASE_PROPERTIES.items()
            if name not in current.get("properties", {}) and "title" not in spec
        }
        if missing:
            _retry(
                lambda: self.client.data_sources.update(
                    data_source_id=data_source_id, properties=missing
                ),
                idempotent=True,
            )

    def list_child_pages(self, page_id: str) -> list[PageNode]:
        """Direct subpages of `page_id`, in document order.

        `NotionBrowser` port implementation (Phase 6). `child_database` is
        ignored on purpose (risk 5). Paginates to the end: a truncated list
        would hide a subject/course (risk 4).
        """
        nodes: list[PageNode] = []
        cursor = None
        try:
            while True:
                page = _retry(
                    lambda c=cursor: self.client.blocks.children.list(
                        block_id=page_id, start_cursor=c
                    ),
                    idempotent=True,
                )
                for block in page.get("results", []):
                    if block.get("type") != "child_page":
                        continue
                    title = block.get("child_page", {}).get("title", "")
                    nodes.append(PageNode(id=block["id"], title=title))
                if not page.get("has_more"):
                    break
                cursor = page.get("next_cursor")
        except httpx.TransportError as exc:
            raise NotionWriteError(_network_message(exc)) from exc
        except Exception as exc:
            raise NotionWriteError(
                f"could not list the subpages of {page_id!r}: {exc}\n"
                "Check the id and whether the page was shared with the integration "
                "(··· -> Connections)."
            ) from exc
        return nodes

    def create_child_page(self, parent_page_id: str, title: str) -> PageNode:
        """Create an empty (title-only) page as a child of `parent_page_id`.

        `idempotent=False`: same rule as `pages.create` for a note. A
        `RemoteProtocolError` is caught BEFORE the broader `TransportError`
        because it is a subclass of it — the order matters, a more general
        `except` first would swallow it silently. It is ambiguous: the
        server may have processed the request before dropping the
        connection — so the message tells the user to reload rather than
        suggesting an immediate retry (risk 12: a re-click would duplicate
        the page).
        """
        try:
            page = _retry(
                lambda: self.client.pages.create(
                    parent={"type": "page_id", "page_id": parent_page_id},
                    properties=title_properties(title),
                ),
                idempotent=False,
            )
        except httpx.RemoteProtocolError as exc:
            raise NotionWriteError(
                f"the connection dropped after sending the request to create '{title}' — "
                "the page MAY have been created. Click Reload before trying "
                f"again. ({exc})"
            ) from exc
        except httpx.TransportError as exc:
            raise NotionWriteError(_network_message(exc)) from exc
        except Exception as exc:
            raise NotionWriteError(
                f"could not create page '{title}' under {parent_page_id!r}: {exc}"
            ) from exc
        return PageNode(id=page["id"], title=title)

    def create_note(self, destination: Destination, draft: NoteDraft) -> PageRef:
        """Compile `draft.body_md` to blocks and create the Notion page.

        For a PAGE destination, the metadata callout is prepended BEFORE
        `chunk_children` splits the blocks — appending it afterward could
        push a document with exactly 100 blocks to 101 children, and the API
        would reject the request.

        On failure for a PAGE destination, the error is translated, not just
        forwarded: a 404/archived error from the API does not say why, and a
        bare "failed to create the page" would send the user off to debug the
        wrong thing — the most common cause is the parent page having
        disappeared from Notion between selection and publish (see
        ESTADO.md §6, Phase 6).
        """
        blocks = md_to_blocks(draft.body_md)
        if destination.kind is DestinationKind.PAGE:
            blocks = [metadata_callout_block(draft), *blocks]
        batches = list(chunk_children(blocks))
        first = batches[0] if batches else []

        if destination.kind is DestinationKind.PAGE:
            parent: dict[str, Any] = {"type": "page_id", "page_id": destination.id}
            properties = page_title_properties(draft)
        else:
            parent = {"type": "data_source_id", "data_source_id": destination.id}
            """`{"database_id": ...}` has been rejected since the 2025-09-03
            API change (bug 2)."""
            properties = page_properties(draft)

        try:
            page = _retry(
                lambda: self.client.pages.create(
                    parent=parent,
                    properties=properties,
                    children=first,
                ),
                idempotent=False,
            )
        except httpx.TransportError as exc:
            raise NotionWriteError(_network_message(exc)) from exc
        except Exception as exc:
            if destination.kind is DestinationKind.PAGE:
                raise NotionWriteError(
                    f"failed to create the page: {exc}\n"
                    f"the destination page ({destination.id!r}) may have been "
                    "deleted, archived, or unshared from the integration; "
                    "choose another one in Destination › Page."
                ) from exc
            raise NotionWriteError(f"failed to create the page: {exc}") from exc

        page_id = page["id"]
        for batch in batches[1:]:
            try:
                _retry(
                    lambda b=batch: self.client.blocks.children.append(
                        block_id=page_id, children=b
                    ),
                    idempotent=False,
                )
            except Exception as exc:
                raise NotionWriteError(
                    f"the page was created ({page.get('url')}) but appending "
                    f"the rest of the content failed: {exc}"
                ) from exc

        return PageRef(id=page_id, url=page.get("url", ""))
