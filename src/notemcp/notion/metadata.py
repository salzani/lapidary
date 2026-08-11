"""Metadata for the "loose page" destination: one rule, three serializations.

A page that is a child of `NOTION_PARENT_PAGE_ID` does not have the database
properties (Tipo, Tags, Resumo) — there is nowhere to put them besides the
body. The solution is a callout at the top of the page carrying the same
fields. `metadata_lines()` is the single business rule (which fields, label,
order, when to omit); the three renderers below just format that same list
for whichever destination needs it (API block, MCP Markdown, preview HTML) —
with no round-trip through Markdown, because `summary`/`tags` are free-form
LLM text and may contain `**` or backticks that would be reinterpreted on
the way back in.
"""

from __future__ import annotations

import html as _html
from typing import Any

from ..models import NoteDraft
from .compiler import plain_rich_text

METADATA_ICON = "🏷️"
METADATA_COLOR = "gray_background"

Block = dict[str, Any]


def metadata_lines(draft: NoteDraft) -> list[tuple[str, str]]:
    """Fields of the metadata callout, in the order they appear.

    Empty tags omit the line. No date on purpose (see ESTADO.md/the
    original plan): `NoteDraft` does not carry one, and the product decision
    is not to include it.
    """
    lines: list[tuple[str, str]] = [("Type", draft.doc_type)]
    if draft.tags:
        lines.append(("Tags", ", ".join(draft.tags)))
    lines.append(("Summary", draft.summary))
    return lines


def metadata_callout_block(draft: NoteDraft) -> Block:
    """Notion `callout` block, built directly as a dict (no round-trip)."""
    rich_text: list[dict[str, Any]] = []
    for i, (label, value) in enumerate(metadata_lines(draft)):
        if i:
            rich_text += plain_rich_text("\n")
        rich_text += plain_rich_text(f"{label}: ", bold=True)
        rich_text += plain_rich_text(value)

    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text,
            "icon": {"type": "emoji", "emoji": METADATA_ICON},
            "color": METADATA_COLOR,
        },
    }


def metadata_callout_md(draft: NoteDraft) -> str:
    """Header in GitHub's alert Markdown dialect, for the MCP writer.

    The format is pinned by `test_callout_markdown_is_exactly_this_string`
    (golden string) — this is not decoration, it is the contract the MCP
    server receives.
    """
    body = "\n".join(f"> **{label}:** {value}" for label, value in metadata_lines(draft))
    return f"> [!NOTE]\n{body}"


def metadata_callout_html(draft: NoteDraft) -> str:
    """HTML for the UI preview — `md_to_html` does not work here.

    `md_to_html("> [!NOTE]…")` would render a `<blockquote>` with the literal
    marker, lying about what will actually become a callout in Notion. This
    renderer escapes user text explicitly instead: a summary containing
    `<script>` must not become real HTML.
    """
    rows = "".join(
        f'<div class="callout-meta-row"><strong>{_html.escape(label)}:</strong> '
        f'{_html.escape(value)}</div>'
        for label, value in metadata_lines(draft)
    )
    return f'<div class="callout-meta">{rows}</div>'
