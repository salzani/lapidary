"""Page-tree cache (Phase 6): short TTL, write-through, one level per key.

Exists only for the UI: reopening a level the user already visited in this
session should not cost another network call. This is not about
correctness — it is about not making the parent-page picker feel like it
freezes on every click.

The CLI does not use this: it is single-shot and would never get a hit —
`notion/browser.build_browser` returns the raw adapter. Only `ui/bridge.py`
wraps it with `CachedBrowser`, one instance per session.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .browser import NotionBrowser, PageNode


class CachedBrowser:
    """Decorator satisfying `NotionBrowser`, caching `list_child_pages`
    per `page_id` with a short TTL.

    One entry per LEVEL (not the whole tree): opening "Faculdade" only caches
    its direct children, not the grandchildren — that is the granularity at
    which the UI requests data.
    """

    def __init__(
        self,
        inner: NotionBrowser,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, list[PageNode]]] = {}
        """page_id -> (fetch timestamp, children)."""
        self._lock = threading.Lock()

    def list_child_pages(self, page_id: str) -> list[PageNode]:
        """Return `page_id`'s children, from cache if still within TTL.

        The network call happens OUTSIDE the lock: holding the lock during
        the writer's backoff retries would serialize the whole UI. Accepted
        and documented consequence: two simultaneous clicks on the same node
        can trigger two fetches — the read is idempotent, the last write to
        the dict wins.
        """
        with self._lock:
            entry = self._entries.get(page_id)
        if entry is not None:
            timestamp, nodes = entry
            if self._clock() - timestamp < self._ttl:
                return nodes

        nodes = self._inner.list_child_pages(page_id)
        with self._lock:
            self._entries[page_id] = (self._clock(), nodes)
        return nodes

    def create_child_page(self, parent_page_id: str, title: str) -> PageNode:
        """Create a child page and update the parent's cached entry in place.

        Write-through: appends the new node at the end and KEEPS the
        existing timestamp — never a refetch. A refetch here would depend on
        `blocks.children.list` already seeing the freshly created page,
        exactly the assumption that caused bug 4 (Notion index lag): if the
        read lagged at all behind `pages.create`, the list would come back
        without the new page and the user would create a duplicate.
        """
        node = self._inner.create_child_page(parent_page_id, title)
        with self._lock:
            entry = self._entries.get(parent_page_id)
            if entry is not None:
                timestamp, nodes = entry
                self._entries[parent_page_id] = (timestamp, [*nodes, node])
        return node

    def invalidate(self, page_id: str | None = None) -> None:
        """`None` drops everything; an id drops only that level.

        "Reload" in the UI calls `invalidate(current_node)` and redoes only
        that fetch. ANCESTORS are deliberately NOT invalidated: the list of
        "Faculdade"'s children does not change just because a grandchild was
        born under "Cálculo I". This is counterintuitive — do not "fix" it
        into invalidating everything on every change.
        """
        with self._lock:
            if page_id is None:
                self._entries.clear()
            else:
                self._entries.pop(page_id, None)

    def is_cached(self, page_id: str) -> bool:
        with self._lock:
            return page_id in self._entries
