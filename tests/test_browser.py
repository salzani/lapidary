"""Tests for the `NotionBrowser` port: `resolve_page_path` and
`match_by_title`, entirely offline (no network calls)."""

import pytest

from notemcp.notion.browser import (
    NotionBrowseError,
    PageNode,
    ROOT_LABEL,
    match_by_title,
    resolve_page_path,
)

ROOT = "root-id"


class FakeBrowser:
    """Fixed in-memory tree:

    root
      Faculdade
        Cálculo I
          Prova 2
        Cálculo II
      Trabalho
      Duplicadas
        Projeto X (id dup-1)
        PROJETO X!! (id dup-2, same slug -> ambiguous)
    """

    def __init__(self):
        self.tree = {
            ROOT: [
                PageNode("faculdade", "Faculdade"),
                PageNode("trabalho", "Trabalho"),
                PageNode("duplicadas", "Duplicadas"),
            ],
            "faculdade": [
                PageNode("calc-1", "Cálculo I"),
                PageNode("calc-2", "Cálculo II"),
            ],
            "calc-1": [PageNode("prova-2", "Prova 2")],
            "calc-2": [],
            "trabalho": [],
            "prova-2": [],
            "duplicadas": [
                PageNode("dup-1", "Projeto X"),
                PageNode("dup-2", "PROJETO X!!"),
            ],
            "dup-1": [],
            "dup-2": [],
        }
        self.calls = []

    def list_child_pages(self, page_id):
        self.calls.append(page_id)
        return list(self.tree.get(page_id, []))

    def create_child_page(self, parent_page_id, title):
        """Not exercised by this test module; every test here only browses."""
        raise AssertionError("não usado neste teste")


def test_page_node_is_frozen():
    node = PageNode("id-1", "Título")
    with pytest.raises(Exception):
        node.title = "outro"


def test_match_by_title_ignores_accent_case_and_punctuation():
    nodes = [PageNode("a", "Cálculo I"), PageNode("b", "Trabalho")]
    assert [n.id for n in match_by_title(nodes, "CALCULO i")] == ["a"]
    assert [n.id for n in match_by_title(nodes, "calculo-i!!")] == ["a"]


def test_match_by_title_with_no_match_is_empty():
    nodes = [PageNode("a", "Cálculo I")]
    assert match_by_title(nodes, "Química") == []


def test_resolve_page_path_with_empty_parts_returns_the_root():
    browser = FakeBrowser()
    node = resolve_page_path(browser, ROOT, [])
    assert node == PageNode(ROOT, ROOT_LABEL)
    assert browser.calls == [], "caminho vazio não pode tocar a rede"


def test_resolve_page_path_resolves_a_deep_path():
    browser = FakeBrowser()
    node = resolve_page_path(browser, ROOT, ["Faculdade", "Cálculo I", "Prova 2"])
    assert node == PageNode("prova-2", "Prova 2")


def test_resolve_page_path_not_found_lists_the_level_titles():
    browser = FakeBrowser()
    with pytest.raises(NotionBrowseError) as exc:
        resolve_page_path(browser, ROOT, ["Faculdade", "Química"])
    msg = str(exc.value)
    assert "Química" in msg
    assert "Cálculo I" in msg and "Cálculo II" in msg


def test_resolve_page_path_never_falls_back_to_the_root_when_not_found():
    browser = FakeBrowser()
    with pytest.raises(NotionBrowseError):
        resolve_page_path(browser, ROOT, ["Não existe"])


def test_resolve_page_path_ambiguous_lists_the_candidate_ids():
    """Two pages with the same slug under the same parent: a hard error,
    never a 'best match' (risk #10)."""
    browser = FakeBrowser()
    with pytest.raises(NotionBrowseError) as exc:
        resolve_page_path(browser, ROOT, ["Duplicadas", "Projeto X"])
    msg = str(exc.value)
    assert "dup-1" in msg and "dup-2" in msg
    assert "--parent-id" in msg


def test_resolve_page_path_root_label_is_synthetic():
    """The root is not resolved by any call — the label is fixed, without
    depending on an unverified `pages.retrieve`/`API-retrieve-a-page`."""
    browser = FakeBrowser()
    with pytest.raises(NotionBrowseError) as exc:
        resolve_page_path(browser, ROOT, ["Não existe"])
    assert ROOT_LABEL in str(exc.value)
