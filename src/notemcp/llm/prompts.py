"""Prompts for the formatter.

The MARKDOWN_SPEC block describes exactly the subset that
``notion/compiler.py`` knows how to translate. **The two must stay in
sync**: if you teach the compiler a new syntax, allow it here too; if you
remove it from here, remove it from there. Any syntax outside this list is
silently discarded during compilation.

Everything here is written in English, including the instructions sent to the
model. That is a deliberate separation: the *prompt* is code, the *note* is
user content. The model is told to write the note in whatever language the
raw text came in, so an English prompt never leaks English output onto a
Portuguese note.

The `doc_type` values are the one exception that stays untranslated. They are
schema values persisted as Notion property values, so changing them would
orphan every page already published.
"""

from __future__ import annotations

from ..models import DOC_TYPES, FormatContext

MARKDOWN_SPEC = """\
Syntax allowed in the body_md field (anything else is DISCARDED):

  # Section title           -> heading 1
  ## Subtitle               -> heading 2
  ### Sub-subtitle          -> heading 3
  Plain paragraph text.
  - list item
  1. numbered item
  - [ ] open task
  - [x] completed task
  > quote
  > [!NOTE] callout         (also: [!TIP] [!IMPORTANT] [!WARNING] [!CAUTION])
  ---                       -> divider
  | col A | col B |         -> table (with a |---|---| separator row)
  ```python                 -> code block with a language
  code
  ```

  Inline: **bold**, *italic*, `code`, ~~strikethrough~~, [text](url)

Lists may be nested at most 2 levels deep.\
"""

LANGUAGE_RULE = """\
Write the note in the SAME LANGUAGE as the raw text you were given. If the \
note is in Portuguese, every field you produce must be in Portuguese; if it \
is in English, produce English; and so on. Do not translate, and do not \
switch language because these instructions are in English. This applies to \
`title`, `summary`, `tags` and `body_md` alike. When the raw text mixes \
languages, follow the one it is mostly written in and leave the foreign \
fragments as they are.\
"""


def _language_rule(ctx: FormatContext) -> str:
    """Return the language instruction for this request.

    Defaults to mirroring the source note. `FormatContext.language` exists as
    an explicit override for the caller who genuinely wants a fixed output
    language — it is not the normal path, because improving a note should
    never silently change the language the author wrote it in.
    """
    if ctx.language:
        return f"Write the note in {ctx.language}, regardless of the language of the raw text."
    return LANGUAGE_RULE


def system_prompt(ctx: FormatContext) -> str:
    return f"""\
You are a technical editor. You receive a raw, messy note — possibly full of \
typos and with no structure — and return a clean, well-organised document.

RULES
1. Preserve ALL factual information from the original. Never invent facts, \
numbers, names, dates or conclusions that are not in the text.
2. Reorganise freely: group related subjects, create sections, turn \
enumerations into lists, extract tasks into checkboxes, put code in blocks.
3. Fix spelling, punctuation and grammar. Improve how the sentences read \
without changing their meaning.
4. If the original is short (1-2 sentences), do NOT invent sections: return a \
single clean paragraph.
5. {_language_rule(ctx)} Today is {ctx.today.isoformat()}.
6. `title`: at most 100 characters, descriptive, no quotes.
7. `doc_type`: pick exactly one of {", ".join(DOC_TYPES)}. These identifiers \
are fixed — use them verbatim, do not translate them.
8. `tags`: up to 5, lowercase, one or two words each, no commas and no '#'.
9. `summary`: 1 or 2 sentences saying what the note is about.
10. `body_md`: the formatted document. Do NOT repeat the title as a heading — \
it already goes in the page title.

{MARKDOWN_SPEC}

Reply with the JSON object ONLY, with no code fences and no commentary."""


def user_prompt(raw: str, ctx: FormatContext) -> str:
    parts = []
    if ctx.hint:
        parts.append(f"Additional instruction from the author: {ctx.hint}\n")
    parts.append("Raw note:\n---\n" + raw.strip() + "\n---")
    return "\n".join(parts)


def repair_prompt(bad_output: str, error: str) -> str:
    return (
        "Your previous reply failed validation.\n\n"
        f"Error:\n{error}\n\n"
        f"Previous reply:\n{bad_output[:4000]}\n\n"
        "Return ONLY the corrected JSON object, respecting the schema. "
        "Keep the same language as the raw note."
    )
