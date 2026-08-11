"""`pyproject.toml` metadata that the Gemini-only migration (Fase 3, R5)
depends on: `google-genai` promoted to a first-class dependency, with the
old `gemini` extra removed. A `pip install '.[gemini]'` left over from
before this migration must not silently succeed with a warning where a
failure was expected (risk #7 of the migration plan) — this test is what
catches that, since nothing else in the suite reads `pyproject.toml`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _load() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_google_genai_is_a_first_class_dependency():
    data = _load()
    dependencies = data["project"]["dependencies"]
    assert any(dep.startswith("google-genai") for dep in dependencies), (
        "google-genai precisa estar em [project.dependencies], não numa extra opcional "
        "— Gemini é o único provedor (Fase 3 R5)"
    )


def test_google_genai_has_a_real_floor_not_a_token_one():
    data = _load()
    dependencies = data["project"]["dependencies"]
    genai_dep = next(dep for dep in dependencies if dep.startswith("google-genai"))
    assert genai_dep != "google-genai>=1.0", (
        "piso >=1.0 é um token contra um pacote que já está em major 2.x — "
        "use um piso que reflita a versão de verdade em uso"
    )


def test_no_optional_dependency_still_offers_gemini():
    data = _load()
    extras = data["project"].get("optional-dependencies", {})
    assert "gemini" not in extras, (
        "o extra 'gemini' precisa ser removido — google-genai não é mais opcional"
    )
    for name, deps in extras.items():
        assert not any("google-genai" in dep for dep in deps), (
            f"google-genai não pode aparecer na extra {name!r} também — "
            "ficaria declarado duas vezes, uma delas mentindo que é opcional"
        )
