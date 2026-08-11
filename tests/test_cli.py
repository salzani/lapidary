"""CLI tests: `--destination`, argparse, and the destination echo in
`--show-blocks`/`--history`.

Every network call is avoided by swapping `Pipeline.from_settings` for a
fake — the point of these tests is destination resolution (flag beats
NOTION_DESTINATION), not the pipeline itself (already covered in
test_pipeline.py).
"""

import json

import pytest

from notemcp import cli
from notemcp.models import DestinationChoice, NoteDraft

DRAFT = NoteDraft(
    title="T", doc_type="nota", tags=[], summary="s", body_md="corpo",
)


class FakePipeline:
    """Replaces `Pipeline.from_settings`: only needs to return a fixed draft
    and record which `DestinationChoice` the CLI actually used."""

    last_destination = None

    def __init__(self, *a, **k):
        self.provider = type("P", (), {"label": "fake"})()

    def format_note(self, raw, ctx, destination):
        FakePipeline.last_destination = destination
        return DRAFT

    def publish(self, draft, destination=None):
        raise AssertionError("--dry-run não pode publicar")


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    FakePipeline.last_destination = None
    monkeypatch.setattr(cli.Pipeline, "from_settings", lambda *a, **k: FakePipeline())


def test_destination_flag_overrides_the_env(monkeypatch, capsys):
    """`--destination` wins over `NOTION_DESTINATION`, not the other way
    around."""
    monkeypatch.setenv("NOTION_DESTINATION", "page")

    cli.main(["texto cru", "--destination", "database", "--dry-run"])

    assert FakePipeline.last_destination == DestinationChoice.database()
    assert "[destino: database]" in capsys.readouterr().out


def test_destination_flag_absent_falls_back_to_the_env(monkeypatch, capsys):
    monkeypatch.setenv("NOTION_DESTINATION", "page")

    cli.main(["texto cru", "--dry-run"])

    assert FakePipeline.last_destination == DestinationChoice.page()
    assert "[destino: page]" in capsys.readouterr().out


def test_invalid_destination_is_rejected_by_argparse(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["texto cru", "--destination", "xpto", "--dry-run"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_show_blocks_includes_the_callout_first_for_page(capsys):
    cli.main(["texto cru", "--destination", "page", "--dry-run", "--show-blocks"])
    blocks = json.loads(capsys.readouterr().out)
    assert blocks[0]["type"] == "callout", "o callout precisa ser o PRIMEIRO bloco"


def test_show_blocks_has_no_callout_for_database(capsys):
    cli.main(["texto cru", "--destination", "database", "--dry-run", "--show-blocks"])
    blocks = json.loads(capsys.readouterr().out)
    assert all(b["type"] != "callout" for b in blocks)


def test_uncurated_gemini_model_warns_but_does_not_fail(capsys):
    """H6: `build_provider` stays permissive about the model name (the
    escape hatch for whatever Google ships after `GEMINI_MODELS` was last
    updated) — the CLI warns instead of failing."""
    exit_code = cli.main(["texto cru", "--dry-run", "--provider", "gemini:gemini-9.9-not-real"])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "aviso" in err
    assert "gemini-9.9-not-real" in err


def test_curated_gemini_model_does_not_warn(capsys):
    from notemcp.llm import registry

    cli.main(["texto cru", "--dry-run", "--provider", f"gemini:{registry.GEMINI_MODELS[0]}"])

    assert "aviso" not in capsys.readouterr().err
