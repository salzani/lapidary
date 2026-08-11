# notemcp

Paste a raw note, an LLM formats it, it becomes a well-structured page in Notion.

`notemcp` is a local-first capture tool. It takes unstructured text — meeting
scribbles, half-formed ideas, lecture notes — sends it to Gemini, the model
you choose explicitly, shows you an editable preview, and publishes it to
Notion with metadata filled in.

Every choice that matters here is explicit, never inferred silently. **There
is no automatic fallback between anything this app lets you configure** — not
between models, not between destinations. A note routed to the wrong model
because a name typo silently resolved to a different one, or one that lands
in the wrong place because a shortcut applied somewhere it shouldn't have,
are both worse outcomes than a loud, one-time setup error. That is the same
principle behind `--parent`/`--destination` failing hard on contradictory
flags instead of guessing (see [CLI reference](#cli-reference)) and behind an
unavailable Gemini model staying in the dropdown, disabled with a reason,
instead of silently swapping in whatever else happens to be ready. This
project used to also offer a local Ollama provider, with the same principle
guarding against a sensitive note silently leaving the machine when the local
model was busy — Ollama support was removed in favor of Gemini-only, but the
"no silent substitution" rule it was built on stayed and now covers
everything above instead.

---

## Quick start

Requires Python **3.11+**. Point the virtualenv at an interpreter that actually
exists on the machine — a venv built on a Python that later disappears becomes a
dangling symlink, and every command fails before it even reaches the entry point.

```bash
python3.13 -m venv .venv          # any 3.11+
.venv/bin/python -m pip install -e ".[dev,ui,mcp]"
```

Create a `.env` with at least `NOTION_TOKEN` and `NOTION_PARENT_PAGE_ID`.
Every other setting has a working default, so a minimal file is two lines:

```bash
NOTION_TOKEN=secret_...
NOTION_PARENT_PAGE_ID=...
```

| Variable | Required | Meaning |
|---|---|---|
| `NOTION_TOKEN` | yes | Internal integration token from <https://www.notion.so/my-integrations> |
| `NOTION_PARENT_PAGE_ID` | yes | The **root** page everything is written under |
| `NOTION_DATA_SOURCE_ID` | no | Shortcut that skips database discovery (see [Destinations](#destinations)) |
| `LLM_PROVIDER` | no | e.g. `gemini:gemini-3.6-flash` (default) or `gemini:gemini-3.1-pro` |
| `NOTION_WRITER` | no | `api` (default) or `mcp` |
| `NOTION_DESTINATION` | no | `database` (default) or `page` |
| `GEMINI_API_KEY` | only for Gemini | |
| `NOTES_DB` | no | Queue location; defaults to `~/.local/share/notemcp/notes.db` |

**Where `.env` is looked for**, first match wins:

1. the path in `NOTEMCP_ENV`, if set
2. next to the executable, for a packaged build
3. the checkout the executable was built in, for a packaged build
4. `~/.config/notemcp/.env`
5. `.env` in the current working directory

Running from source, step 5 (`.env` in the CWD) is usually what fires — but
not always: `packaging/install.py` copies the repository's `.env` to
`~/.config/notemcp/.env` (step 4) the first time you install the desktop app,
and step 4 beats step 5. If you have run `packaging/install.py` even once
from this checkout, that copy is what a plain `python -m notemcp.cli` picks
up from then on, not the one at the repository root — check `--doctor`'s
".env resolution" section (see [CLI reference](#cli-reference)) if you edit
the repo's `.env` and the change does not seem to take effect.

A packaged build finds the checkout's own `.env` through step 3 —
`packaging/build.py` writes to `<checkout>/dist/<name>/`, so the executable
can derive where it came from. Clone, build, pin it to your launcher, and the
`.env` you already edited keeps working, with no second copy to maintain —
unless `packaging/install.py` has already made that second copy per the
paragraph above, in which case step 3 never gets the chance to run: step 2
(`.env` next to the executable) and step 3 both come before step 4, but
`install.py`'s copy is what actually starts existing in practice.

Step 4 is also the fallback for a bundle that no longer sits in a checkout —
one you moved, or shipped to someone else. Keeping credentials in two files
means they drift, and the drift is silent, so prefer one.

If the token is missing, the error names every path it searched.

**Share the parent page with your integration.** On the page: `···` →
Connections → your integration. Without this the API returns `404` even with a
valid token — it is the single most common setup failure.

**Get a `GEMINI_API_KEY`.** Create one at
<https://aistudio.google.com/apikey> and put it in `.env`. The free tier has
a real rate limit — a 429 mid-session is the failure you are most likely to
actually hit, not a model quality problem; `notemcp` reports it with its own
message ("rate limit do free tier atingido") instead of a generic API error,
and the fix is simply to wait and retry, not to reconfigure anything.

### Run it

```bash
# Command line
.venv/bin/python -m notemcp.cli "a loose note"
.venv/bin/python -m notemcp.cli note.txt
cat note.txt | .venv/bin/python -m notemcp.cli

# Graphical interface
.venv/bin/python -m notemcp.ui.app
```

The installed entry points `notemcp` and `notemcp-ui` do the same thing.

### Run the tests

```bash
env -u DISPLAY QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

`QT_QPA_PLATFORM=offscreen` is mandatory — the suite imports PySide6. No test
touches the network.

---

## CLI reference

```bash
notemcp note.txt --dry-run                          # format, print, write nothing
notemcp note.txt --show-blocks                      # dump the compiled Notion blocks
notemcp note.txt --provider gemini:gemini-3.6-flash # override LLM_PROVIDER
notemcp note.txt --hint "extract the action items"  # steer the model
notemcp note.txt --destination page                 # standalone page, not a database row
notemcp note.txt --parent "College/Calculus I"      # publish inside a specific page
notemcp note.txt --parent-id 1a2b3c4d...            # same, by id
notemcp --list-pages                                # list subpages of the root, with ids
notemcp --list-pages "College"                      # list subpages of one level
notemcp --history                                   # what has been captured
notemcp --retry                                     # re-send what failed
notemcp --retry --parent-id 1a2b3c4d...             # re-send, re-pointed elsewhere
notemcp --doctor                                     # diagnose config, SDK, and provider status
notemcp --doctor --live                              # + one real Gemini call (spends quota, opt-in)
```

`--parent` and `--parent-id` are mutually exclusive and both imply
`--destination page`. Combining either with `--destination database` is a hard
error rather than a silent precedence rule: contradictory configuration should
not resolve itself.

`--parent` matches titles case-, accent-, and punctuation-insensitively, one
level at a time. A path that does not exist, or that is ambiguous at some level,
fails loudly and lists the candidates. It never falls back to the root and never
picks a "best match".

`--doctor` (`src/notemcp/doctor.py`) prints six sections — runtime, `.env`
resolution, redacted configuration, the Gemini SDK probed step by step,
provider discovery, and a verdict with its own exit code (`0` usable, `2` SDK
fine but no key, `1` broken) — and works without `NOTION_TOKEN` set. It runs
identically from source and from a packaged build, which is what makes it
useful: run it both ways and the diff between the two outputs is the bug.
This is also the first thing to run when a packaged build behaves oddly — see
["Building a desktop executable"](#building-a-desktop-executable) below.
`--live` adds one real, billed Gemini call and prints its latency; it is
opt-in on purpose and is never run by the test suite, CI, or `packaging/build.py`.

---

## Architecture

### The decision everything else rests on

**The LLM returns Markdown, never Notion block JSON.**

Notion's block schema is verbose and full of traps: a 2000-character ceiling per
`rich_text` element, 100 children per request, at most two levels of nesting. No
model produces that reliably, and a small local model is not close.

So the model does exactly one thing — return a `NoteDraft` (title, doc type,
tags, summary, and a Markdown body). A deterministic compiler translates
Markdown into blocks. The compiler is pure, has no I/O, and is the most heavily
tested module in the project.

```
raw text → LLMProvider → NoteDraft → md_to_blocks() → NotionWriter → Notion page
                                     (API writer only)
```

The corollary is a rule that cannot be broken: **`MARKDOWN_SPEC` in
`llm/prompts.py` and the compiler in `notion/compiler.py` travel together.**
Syntax allowed in the prompt but unimplemented in the compiler is discarded at
compile time without an error.

### Ports and adapters

Three ports, each with swappable implementations selected by configuration.
Callers never know which implementation is active.

| Port | Implementations | Selected by |
|---|---|---|
| `LLMProvider` (`llm/base.py`) | `gemini:<model>` | `LLM_PROVIDER` |
| `NotionWriter` (`notion/base.py`) | `api`, `mcp` | `NOTION_WRITER` |
| `NotionBrowser` (`notion/browser.py`) | `api`, `mcp` | `NOTION_WRITER` |

`NotionWriter` and `NotionBrowser` are deliberately separate ports satisfied by
the same concrete classes. Writing and browsing are different responsibilities —
the code that navigates your page tree has no business publishing — but they
share a transport, and splitting the implementations would mean a second HTTP
client, or worse, a second MCP subprocess.

#### The two writers are a trade, not a hierarchy

`NotionApiWriter` compiles Markdown into blocks and speaks the REST API. It owns
the real complexity: batching, the 2000-character limit, nesting depth, and
transport retries.

`NotionMcpWriter` talks to the official Notion MCP server over stdio. That
server accepts Markdown directly, so this adapter **skips the compiler** and
sends the body verbatim. You gain syntax our compiler does not implement; you
lose fine control over the resulting blocks and the guarantee that the UI
preview matches what lands in Notion. `api` remains the default for that reason.

### Destinations

A note can land in one of two places, chosen per note.

| Destination | What it is | Metadata |
|---|---|---|
| `database` (default) | A row in the "Notas" database under the root page | Type, Tags, Date, Summary as real, filterable columns |
| `page` | A child page of any page you pick | No columns exist, so type/tags/summary are rendered as a **callout** at the top of the body |

`NOTION_DATA_SOURCE_ID` is a shortcut that applies **only** to the `database`
destination. Even when it is set, choosing `page` always publishes as a child
page. Without that guard, a user with the shortcut configured would pick
"standalone page", get a valid URL back, and find the note in the database — a
failure with no error attached to it.

`NOTION_PARENT_PAGE_ID` is the **root of everything this app writes**, not one
destination among others: the "Notas" database is created under it too. To keep
loose notes and the database in different places, choose the parent page per
note rather than repointing the root.

### Three layers that are easy to confuse

The most valuable modeling decision in the project, and the source of most of
its near-misses:

| Layer | Type | Lifetime |
|---|---|---|
| The user's **choice** | `DestinationChoice(kind, page_id)` | Persisted per note |
| The **resolved** target | `Destination(kind, id)` | Ephemeral, per publish |
| A **node** in the tree | `PageNode(id, title)` | Ephemeral, cached per session |

They are distinct because a resolved database id is *discovered* and can change
between capture and retry, while a page the user pointed at is *intent* and does
not. Collapsing them into one type is precisely what makes a retry publish to
the wrong place.

`page_id is None` means "the user chose nothing" and resolves to the root.
`page_id == ""` is rejected at construction, because an empty string flowing
into an `or root` expression publishes silently to the wrong page.

### The queue

Every note enters SQLite **before** the model is called, and the draft is saved
**before** the Notion write.

```
captured ──formatted──> drafted ──published──> published
                           │
                           └──write failed──> failed ──retry──> published
```

`pending()` returns `drafted` and `failed` together: both already have a draft,
so a retry never calls the model again. In practice this is the difference
between a 0.7-second retry and a 40-second one.

Schema migrations are additive and idempotent, applied via `PRAGMA table_info`
at open time. A database from any earlier version gains the new columns without
manual intervention, and rows written before a column existed read as the
default that preserves old behaviour.

### Interface layout

```
cli.py ──┐
         ├──> pipeline.py ──> LLMProvider
ui/ ─────┘         │          NotionWriter / NotionBrowser
                   └────────> store/db.py
```

The GUI is a `QWebEngineView` serving a local page, bridged to Python over
QWebChannel. Every network call runs on a `QThreadPool`; a local model inference
takes tens of seconds and would otherwise freeze even window repaints.

The preview uses **the compiler's own Markdown parser instance**. A different
renderer — a JavaScript Markdown library, say — would accept syntax the compiler
silently discards, and the preview would lie about what reaches Notion.

The CLI does not read preferences saved by the GUI
(`~/.config/notemcp/state.json`). They are independent interfaces, each with its
own default.

---

## Module map

| Path | Responsibility |
|---|---|
| `notion/compiler.py` | Markdown → Notion blocks. Pure. Where the API's limits live |
| `notion/api_writer.py` | REST adapter: data sources, batching, transport retry |
| `notion/mcp_writer.py` | MCP adapter: persistent asyncio session over stdio |
| `notion/browser.py` | Page-tree navigation port and path resolution |
| `notion/tree_cache.py` | TTL cache decorator over the browser |
| `notion/metadata.py` | One metadata rule, three serializations (block, Markdown, HTML) |
| `notion/schema.py` | Notion property shapes for both destinations |
| `llm/prompts.py` | Prompt and `MARKDOWN_SPEC` — travels with the compiler |
| `llm/registry.py` | Provider discovery for the UI dropdown |
| `models.py` | Domain types and their invariants |
| `pipeline.py` | Orchestration, destination resolution, retry |
| `store/db.py` | Queue, history, migrations |
| `ui/bridge.py` | QWebChannel bridge — the Python half of the UI |
| `ui/worker.py` | Thread pool, and the strong reference that keeps workers alive |
| `ui/web/` | The interface itself |

---

## Design notes worth knowing before you change things

A few behaviours look redundant and are load-bearing. Each is documented at its
definition; this is the short list so you recognise them.

- **Two staleness guards, not one.** The bridge discards out-of-order navigation
  responses, and the frontend independently discards responses aimed at the
  wrong level of the dropdown chain. They cover different failures.
- **The metadata callout is built as a block dict, never round-tripped through
  Markdown.** Summary text comes from a language model and may contain `**` or
  backticks; re-parsing it would reinterpret them.
- **The callout is prepended before batching, not after.** A body of exactly 100
  blocks plus a callout is 101 children, which the API rejects.
- **The cache writes through on page creation instead of refetching.** A refetch
  would depend on Notion's index being immediately consistent — the assumption
  that previously caused duplicate databases.
- **Invalidating a node does not invalidate its ancestors.** A parent's child
  list does not change because a grandchild appeared.
- **Title comparison happens only in Python.** Divergent Unicode normalization
  between JavaScript and Python is a bug that surfaces only on accented input.

There is no automated coverage of the JavaScript ↔ Python boundary. The test
suite exercises the bridge directly in Python, so a signature mismatch between
`app.js` and a `@Slot` is invisible to it — this has silently broken a button
before. Check arity by hand when you change either side.

---

## Building a desktop executable

`packaging/` (not `build/` — PyInstaller already owns that directory name,
and it's in `.gitignore`) holds everything needed to turn the UI into a
standalone executable with [PyInstaller](https://pyinstaller.org), in
**`onedir`** mode.

`onedir`, not `onefile`, is not a style preference. PySide6 with
QtWebEngine bundles a full Chromium — the installed wheel is ~650 MB.
`onefile` re-extracts that into a temp directory on *every launch*: tens of
seconds of startup, and a known class of Chromium sandbox failures specific
to running out of a freshly unpacked tmpfs. `onedir` extracts once, at
build time, into a real directory the app then runs from directly.

### Building it

Requires the same Python **3.11+** as the rest of the project, plus the
`ui` and `build` extras:

```bash
.venv/bin/python -m pip install -e ".[ui,build]"
.venv/bin/python packaging/build.py
```

`packaging/build.py` is the single entry point on both platforms — there is
no separate script per OS. It detects the current OS, checks that Python,
PySide6, PyInstaller, and `google-genai` are all present and prints the
exact `pip install` command to run if one is missing, runs PyInstaller
against `packaging/notemcp.spec`, and then **smoke-tests the result** in
three steps: it runs the built executable with `--version` (proving the
frozen interpreter and the `notemcp` package inside it actually start),
boots the real GUI window under `QT_QPA_PLATFORM=offscreen` with a short
auto-quit timer and checks for a clean exit (proving the window itself is
constructed and the Qt event loop runs), and finally runs `--doctor`
(see below), failing the build outright if it reports the SDK broken. A
build that produces files but ships a binary that never starts, or that
starts but cannot reach its only provider, is worse than no build — this is
why the script does not stop at "PyInstaller exited 0".

The artifact lands at `dist/notemcp/` — `dist/notemcp/notemcp` on Linux,
`dist/notemcp/notemcp.exe` on Windows. Ship the whole `dist/notemcp/`
directory; the executable does not work on its own, separated from the
libraries and Qt resources next to it. Expect it to land somewhere in the
same order of magnitude as the installed `PySide6` wheel (hundreds of MB,
dominated by the bundled Chromium) — see [ESTADO.md](ESTADO.md) for exactly
what this project has and has not measured about the built size.

### If the built app behaves oddly

Run `--doctor` first, always:

```bash
dist/notemcp/notemcp --doctor
```

`packaging/build.py` already runs this as its third smoke test and fails
the build on a broken verdict (exit `1`), but a bundle produced before that
gate existed, or an environment change since the build, can still ship one
that "works" (exits 0/2 on `--version` and the GUI boot) while actually
unable to reach Gemini. `--doctor` is the same code running both from
source and from the frozen executable (`src/notemcp/doctor.py` — not
`packaging/`, deliberately, so it gets ordinary test coverage instead of
rotting unwired into the frozen entry point). Run it both ways and compare:

```bash
.venv/bin/python -m notemcp.cli --doctor
dist/notemcp/notemcp --doctor
```

The diff between the two outputs is the bug. This is exactly how the
OpenSSL/libcrypto mismatch in [ESTADO.md](ESTADO.md) (§5) was found: the
source run showed Gemini available, the packaged one showed every provider
unavailable with a misleading "not installed" reason, and `--doctor`'s
`4. Gemini SDK` section is what pointed at the real, native-library cause
instead.

### Cross-compilation does not exist here

This is the single most common point of confusion, so it's worth stating
plainly: **PyInstaller cannot cross-compile.** It freezes the interpreter
and native extensions it is *currently running under* — running it on Linux
produces a Linux binary, running it on Windows produces a Windows binary,
and there is no flag that changes that. `.github/workflows/build.yml` runs
the same `packaging/build.py` on an `ubuntu-latest` and a `windows-latest`
runner and publishes both artifacts — that matrix is the practical answer
if you only have one of the two platforms yourself: let CI build the other
one.

### `.env` resolution in a packaged build

A desktop shortcut starts a process with an unpredictable working directory
(`/`, `C:\Windows\System32`, the user's home) — `.env` resolved relative to
the CWD, which is what a plain `env_file=".env"` does, would never be found
that way. `src/notemcp/config.py::_resolve_env_file` instead checks, in
order, the first that exists: the path in `NOTEMCP_ENV` if set, `.env` next
to the executable (only when frozen), `~/.config/notemcp/.env`, then
finally `.env` in the CWD — the same development path this project always
supported. Put the built app's `.env` next to the executable, or in
`~/.config/notemcp/.env` if you'd rather keep it out of the `dist/`
directory you might delete on the next rebuild.

### The MCP writer is not part of the packaged build

`NOTION_WRITER=mcp` (`NotionMcpWriter`) shells out to
`npx -y @notionhq/notion-mcp-server`, which means a working Node.js/npm
installation on whatever machine runs the executable — PyInstaller has no
way to embed that. This is not a bug to fix later; it's a boundary. The
packaged executable supports `NOTION_WRITER=api` (the default, and the only
one that does not need Node at all). If you need the MCP writer, run
notemcp from source with Node installed instead of from the packaged
executable.

### Pinning it to the application menu / taskbar

`packaging/install.py` is the single command that takes a built executable
the rest of the way to something you can pin and open like any other app:

```bash
.venv/bin/python -m pip install -e ".[ui,build]"
.venv/bin/python packaging/install.py
```

End to end, this builds the executable (it calls `packaging/build.py`
itself — pass `--skip-build` to reuse an existing `dist/notemcp/` instead),
generates app icons at 16/32/48/64/128/256px plus a multi-size Windows
`.ico`, installs the platform's menu entry, checks that `.env` is where the
app will find it, and prints a summary of what it did and where.

**Linux:** writes `~/.local/share/applications/notemcp.desktop` (respecting
`XDG_DATA_HOME` if set) and the icon sizes under
`~/.local/share/icons/hicolor/<size>/apps/notemcp.png`. It sets
`StartupWMClass=notemcp` — without that line GNOME cannot tell a running
notemcp window apart from the pinned launcher icon, and pinning it shows
two separate icons in the dock instead of one. It also runs
`update-desktop-database` and `gtk-update-icon-cache` if they're installed
(cache refreshes, not requirements — skipped silently if absent), and
validates the generated file with `desktop-file-validate` if that's
available, printing the result.

**Windows:** writes a `.lnk` to
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\notemcp.lnk`, built via a
PowerShell snippet driving `WScript.Shell` — no `pywin32` dependency added
for it.

Icons are generated with PySide6's `QPainter` (already a build dependency)
under `QT_QPA_PLATFORM=offscreen`, so this works in CI or over SSH with no
display. They reproduce the accent-colored dot from
`src/notemcp/ui/web/style.css` (`--accent`, `--bg`, `--radius` — the same
mark as `.brand::before` in the web UI) rather than inventing a new one.
Generated files land in `packaging/assets/`, which is derived output and
therefore in `.gitignore`, not committed.

**Remove it** with:

```bash
.venv/bin/python packaging/install.py --uninstall
```

This removes the menu entry and the generated icons. It never touches
`.env` or the notes database — those are your data, not build artifacts,
and are left exactly where they were.

**If you move or delete this checkout,** the menu entry breaks — `Exec=`
points at the absolute path of `dist/notemcp/notemcp` *inside this specific
checkout*, not somewhere the icon/launcher owns independently. There is no
"repair" step: from the new location, just run `packaging/install.py`
again (or `packaging/install.py --skip-build` after a fresh
`packaging/build.py`) and it overwrites the old entry with the new path.

**`.env` still has to exist somewhere the app will find it** — see
"`.env` resolution in a packaged build" above.
`packaging/install.py` checks `~/.config/notemcp/.env` specifically: if
it's already there, it says so; if it's missing but this repository has a
`.env` at its root, it copies it there (and sets permissions to `600` — it
holds a token) and says so; if neither exists, it prints the exact path and
the two lines you need (`NOTION_TOKEN=`, `NOTION_PARENT_PAGE_ID=`) instead
of writing a template file — this project deliberately does not ship a
`.env.example`.

---

## Project status

See [ESTADO.md](ESTADO.md) for the working handoff: what has been verified
against a real Notion workspace, what has only been verified by test, what is
written but never seen running, and the catalogue of bugs this project has
already paid for.
