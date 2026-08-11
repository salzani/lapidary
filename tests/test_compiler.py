"""Tests for the markdown-to-Notion-blocks compiler: `md_to_blocks` (basic
markdown, lists, code/quote/callout, tables, API limits) and
`plain_rich_text` (used by `notion/metadata.py`, bypassing the parser)."""

from notemcp.notion.compiler import MAX_TEXT_LEN, md_to_blocks, plain_rich_text


def kinds(blocks):
    return [b["type"] for b in blocks]


def plain(block):
    kind = block["type"]
    return "".join(r["text"]["content"] for r in block[kind].get("rich_text", []))


def test_headings_are_capped_at_level_3():
    blocks = md_to_blocks("# um\n\n## dois\n\n### tres\n\n#### quatro")
    assert kinds(blocks) == ["heading_1", "heading_2", "heading_3", "heading_3"]
    assert plain(blocks[3]) == "quatro"


def test_paragraph_and_divider():
    blocks = md_to_blocks("texto\n\n---\n\noutro")
    assert kinds(blocks) == ["paragraph", "divider", "paragraph"]


def test_inline_annotations():
    blocks = md_to_blocks("um **negrito** e *itálico* e `cod` e ~~risca~~")
    rich = blocks[0]["paragraph"]["rich_text"]
    by_text = {r["text"]["content"]: r["annotations"] for r in rich}
    assert by_text["negrito"]["bold"]
    assert by_text["itálico"]["italic"]
    assert by_text["cod"]["code"]
    assert by_text["risca"]["strikethrough"]


def test_link_becomes_rich_text_link():
    blocks = md_to_blocks("veja [aqui](https://exemplo.com) ok")
    rich = blocks[0]["paragraph"]["rich_text"]
    linked = [r for r in rich if r["text"].get("link")]
    assert len(linked) == 1
    assert linked[0]["text"]["content"] == "aqui"
    assert linked[0]["text"]["link"] == {"url": "https://exemplo.com"}


def test_adjacent_same_format_segments_are_merged():
    """Without the merge, the inline walker would generate one rich_text
    entry per fragment instead of a single combined one."""
    blocks = md_to_blocks("aaa bbb ccc ddd")
    assert len(blocks[0]["paragraph"]["rich_text"]) == 1


def test_bullet_and_numbered_lists():
    blocks = md_to_blocks("- a\n- b\n\n1. x\n2. y")
    assert kinds(blocks) == [
        "bulleted_list_item", "bulleted_list_item",
        "numbered_list_item", "numbered_list_item",
    ]


def test_task_list_becomes_to_do():
    """The `[ ]`/`[x]` marker must be stripped out of the rendered text."""
    blocks = md_to_blocks("- [ ] pendente\n- [x] feito")
    assert kinds(blocks) == ["to_do", "to_do"]
    assert blocks[0]["to_do"]["checked"] is False
    assert blocks[1]["to_do"]["checked"] is True
    assert plain(blocks[0]) == "pendente"
    assert plain(blocks[1]) == "feito"


def test_nested_list_becomes_children():
    blocks = md_to_blocks("- pai\n    - filho")
    assert len(blocks) == 1
    children = blocks[0]["bulleted_list_item"]["children"]
    assert [c["type"] for c in children] == ["bulleted_list_item"]
    assert plain(children[0]) == "filho"


def test_depth_beyond_two_levels_is_flattened():
    """The Notion API rejects >2 levels in a single request; the excess
    becomes a sibling instead of raising an error. No content may be lost
    in the flattening."""
    md = "- n1\n    - n2\n        - n3\n            - n4"
    blocks = md_to_blocks(md)

    def max_depth(bs, d=1):
        best = d
        for b in bs:
            kids = b[b["type"]].get("children")
            if kids:
                best = max(best, max_depth(kids, d + 1))
        return best

    assert max_depth(blocks) <= 2
    flat = []

    def walk(bs):
        for b in bs:
            flat.append(plain(b))
            walk(b[b["type"]].get("children") or [])

    walk(blocks)
    assert {"n1", "n2", "n3", "n4"} <= set(flat)


def test_fence_language_is_mapped_and_unknown_falls_back():
    assert md_to_blocks("```py\nx=1\n```")[0]["code"]["language"] == "python"
    assert md_to_blocks("```ts\nx\n```")[0]["code"]["language"] == "typescript"
    assert md_to_blocks("```brainfuck\n+\n```")[0]["code"]["language"] == "plain text"
    assert md_to_blocks("```\nx\n```")[0]["code"]["language"] == "plain text"


def test_fence_content_is_preserved_verbatim():
    block = md_to_blocks("```python\ndef f():\n    return **1**\n```")[0]
    assert plain(block) == "def f():\n    return **1**"


def test_blockquote_becomes_quote():
    blocks = md_to_blocks("> citação aqui")
    assert kinds(blocks) == ["quote"]
    assert plain(blocks[0]) == "citação aqui"


def test_github_alert_becomes_callout():
    blocks = md_to_blocks("> [!WARNING] cuidado com isso")
    assert kinds(blocks) == ["callout"]
    assert blocks[0]["callout"]["icon"] == {"type": "emoji", "emoji": "⚠️"}
    assert plain(blocks[0]) == "cuidado com isso"


def test_table():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    block = md_to_blocks(md)[0]
    assert block["type"] == "table"
    assert block["table"]["table_width"] == 2
    assert block["table"]["has_column_header"] is True
    rows = block["table"]["children"]
    assert len(rows) == 3
    assert rows[2]["table_row"]["cells"][1][0]["text"]["content"] == "4"


def test_long_paragraph_is_split_into_multiple_rich_text():
    """900 repeated words (~7200 chars) must be split across multiple
    rich_text entries, each within the API's per-entry length limit."""
    para = " ".join(["palavra"] * 900)
    rich = md_to_blocks(para)[0]["paragraph"]["rich_text"]
    assert len(rich) > 1
    assert all(len(r["text"]["content"]) <= MAX_TEXT_LEN for r in rich)
    assert "".join(r["text"]["content"] for r in rich) == para


def test_long_code_block_is_split():
    code = "\n".join(f"linha {i}" * 20 for i in range(200))
    rich = md_to_blocks(f"```python\n{code}\n```")[0]["code"]["rich_text"]
    assert len(rich) > 1
    assert all(len(r["text"]["content"]) <= MAX_TEXT_LEN for r in rich)
    assert "".join(r["text"]["content"] for r in rich) == code


def test_empty_input_produces_no_blocks():
    assert md_to_blocks("") == []
    assert md_to_blocks("   \n\n  ") == []


def test_plain_rich_text_splits_at_2000():
    text = "x" * 2500
    rich = plain_rich_text(text)
    assert len(rich) == 2
    assert all(len(r["text"]["content"]) <= MAX_TEXT_LEN for r in rich)
    assert "".join(r["text"]["content"] for r in rich) == text


def test_plain_rich_text_does_not_reinterpret_markdown():
    rich = plain_rich_text("usa **kwargs** e `x`")
    assert len(rich) == 1
    assert rich[0]["text"]["content"] == "usa **kwargs** e `x`"
    assert rich[0]["annotations"]["bold"] is False
    assert rich[0]["annotations"]["code"] is False


def test_plain_rich_text_bold_flag():
    rich = plain_rich_text("Tipo: ", bold=True)
    assert rich[0]["annotations"]["bold"] is True
