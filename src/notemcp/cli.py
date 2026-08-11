"""No-UI entrypoint — full pipeline and queue management.

    notemcp "raw text here"
    notemcp note.txt
    cat note.txt | notemcp
    notemcp note.txt --dry-run --provider gemini:gemini-3.5-flash-lite
    notemcp note.txt --parent "College/Calculus I"    # publish under a parent page, by path
    notemcp note.txt --parent-id 3b3450d7...          # same, by id (escape hatch)
    notemcp --list-pages "College"                    # list that level, with ids
    notemcp --history            # recent notes and each one's status
    notemcp --retry              # resend to Notion whatever is pending
    notemcp --retry --parent-id 3b3450d7...  # same, but re-point to a different parent page
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings
from .llm.base import LLMError
from .models import DestinationChoice, DestinationKind, FormatContext, NoteRecord
from .notion.base import NotionWriteError
from .notion.browser import NotionBrowseError, build_browser, resolve_page_path
from .notion.compiler import md_to_blocks
from .notion.metadata import metadata_callout_block
from .pipeline import Pipeline
from .store.db import NoteStore

STATUS_MARK = {
    "published": "ok  ",
    "failed": "FAIL",
    "drafted": "pend",
    "captured": "raw ",
}


def _split_parent_path(raw: str) -> list[str]:
    """`"College/Calculus I"` -> `["College", "Calculus I"]`.

    A title containing `/` is not supported by this separator — use
    `--parent-id` in that case (documented in `--help`; an escape scheme
    would be complexity without real demand).
    """
    return [part.strip() for part in raw.split("/") if part.strip()]


def _destination_label(record: NoteRecord) -> str:
    """`(database)`, `(page)` at the root, or `(page:3b34…db02)` with the
    id truncated — enough to recognize the page without cluttering the
    history line.
    """
    if record.destination_kind is DestinationKind.DATABASE:
        return "(database)"
    page_id = record.destination_page_id
    if not page_id:
        return "(page)"
    if len(page_id) <= 8:
        return f"(page:{page_id})"
    return f"(page:{page_id[:4]}…{page_id[-4:]})"


def _warn_if_uncurated_model(spec: str) -> None:
    """Warn, never fail, when the requested Gemini model is not in the
    curated list (`llm.registry.GEMINI_MODELS`).

    `build_provider` (`llm/base.py`) stays permissive about the model
    name on purpose — the escape hatch for a model Google ships after this
    list was last updated. But a typo, or a name from a list this project
    has since found to be wrong (see `registry.GEMINI_MODELS`'s own
    docstring), should not pass through in total silence either.
    """
    backend, _, model = spec.partition(":")
    if backend.strip().lower() != "gemini":
        return
    model = model.strip()
    if not model:
        return

    from .llm.registry import GEMINI_MODELS

    if model not in GEMINI_MODELS:
        print(
            f"warning: {model!r} is not in the curated list of Gemini models "
            f"({', '.join(GEMINI_MODELS)}) — trying anyway.",
            file=sys.stderr,
        )


def _read_input(value: str | None) -> str:
    """An existing path is read from disk; anything else is treated as the
    raw text itself. `len(value) < 4096` guards `path.is_file()` against a
    long raw note tripping an OS "filename too long" error.
    """
    if value is None:
        if sys.stdin.isatty():
            raise SystemExit("nothing to format: pass some text, a file, or use stdin")
        return sys.stdin.read()
    path = Path(value)
    if len(value) < 4096 and path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def _make_store(settings) -> NoteStore:
    return NoteStore(settings.notes_db or None)


def cmd_history(settings, limit: int) -> int:
    records = _make_store(settings).recent(limit)
    if not records:
        print("no notes recorded yet", file=sys.stderr)
        return 0
    for r in records:
        mark = STATUS_MARK.get(r.status, r.status)
        line = f"[{mark}] #{r.id:<4} {r.created_at[:16]}  {_destination_label(r)}  {r.title}"
        print(line)
        if r.notion_url:
            print(f"          {r.notion_url}")
        if r.error:
            print(f"          error: {r.error.splitlines()[0][:110]}")
    return 0


def cmd_list_pages(settings, path: str) -> int:
    """`--list-pages [path]`: exists so `--parent-id` is usable without
    opening the UI's browser — prints the level (id + title) and exits."""
    settings.require_notion(DestinationKind.PAGE)
    browser = build_browser(settings)
    try:
        node = resolve_page_path(browser, settings.notion_parent_page_id, _split_parent_path(path))
        children = browser.list_child_pages(node.id)
    except (NotionBrowseError, NotionWriteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{node.title} ({node.id}):")
    if not children:
        print("  (no subpages)")
    for child in children:
        print(f"  {child.title}  ({child.id})")
    return 0


def cmd_retry(settings, parent_id: str | None) -> int:
    """`--retry --parent-id <x>` re-points ALL pending notes to that parent
    page, explicitly — never a silent fallback. It is the way out for a
    `failed` note whose original parent page disappeared from Notion and
    would otherwise stay stuck in `failed` forever (risk 15).
    """
    store = _make_store(settings)
    pending = store.pending()
    if not pending:
        print("nothing pending", file=sys.stderr)
        return 0

    override = DestinationChoice.page(parent_id) if parent_id else None

    print(f"resending {len(pending)} note(s)...", file=sys.stderr)
    settings.require_notion()
    pipeline = Pipeline.from_settings(settings, store=store)

    failures = 0
    for record, ref, error in pipeline.retry_pending(override=override):
        if error:
            failures += 1
            print(f"[FAIL] #{record.id} {record.title}: {error.splitlines()[0]}", file=sys.stderr)
        else:
            print(f"[ok  ] #{record.id} {record.title} -> {ref.url}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    `--parent` and `--parent-id` are mutually exclusive, and both imply
    `--destination page` — combining either with `--destination database` is
    a hard error, never "last one wins" (same principle as the
    `NOTION_DATA_SOURCE_ID` guard in `Pipeline._resolve_destination`:
    contradictory configuration does not resolve itself).

    The CLI does not read `state.json` (that is the UI's preference): the
    default destination comes only from `NOTION_DESTINATION`, overridden by
    `--destination` when present.

    `--parent-id` is an unambiguous escape hatch: no network call, the id is
    already the target. `--parent` resolves by title BEFORE anything else —
    even under `--dry-run`: that resolution is the whole point of the flag,
    and without it `--dry-run --parent` would say nothing about whether the
    path exists.

    In a dry run nothing is recorded in the store: no publish was attempted,
    so there is nothing pending to recover later.

    With `--show-blocks`, stdout carries only the JSON (so it can be piped);
    the human-readable preview goes to stderr. The metadata callout is
    inserted as the FIRST block, matching the API writer's own ordering.
    """
    parser = argparse.ArgumentParser(prog="notemcp", description=__doc__)
    parser.add_argument("input", nargs="?", help="raw text or a file path")
    parser.add_argument("--provider", help="e.g. gemini:gemini-3.5-flash-lite")
    parser.add_argument(
        "--destination",
        choices=[k.value for k in DestinationKind],
        help="where to publish: 'database' (a row in the Notas database, default) or "
        "'page' (a child page of NOTION_PARENT_PAGE_ID). Overrides NOTION_DESTINATION.",
    )
    parser.add_argument(
        "--parent",
        help="path of titles down to the parent page, starting from the root "
        "(e.g. 'College/Calculus I'). Implies --destination page. "
        "A title containing '/' is not supported by this separator — use --parent-id.",
    )
    parser.add_argument(
        "--parent-id",
        help="id of the parent page, bypassing title navigation (escape hatch). "
        "Implies --destination page.",
    )
    parser.add_argument(
        "--list-pages",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="list the subpages of the path (or the root, if omitted) and exit, "
        "without publishing.",
    )
    parser.add_argument("--hint", help="extra instruction for the formatter")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="format and print the result without writing to Notion",
    )
    parser.add_argument(
        "--show-blocks", action="store_true", help="print the generated blocks JSON"
    )
    parser.add_argument("--history", action="store_true", help="list recorded notes")
    parser.add_argument("--limit", type=int, default=20, help="how many notes to show in --history")
    parser.add_argument(
        "--retry", action="store_true", help="resend pending notes to Notion"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="diagnose configuration, SDK, and available providers",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="with --doctor: makes ONE real call to Gemini and measures latency "
        "(opt-in — uses your quota; never used by CI or build.py)",
    )
    args = parser.parse_args(argv)

    if args.doctor:
        from .doctor import main as run_doctor

        return run_doctor(argv)

    if args.parent and args.parent_id:
        parser.error("--parent and --parent-id are mutually exclusive")
    if (args.parent or args.parent_id) and args.destination == "database":
        parser.error("--parent/--parent-id imply --destination page; do not combine with --destination database")

    settings = load_settings()

    if args.list_pages is not None:
        return cmd_list_pages(settings, args.list_pages)
    if args.history:
        return cmd_history(settings, args.limit)
    if args.retry:
        return cmd_retry(settings, args.parent_id)

    destination_kind = (
        DestinationKind(args.destination) if args.destination else settings.destination_kind()
    )
    if args.parent or args.parent_id:
        destination_kind = DestinationKind.PAGE

    if args.parent_id:
        destination = DestinationChoice.page(args.parent_id)
    elif args.parent:
        settings.require_notion(DestinationKind.PAGE)
        browser = build_browser(settings)
        try:
            node = resolve_page_path(
                browser, settings.notion_parent_page_id, _split_parent_path(args.parent)
            )
        except NotionBrowseError as exc:
            raise SystemExit(f"error: {exc}") from exc
        destination = DestinationChoice.page(node.id)
    else:
        destination = (
            DestinationChoice.database()
            if destination_kind is DestinationKind.DATABASE
            else DestinationChoice.page()
        )

    raw = _read_input(args.input).strip()
    if not raw:
        raise SystemExit("empty input")

    if not args.dry_run:
        settings.require_notion(destination_kind)

    store = None if args.dry_run else _make_store(settings)

    _warn_if_uncurated_model(args.provider or settings.llm_provider)

    try:
        pipeline = Pipeline.from_settings(settings, args.provider, store=store)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"formatting with {pipeline.provider.label}...", file=sys.stderr)
    try:
        draft = pipeline.format_note(raw, FormatContext(hint=args.hint), destination)
    except LLMError as exc:
        raise SystemExit(f"error: {exc}") from exc

    preview = sys.stderr if args.show_blocks else sys.stdout
    print(f"\n# {draft.title}  [destination: {destination_kind.value}]", file=preview)
    print(f"type: {draft.doc_type}   tags: {', '.join(draft.tags) or '-'}", file=preview)
    print(f"summary: {draft.summary}\n", file=preview)
    print(draft.body_md, file=preview)

    if args.show_blocks:
        blocks = md_to_blocks(draft.body_md)
        if destination_kind is DestinationKind.PAGE:
            blocks = [metadata_callout_block(draft), *blocks]
        print(f"\n--- {len(blocks)} blocks ---", file=sys.stderr)
        print(json.dumps(blocks, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("\n(dry-run: nothing was written to Notion)", file=sys.stderr)
        return 0

    try:
        ref = pipeline.publish(draft, destination=destination)
    except NotionWriteError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        print("The draft is saved — run `notemcp --retry` to resend.", file=sys.stderr)
        return 1

    print(f"\npublished: {ref.url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
