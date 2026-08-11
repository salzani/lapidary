"""H6: `GEMINI_MODELS` (`llm/registry.py`) is the single source of truth
for which Gemini model names this project claims to support. This test
scans every place a model name is written by hand — the README and the
CLI's `--provider` help text — and asserts every `gemini-...` name found
there is one this project actually curates.

This is what would have caught the two mistakes an earlier session made:
`gemini-3.1-pro-preview` (does not exist — the flagship is `gemini-3.1-pro`,
GA) and stale references to `gemini-2.5-flash` (a previous generation).
"""

from __future__ import annotations

import re
from pathlib import Path

from notemcp.llm import registry

REPO_ROOT = Path(__file__).parent.parent
MODEL_NAME_RE = re.compile(r"gemini-[A-Za-z0-9._-]+")


def _model_names_in(text: str) -> set[str]:
    return set(MODEL_NAME_RE.findall(text))


def test_every_model_name_in_the_readme_is_curated():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    found = _model_names_in(readme)

    assert found, "esperava encontrar pelo menos um nome de modelo Gemini no README"
    uncurated = found - set(registry.GEMINI_MODELS)
    assert not uncurated, (
        f"README.md cita modelo(s) fora de registry.GEMINI_MODELS: {sorted(uncurated)}"
    )


def test_every_model_name_in_the_provider_help_is_curated():
    """Reads `cli.py`'s source text directly rather than constructing the
    real `argparse.ArgumentParser` — the point is to check what a human
    reading `--help` sees, without needing to reach into argparse's
    internals to get there."""
    cli_source = (REPO_ROOT / "src" / "notemcp" / "cli.py").read_text(encoding="utf-8")

    match = re.search(r'"--provider",\s*help="([^"]*)"', cli_source)
    assert match, "--provider precisa declarar um help= com um exemplo de modelo"
    help_text = match.group(1)

    found = _model_names_in(help_text)
    assert found, "esperava um exemplo de modelo Gemini no help de --provider"
    uncurated = found - set(registry.GEMINI_MODELS)
    assert not uncurated, (
        f"help de --provider cita modelo(s) fora de registry.GEMINI_MODELS: {sorted(uncurated)}"
    )
