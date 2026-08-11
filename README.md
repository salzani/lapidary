# Lapidary

Paste a messy note. A language model cleans it up. It lands in Notion as a
properly structured page.

*Lapidary* — of gem-cutting, and of prose that is elegant and concise. The tool
does the second by doing the first: your raw text keeps every fact it had, but
comes out shaped.

It runs locally as a desktop app or from the command line, and talks to Google
Gemini and to Notion. Nothing is ever substituted silently — not the model, not
the destination, not a failed write.

---

## How it works

**The model returns Markdown, never Notion block JSON.** That single decision
shapes everything else.

Notion's block schema is verbose and trap-laden: 2000 characters per rich-text
element, 100 children per request, two levels of nesting maximum. No model
produces that reliably. So the model does exactly one job — return a
`NoteDraft` — and a deterministic compiler turns Markdown into blocks. The
compiler is pure, has no I/O, and is the most heavily tested module here.

```
raw text ──> LLMProvider ──> NoteDraft ──> md_to_blocks() ──> NotionWriter ──> page
                             title              (API writer only)
                             doc_type
                             tags
                             summary
                             body_md
```

The corollary is a rule that cannot be broken: **`MARKDOWN_SPEC` in
`llm/prompts.py` and the compiler in `notion/compiler.py` travel together.**
Syntax allowed in the prompt but unimplemented in the compiler is discarded at
compile time, silently.

The model is also told to write in **the same language as the note**. Improving
someone's text should never translate it.

---

## Architecture

### Ports

Three ports, swapped by configuration. Callers never know which is active.

| Port | Implementations | Selected by |
|---|---|---|
| `LLMProvider` (`llm/base.py`) | `gemini:<model>` | `LLM_PROVIDER` |
| `NotionWriter` (`notion/base.py`) | `api`, `mcp` | `NOTION_WRITER` |
| `NotionBrowser` (`notion/browser.py`) | `api`, `mcp` | `NOTION_WRITER` |

`NotionWriter` and `NotionBrowser` are separate ports satisfied by the same
classes: navigating your page tree and publishing to it are different
responsibilities, but they share a transport, and splitting the implementations
would mean a second HTTP client — or a second MCP subprocess.

**The two writers are a trade, not a hierarchy.** `api` compiles Markdown into
blocks and owns the real complexity (batching, size limits, transport retries).
`mcp` talks to Notion's official MCP server, which accepts Markdown directly,
so it **skips the compiler**: more syntax, less control, and the UI preview no
longer guarantees what lands. `api` is the default, and the only one that works
from a packaged build (`mcp` shells out to `npx`).

### Destinations

Chosen per note, in the UI or on the command line.

| Destination | Where it lands | Metadata |
|---|---|---|
| `database` (default) | A row in the "Notas" database | Type, Tags, Date, Summary as real, filterable columns |
| `page` | A child of any page you pick | No columns exist, so those fields become a **callout** at the top |

For `page`, the parent is chosen by walking your Notion tree — a chain of
dropdowns in the UI, or `--parent "College/Calculus I"` on the CLI.

`NOTION_PARENT_PAGE_ID` is the **root of everything this app writes**, not one
destination among others: the database is created under it too.

### Three layers that are easy to confuse

The most valuable modelling decision here, and the source of most near-misses:

| Layer | Type | Lifetime |
|---|---|---|
| The user's **choice** | `DestinationChoice(kind, page_id)` | persisted per note |
| The **resolved** target | `Destination(kind, id)` | ephemeral, per publish |
| A **node** in the tree | `PageNode(id, title)` | ephemeral, cached per session |

They are distinct because a resolved database id is *discovered* and changes
between capture and retry, while a page you pointed at is *intent* and does
not. Collapsing them is exactly what makes a retry publish to the wrong place.

### The queue

Every note enters SQLite **before** the model is called; the draft is saved
**before** the Notion write.

```
captured ──formatted──> drafted ──published──> published
                           │
                           └──write failed──> failed ──retry──> published
```

`pending()` returns `drafted` and `failed` together — both already have a
draft, so a retry never calls the model again. That is the difference between a
0.7-second retry and a 40-second one.

Migrations are additive and idempotent (`PRAGMA table_info` at open). Old
databases gain new columns with no manual step.

### Interfaces

```
cli.py ──┐
         ├──> pipeline.py ──> LLMProvider
ui/ ─────┘        │           NotionWriter / NotionBrowser
                  └────────> store/db.py
```

The GUI is a `QWebEngineView` bridged to Python over QWebChannel. Network calls
run on a `QThreadPool` — inference takes tens of seconds and would otherwise
freeze window repaints. The preview renders through **the compiler's own
Markdown parser**, so it cannot accept syntax the compiler discards.

---

## Setup

Requires Python **3.11+** and a Gemini API key from
<https://aistudio.google.com/apikey>.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,ui]"
```

Create a `.env` with two lines:

```bash
NOTION_TOKEN=ntn_...
NOTION_PARENT_PAGE_ID=...
GEMINI_API_KEY=...
```

**Share the parent page with your integration** — on the page, `···` →
Connections. Without it the API returns `404` even with a valid token; it is
the most common setup failure.

`.env` is searched in this order, first match winning: `NOTEMCP_ENV` → next to
the executable → the checkout it was built from → `~/.config/notemcp/.env` →
the current directory. The middle two only apply to a packaged build.

Optional: `LLM_PROVIDER` (default `gemini:gemini-3.6-flash`), `NOTION_WRITER`,
`NOTION_DESTINATION`, `NOTION_DATA_SOURCE_ID`, `NOTES_DB`.

---

## Usage

```bash
notemcp "a loose note"                  # or a file path, or stdin
notemcp note.txt --dry-run              # format and print, write nothing
notemcp note.txt --destination page     # standalone page, not a database row
notemcp note.txt --parent "College/Calculus I"
notemcp --list-pages "College"          # subpages of a level, with ids
notemcp --history                       # what has been captured
notemcp --retry                         # re-send what failed
notemcp --doctor                        # diagnose config, SDK and providers
```

Also `--provider`, `--parent-id`, `--hint`, `--show-blocks`, `--limit`.

`--parent` and `--parent-id` are mutually exclusive and both imply
`--destination page`; combining either with `--destination database` is a hard
error, not a precedence rule. A path that is missing or ambiguous fails loudly
and lists the candidates — it never falls back to the root.

Graphical interface: `notemcp-ui`.

Tests: `env -u DISPLAY QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
(286, none touching the network).

---

## Desktop app

```bash
.venv/bin/python -m pip install -e ".[ui,build]"
.venv/bin/python packaging/install.py      # builds, then adds a menu entry
```

`--skip-build` reuses an existing build; `--uninstall` removes the menu entry
and icons, never your `.env` or notes.

The build is PyInstaller `onedir` (~1 GB — it embeds Chromium via QtWebEngine)
and **cannot cross-compile**: a Windows `.exe` has to be built on Windows. The
CI matrix in `.github/workflows/build.yml` is the answer if you only have one
platform.

**If the packaged app misbehaves, run `notemcp --doctor` first.** It reports
which `.env` was resolved, whether the Gemini SDK actually imports, and what
provider discovery returned — from inside the bundle. It was written because a
build once shipped where nothing worked and nothing could be inspected.

---

## Things that look redundant and are not

Each is documented where it is defined; this is the short list so you recognise
them before deleting one.

- **Two staleness guards for tree navigation**, one in the bridge and one in
  the frontend. They cover different races.
- **The metadata callout is built as a block dict**, never round-tripped
  through Markdown — model output may contain `**` or backticks.
- **The callout is prepended before batching.** 100 blocks plus a callout is
  101 children, which the API rejects.
- **The tree cache writes through on page creation** instead of refetching.
  Refetching would assume Notion's index is immediately consistent; it is not.
- **Invalidating a node leaves its ancestors alone.** A parent's child list
  does not change because a grandchild appeared.
- **Title comparison happens only in Python.** Divergent Unicode normalization
  between JS and Python surfaces only on accented input.
- **OpenSSL is pinned to the build interpreter's own copy** in the PyInstaller
  spec. Mixing a conda `_ssl` with the system `libcrypto` produced a bundle
  where every `import ssl` failed — and since `ssl` is buried inside the Gemini
  SDK, the app reported the SDK as "not installed".

There is **no automated coverage of the JavaScript ↔ Python boundary**. The
suite exercises the bridge directly in Python, so an arity mismatch between
`app.js` and a `@Slot` is invisible to it — this has silently broken a button
before. Check by hand when changing either side.

---

## A note on names

The product is **Lapidary**. The Python package, the console commands and the
window class are still `notemcp` — renaming those touches imports, entry
points, the desktop entry, and the paths where your config and notes already
live. The display name and the internal identifier are different things, and
only one of them is free to change.

## Status

[ESTADO.md](ESTADO.md) is the working handoff: what has been verified against a
real Notion workspace, what is only verified by test, what is written but never
seen running, and the catalogue of bugs this project has already paid for.
