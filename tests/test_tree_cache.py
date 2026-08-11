"""`CachedBrowser` (Phase 6): TTL, selective invalidation, and
write-through."""

from notemcp.notion.browser import NotionBrowser, PageNode
from notemcp.notion.tree_cache import CachedBrowser


class FakeInner:
    def __init__(self):
        self.list_calls = []
        self.create_calls = []
        self.tree = {
            "root": [PageNode("faculdade", "Faculdade")],
            "faculdade": [PageNode("calc-1", "Cálculo I")],
        }

    def list_child_pages(self, page_id):
        self.list_calls.append(page_id)
        return list(self.tree.get(page_id, []))

    def create_child_page(self, parent_page_id, title):
        self.create_calls.append((parent_page_id, title))
        node = PageNode(f"new-{len(self.create_calls)}", title)
        return node


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_cached_browser_satisfies_the_protocol():
    assert isinstance(CachedBrowser(FakeInner()), NotionBrowser)


def test_hit_does_not_call_the_inner():
    inner = FakeInner()
    cache = CachedBrowser(inner)

    first = cache.list_child_pages("root")
    second = cache.list_child_pages("root")

    assert first == second == [PageNode("faculdade", "Faculdade")]
    assert inner.list_calls == ["root"], "segunda chamada não pode ter tocado o inner"


def test_ttl_expires_with_an_injected_clock():
    """No `sleep`: the clock is injected and advanced manually. 299s in is
    still within the 300s TTL (hit); advancing 2s more brings the total to
    301s, past the TTL, forcing a refetch."""
    inner = FakeInner()
    clock = FakeClock()
    cache = CachedBrowser(inner, ttl_seconds=300.0, clock=clock)

    cache.list_child_pages("root")
    clock.advance(299.0)
    cache.list_child_pages("root")
    assert inner.list_calls == ["root"], "dentro do TTL, ainda é hit"

    clock.advance(2.0)
    cache.list_child_pages("root")
    assert inner.list_calls == ["root", "root"], "TTL vencido precisa refazer o fetch"


def test_is_cached_reports_hit_and_miss():
    inner = FakeInner()
    cache = CachedBrowser(inner)
    assert not cache.is_cached("root")
    cache.list_child_pages("root")
    assert cache.is_cached("root")


def test_invalidate_a_single_node_does_not_touch_the_others():
    inner = FakeInner()
    cache = CachedBrowser(inner)
    cache.list_child_pages("root")
    cache.list_child_pages("faculdade")

    cache.invalidate("root")

    assert not cache.is_cached("root")
    assert cache.is_cached("faculdade")


def test_invalidate_everything_drops_all_nodes():
    inner = FakeInner()
    cache = CachedBrowser(inner)
    cache.list_child_pages("root")
    cache.list_child_pages("faculdade")

    cache.invalidate()

    assert not cache.is_cached("root")
    assert not cache.is_cached("faculdade")


def test_invalidating_a_node_does_not_invalidate_its_ancestor():
    """Deliberately counter-intuitive: 'Faculdade's list of children does
    not need to change just because a grandchild was born under
    'Cálculo I'."""
    inner = FakeInner()
    cache = CachedBrowser(inner)
    cache.list_child_pages("root")
    cache.list_child_pages("faculdade")

    cache.invalidate("faculdade")

    assert cache.is_cached("root"), "invalidar o filho não pode derrubar o pai"
    assert not cache.is_cached("faculdade")


def test_created_page_appears_without_a_second_fetch():
    """Write-through: creating a page inserts into the parent's cache
    without a refetch — a refetch would depend on Notion's immediate
    consistency (bug 4). The first `list_child_pages` call below only
    exists to populate the "faculdade" cache entry before creating."""
    inner = FakeInner()
    cache = CachedBrowser(inner)
    cache.list_child_pages("faculdade")

    created = cache.create_child_page("faculdade", "Cálculo II")

    children = cache.list_child_pages("faculdade")
    assert created in children
    assert [c.title for c in children] == ["Cálculo I", "Cálculo II"]
    assert inner.list_calls == ["faculdade"], "não pode ter refetchado depois de criar"


def test_created_page_in_an_uncached_parent_does_not_raise():
    """Creating inside a node that was never listed (cache miss) must not
    break — there's just nothing to update in-place."""
    inner = FakeInner()
    cache = CachedBrowser(inner)
    node = cache.create_child_page("faculdade", "Cálculo II")
    assert node.title == "Cálculo II"
    assert not cache.is_cached("faculdade")


def test_write_through_keeps_the_original_timestamp():
    """The level's TTL is not reset by a creation — only the content
    changes. Advancing 9s then 2s more brings the total to 11s since the
    original fetch, past the 10s TTL."""
    inner = FakeInner()
    clock = FakeClock()
    cache = CachedBrowser(inner, ttl_seconds=10.0, clock=clock)

    cache.list_child_pages("faculdade")
    clock.advance(9.0)
    cache.create_child_page("faculdade", "Cálculo II")
    clock.advance(2.0)

    cache.list_child_pages("faculdade")
    assert inner.list_calls == ["faculdade", "faculdade"], "timestamp preservado: TTL vence normalmente"
