"""Metadata for the "loose page" destination: one rule, three
serializations.

A callout at the top of the page is the only way to show Type/Tags/Summary
when there are no database properties to carry them.
"""

from notemcp.models import NoteDraft
from notemcp.notion.metadata import (
    METADATA_COLOR,
    METADATA_ICON,
    metadata_callout_block,
    metadata_callout_html,
    metadata_callout_md,
    metadata_lines,
)

DRAFT = NoteDraft(
    title="Cache de embeddings",
    doc_type="ideia",
    tags=["infra", "deploy"],
    summary="Guardar embeddings em disco.",
    body_md="corpo",
)

DRAFT_NO_TAGS = DRAFT.model_copy(update={"tags": []})


def test_metadata_lines_omits_empty_tags():
    lines = metadata_lines(DRAFT_NO_TAGS)
    assert lines == [("Tipo", "ideia"), ("Resumo", "Guardar embeddings em disco.")]
    assert "Tags" not in dict(lines)


def test_metadata_lines_includes_tags_when_present():
    lines = metadata_lines(DRAFT)
    assert lines == [
        ("Tipo", "ideia"),
        ("Tags", "infra, deploy"),
        ("Resumo", "Guardar embeddings em disco."),
    ]


def test_summary_with_markdown_chars_is_not_reinterpreted():
    """Summary is free-form LLM text — bold/backticks must not be
    reinterpreted as markdown."""
    draft = DRAFT.model_copy(update={"summary": "usa **kwargs e `x`"})

    block = metadata_callout_block(draft)
    plain = "".join(r["text"]["content"] for r in block["callout"]["rich_text"])
    assert "usa **kwargs e `x`" in plain
    assert not any(r["annotations"]["bold"] and "kwargs" in r["text"]["content"]
                   for r in block["callout"]["rich_text"])

    html = metadata_callout_html(draft)
    assert "usa **kwargs e `x`" in html
    assert "<strong>kwargs" not in html and "<code>x</code>" not in html


def test_the_three_renderers_carry_the_same_values():
    lines = dict(metadata_lines(DRAFT))

    block = metadata_callout_block(DRAFT)
    block_plain = "".join(r["text"]["content"] for r in block["callout"]["rich_text"])
    for label, value in lines.items():
        assert f"{label}: {value}" in block_plain

    md = metadata_callout_md(DRAFT)
    for label, value in lines.items():
        assert f"**{label}:** {value}" in md

    html = metadata_callout_html(DRAFT)
    for label, value in lines.items():
        assert value in html
        assert label in html


def test_callout_markdown_is_exactly_this_string():
    """Golden string: pins the alert dialect that the MCP server receives."""
    expected = (
        "> [!NOTE]\n"
        "> **Tipo:** ideia\n"
        "> **Tags:** infra, deploy\n"
        "> **Resumo:** Guardar embeddings em disco."
    )
    assert metadata_callout_md(DRAFT) == expected


def test_callout_block_is_a_notion_callout():
    block = metadata_callout_block(DRAFT)
    assert block["type"] == "callout"
    assert block["callout"]["icon"] == {"type": "emoji", "emoji": METADATA_ICON}
    assert block["callout"]["color"] == METADATA_COLOR


def test_metadata_html_escapes_user_text():
    draft = DRAFT.model_copy(update={"summary": "<script>alert(1)</script>"})
    html = metadata_callout_html(draft)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
