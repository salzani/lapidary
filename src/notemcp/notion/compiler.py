"""Deterministic compiler: Markdown -> Notion API blocks.

This is the piece that exists so the LLM *does not* have to emit Notion's
block JSON. It returns Markdown (which models write well), and the
translation here is purely mechanical — no network, no state, 100%
testable.

Notion API constraints handled here:
  - a rich_text's ``text.content`` has a 2000-char limit (split automatically)
  - only 2 levels of nesting are accepted in a single request (deeper levels
    are flattened onto the parent)
The 100-children-per-request limit belongs to the writer, not here — see
``api_writer.chunk_children``.
"""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

MAX_TEXT_LEN = 2000
MAX_NEST_DEPTH = 2

Block = dict[str, Any]
RichText = dict[str, Any]

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])
"""Explicitly commonmark plus the two extensions in our subset. The
"gfm-like" preset would also work, but it turns on `linkify`, which requires
the linkify-it-py dependency."""

_NOTION_LANGUAGES = {
    "bash", "c", "c#", "c++", "css", "diff", "docker", "elixir", "go",
    "graphql", "haskell", "html", "java", "javascript", "json", "kotlin",
    "lua", "makefile", "markdown", "matlab", "nix", "objective-c", "ocaml",
    "perl", "php", "plain text", "powershell", "python", "r", "ruby", "rust",
    "scala", "shell", "sql", "swift", "toml", "typescript", "vb.net", "xml",
    "yaml",
}
"""Languages accepted by Notion's `code` block (the subset used in practice)."""
_LANGUAGE_ALIASES = {
    "sh": "shell", "zsh": "shell", "console": "shell", "bash": "bash",
    "py": "python", "py3": "python", "js": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript", "yml": "yaml",
    "dockerfile": "docker", "cs": "c#", "csharp": "c#", "cpp": "c++",
    "golang": "go", "rs": "rust", "rb": "ruby", "md": "markdown",
    "text": "plain text", "txt": "plain text", "": "plain text",
}

_CALLOUT_KINDS = {
    "note": "📝", "tip": "💡", "important": "❗",
    "warning": "⚠️", "caution": "🚨", "info": "ℹ️",
}
"""`> [!NOTE]`-style markers map to a callout, keyed to the icon emoji."""


def _annotations(
    *, bold: bool = False, italic: bool = False,
    strikethrough: bool = False, code: bool = False,
) -> dict[str, Any]:
    return {
        "bold": bold,
        "italic": italic,
        "strikethrough": strikethrough,
        "underline": False,
        "code": code,
        "color": "default",
    }


def _split(content: str, limit: int = MAX_TEXT_LEN) -> list[str]:
    """Split into chunks of at most `limit` chars, preferring to cut at a
    newline or space so a word is not broken in the middle."""
    if len(content) <= limit:
        return [content]
    parts: list[str] = []
    rest = content
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    return parts


def _text_segments(
    content: str, annotations: dict[str, Any], href: str | None = None
) -> list[RichText]:
    """Turn a chunk of text into 1+ rich_text objects, respecting the 2000-char limit."""
    if not content:
        return []
    out: list[RichText] = []
    for chunk in _split(content):
        item: RichText = {
            "type": "text",
            "text": {"content": chunk},
            "annotations": annotations,
        }
        if href:
            item["text"]["link"] = {"url": href}
        out.append(item)
    return out


def _rich_text(inline: Token | None) -> list[RichText]:
    """Convert a markdown-it `inline` token into Notion rich_text.

    An image in the middle of a paragraph has no inline equivalent in
    Notion, so it becomes a link with the alt text as its label.
    """
    if inline is None or not inline.children:
        return []

    segments: list[RichText] = []
    bold = italic = strike = False
    href: str | None = None

    for child in inline.children:
        t = child.type
        if t == "text":
            segments += _text_segments(
                child.content, _annotations(bold=bold, italic=italic, strikethrough=strike), href
            )
        elif t == "code_inline":
            segments += _text_segments(
                child.content,
                _annotations(bold=bold, italic=italic, strikethrough=strike, code=True),
                href,
            )
        elif t == "strong_open":
            bold = True
        elif t == "strong_close":
            bold = False
        elif t == "em_open":
            italic = True
        elif t == "em_close":
            italic = False
        elif t == "s_open":
            strike = True
        elif t == "s_close":
            strike = False
        elif t == "link_open":
            href = child.attrGet("href")
        elif t == "link_close":
            href = None
        elif t in ("softbreak", "hardbreak"):
            segments += _text_segments("\n", _annotations())
        elif t == "image":
            alt = child.content or child.attrGet("src") or "image"
            segments += _text_segments(alt, _annotations(), child.attrGet("src"))

    return _merge_adjacent(segments)


def _merge_adjacent(segments: list[RichText]) -> list[RichText]:
    """Merge neighboring segments that share the same formatting.

    The inline walker produces many small fragments; without this the
    resulting JSON is needlessly bloated.
    """
    merged: list[RichText] = []
    for seg in segments:
        if merged:
            prev = merged[-1]
            same_ann = prev["annotations"] == seg["annotations"]
            same_link = prev["text"].get("link") == seg["text"].get("link")
            fits = len(prev["text"]["content"]) + len(seg["text"]["content"]) <= MAX_TEXT_LEN
            if same_ann and same_link and fits:
                prev["text"]["content"] += seg["text"]["content"]
                continue
        merged.append(seg)
    return merged


def _block(kind: str, payload: dict[str, Any]) -> Block:
    return {"object": "block", "type": kind, kind: payload}


def _matching_close(tokens: list[Token], open_idx: int) -> int:
    """Index of the token that closes the container opened at `open_idx`.

    Uses `Token.nesting` (+1 opens, -1 closes, 0 self-closing), so it works
    for any nesting depth without knowing the types involved.
    """
    depth = 0
    for j in range(open_idx, len(tokens)):
        depth += tokens[j].nesting
        if depth == 0:
            return j
    return len(tokens) - 1


def _parse_sequence(tokens: list[Token]) -> list[Block]:
    """Convert a balanced sequence of block tokens.

    Heading levels are capped at h3 (`min(level, 3)`) because Notion has no
    h4+ block type.
    """
    blocks: list[Block] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        kind = tok.type

        if kind == "heading_open":
            close = _matching_close(tokens, i)
            level = min(int(tok.tag[1:]), 3)
            blocks.append(
                _block(f"heading_{level}", {"rich_text": _rich_text(tokens[i + 1])})
            )
            i = close + 1

        elif kind == "paragraph_open":
            close = _matching_close(tokens, i)
            blocks += _parse_paragraph(tokens[i + 1])
            i = close + 1

        elif kind in ("bullet_list_open", "ordered_list_open"):
            close = _matching_close(tokens, i)
            blocks += _parse_list(tokens[i : close + 1])
            i = close + 1

        elif kind == "blockquote_open":
            close = _matching_close(tokens, i)
            blocks.append(_parse_blockquote(tokens[i + 1 : close]))
            i = close + 1

        elif kind == "fence":
            blocks.append(_parse_fence(tok))
            i += 1

        elif kind == "code_block":
            blocks.append(
                _block("code", {
                    "rich_text": _text_segments(tok.content.rstrip("\n"), _annotations()),
                    "language": "plain text",
                })
            )
            i += 1

        elif kind == "hr":
            blocks.append(_block("divider", {}))
            i += 1

        elif kind == "table_open":
            close = _matching_close(tokens, i)
            table = _parse_table(tokens[i : close + 1])
            if table:
                blocks.append(table)
            i = close + 1

        else:
            i += 1

    return blocks


def _parse_paragraph(inline: Token) -> list[Block]:
    """Convert a paragraph's inline token to blocks.

    A paragraph containing only an image becomes a real image block instead
    of an inline-link fallback.
    """
    children = inline.children or []
    only_image = [c for c in children if c.type not in ("text",) or c.content.strip()]
    if len(only_image) == 1 and only_image[0].type == "image":
        src = only_image[0].attrGet("src")
        if src and src.startswith(("http://", "https://")):
            return [_block("image", {"type": "external", "external": {"url": src}})]

    rich = _rich_text(inline)
    if not rich:
        return []
    return [_block("paragraph", {"rich_text": rich})]


def _parse_fence(tok: Token) -> Block:
    lang = (tok.info or "").strip().split()[0].lower() if tok.info.strip() else ""
    lang = _LANGUAGE_ALIASES.get(lang, lang)
    if lang not in _NOTION_LANGUAGES:
        lang = "plain text"
    return _block("code", {
        "rich_text": _text_segments(tok.content.rstrip("\n"), _annotations()),
        "language": lang,
    })


def _parse_list(tokens: list[Token]) -> list[Block]:
    """`tokens[0]` is the *_list_open and `tokens[-1]` its matching *_list_close."""
    ordered = tokens[0].type == "ordered_list_open"
    blocks: list[Block] = []
    i = 1
    while i < len(tokens) - 1:
        if tokens[i].type != "list_item_open":
            i += 1
            continue
        close = _matching_close(tokens, i)
        blocks.append(_parse_list_item(tokens[i + 1 : close], ordered))
        i = close + 1
    return blocks


def _parse_list_item(tokens: list[Token], ordered: bool) -> Block:
    """Convert a list item's tokens to a block.

    The item's first paragraph becomes the block's text; everything else
    becomes `children`.
    """
    inner = _parse_sequence(tokens)
    lead: list[RichText] = []
    if inner and inner[0]["type"] == "paragraph":
        lead = inner[0]["paragraph"]["rich_text"]
        inner = inner[1:]

    checked = _strip_task_marker(lead)
    if checked is None:
        kind = "numbered_list_item" if ordered else "bulleted_list_item"
        payload: dict[str, Any] = {"rich_text": lead}
    else:
        kind = "to_do"
        payload = {"rich_text": lead, "checked": checked}

    if inner:
        payload["children"] = inner
    return _block(kind, payload)


def _strip_task_marker(rich: list[RichText]) -> bool | None:
    """Detect a leading `[ ] `/`[x] ` marker on the item and strip it.

    Done by hand instead of pulling in the tasklists plugin — it is a single
    rule and avoids one more dependency.
    """
    if not rich:
        return None
    head = rich[0]["text"]["content"]
    for marker, checked in (("[ ] ", False), ("[x] ", True), ("[X] ", True)):
        if head.startswith(marker):
            rich[0]["text"]["content"] = head[len(marker) :]
            return checked
    return None


def _parse_blockquote(tokens: list[Token]) -> Block:
    inner = _parse_sequence(tokens)
    lead: list[RichText] = []
    if inner and inner[0]["type"] == "paragraph":
        lead = inner[0]["paragraph"]["rich_text"]
        inner = inner[1:]

    emoji = _strip_callout_marker(lead)
    if emoji:
        payload: dict[str, Any] = {
            "rich_text": lead,
            "icon": {"type": "emoji", "emoji": emoji},
            "color": "gray_background",
        }
        kind = "callout"
    else:
        payload = {"rich_text": lead}
        kind = "quote"

    if inner:
        payload["children"] = inner
    return _block(kind, payload)


def _strip_callout_marker(rich: list[RichText]) -> str | None:
    """`> [!NOTE]` (GitHub's alert syntax) becomes a Notion callout."""
    if not rich:
        return None
    head = rich[0]["text"]["content"]
    if not head.startswith("[!"):
        return None
    end = head.find("]")
    if end == -1:
        return None
    kind = head[2:end].strip().lower()
    emoji = _CALLOUT_KINDS.get(kind)
    if not emoji:
        return None
    rich[0]["text"]["content"] = head[end + 1 :].lstrip("\n ")
    if not rich[0]["text"]["content"]:
        rich.pop(0)
    return emoji


def _parse_table(tokens: list[Token]) -> Block | None:
    rows: list[list[list[RichText]]] = []
    has_header = False

    i = 0
    in_head = False
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "thead_open":
            in_head = True
        elif tok.type == "thead_close":
            in_head = False
        elif tok.type == "tr_open":
            close = _matching_close(tokens, i)
            cells: list[list[RichText]] = []
            j = i + 1
            while j < close:
                if tokens[j].type in ("th_open", "td_open"):
                    cell_close = _matching_close(tokens, j)
                    inline = tokens[j + 1] if tokens[j + 1].type == "inline" else None
                    cells.append(_rich_text(inline))
                    j = cell_close + 1
                else:
                    j += 1
            if cells:
                rows.append(cells)
                if in_head:
                    has_header = True
            i = close + 1
            continue
        i += 1

    if not rows:
        return None

    width = max(len(r) for r in rows)
    for r in rows:
        r.extend([] for _ in range(width - len(r)))

    return _block("table", {
        "table_width": width,
        "has_column_header": has_header,
        "has_row_header": False,
        "children": [
            {"object": "block", "type": "table_row", "table_row": {"cells": r}}
            for r in rows
        ],
    })


def _cap_depth(blocks: list[Block], depth: int = 1) -> list[Block]:
    """Flatten nesting beyond what the API accepts (2 levels per request).

    Instead of failing at runtime on a note with a 3-level list, the
    excess levels are promoted to siblings of the parent block. `table` is
    exempt: a `table_row` child does not count as free-form nesting, so it
    is never flattened.
    """
    out: list[Block] = []
    for block in blocks:
        kind = block["type"]
        payload = block[kind]
        children = payload.get("children")
        if not children:
            out.append(block)
            continue

        if kind == "table":
            out.append(block)
            continue

        if depth >= MAX_NEST_DEPTH:
            payload.pop("children")
            out.append(block)
            out.extend(_cap_depth(children, depth))
        else:
            payload["children"] = _cap_depth(children, depth + 1)
            out.append(block)
    return out


def plain_rich_text(text: str, *, bold: bool = False) -> list[RichText]:
    """Build plain-text rich_text without going through the Markdown parser.

    Exists for code that assembles blocks directly as dicts (e.g.
    ``notion/metadata.py``) and needs the same annotation handling and
    2000-char split without reinterpreting the text as Markdown — an LLM
    summary containing `**` or backticks must not accidentally turn into
    formatting.
    """
    return _text_segments(text, _annotations(bold=bold))


def md_to_blocks(markdown: str) -> list[Block]:
    """Convert Markdown (the subset defined in ``llm/prompts.py``) into Notion blocks."""
    tokens = _MD.parse(markdown or "")
    return _cap_depth(_parse_sequence(tokens))


def md_to_html(markdown: str) -> str:
    """Render HTML for the UI preview.

    Uses **the same parser instance** as ``md_to_blocks``, on purpose: what
    shows up in the preview is what the compiler actually sees. A different
    renderer (e.g. a JS Markdown library) would accept syntax the compiler
    discards, and the preview would lie about what will reach Notion.
    """
    return _MD.render(markdown or "")
