"""Discovery of available providers, to feed the UI dropdown.

Gemini is the only provider notemcp offers — see the README and ESTADO.md
for why the local Ollama backend was removed (§2 of the top-level plan:
routing every note through a cloud model is a deliberate, user-made
decision, not an oversight). The list still exists, and an unavailable
entry still carries a reason instead of disappearing, because "Gemini is
there, just missing the key" is more useful than no option at all — the
same reasoning that applied to Ollama before it, now with one provider
instead of two.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

GEMINI_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro")
"""Gemini models offered in the dropdown. A curated list instead of
`models.list()` because the API returns dozens of variants (embeddings,
tuning, dated versions) that would only clutter the choice.

Verified against Google's own documentation (ago/2026): `gemini-3.6-flash`
is the current GA Flash model, `gemini-3.1-pro` is the current GA flagship
(NOT `gemini-3.1-pro-preview` — that name does not exist; the "-preview"
suffix belonged to an earlier, no-longer-current release). An earlier
version of this list carried both mistakes. `--provider`/`LLM_PROVIDER`
still accept any model name (see `build_provider` in `llm/base.py`) — this
tuple is what `--provider`/`LLM_PROVIDER` get *warned* against, via
`cli.py`'s `_warn_if_uncurated_model`, not validated against."""

DEFAULT_GEMINI_MODEL = GEMINI_MODELS[0]
"""The model `config.py`'s default `LLM_PROVIDER` points at. Kept as a
plain string literal in `config.py` too, deliberately not imported from
here — `config` importing `llm.registry` at module scope would make
`Settings()` pay for `dataclasses`/`google.genai`'s discovery machinery on
every import. `tests/test_config.py` asserts the two literals agree, which
is the right kind of coupling for a value that has to match without
following an import chain to prove it."""


@dataclass
class ProviderInfo:
    id: str
    label: str
    backend: str
    available: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def discover(settings) -> list[ProviderInfo]:
    return _discover_gemini(settings)


def _discover_gemini(settings) -> list[ProviderInfo]:
    """List every curated Gemini model, or a disabled placeholder explaining why not.

    Catches `Exception`, not just `ImportError`: a native library failing to
    load underneath `google.genai` — the OpenSSL/libcrypto mismatch
    catalogued in ESTADO.md §5 raised `ImportError` too, just not the "not
    installed" kind — must not be reported as "not installed" when it
    plainly is. That translation is what sent an earlier session chasing
    the wrong fix. The exception's own type name goes into the reason so
    the message is something to search for, not a false "not installed" on
    a build where the package is right there.

    With Gemini as the only provider (see the module docstring), this
    function alone decides what `discover()` returns — it must never come
    back with an empty list, since that reads to the UI/CLI exactly like
    "nothing configured yet" instead of "here is what's wrong".
    """
    try:
        import google.genai  # noqa: F401

        installed = True
        reason = ""
    except Exception as exc:  # noqa: BLE001
        installed = False
        reason = f"Gemini — failed to load the SDK: {type(exc).__name__}: {exc}"

    if installed and not settings.gemini_api_key:
        reason = "GEMINI_API_KEY is not set"

    return [
        ProviderInfo(
            id=f"gemini:{model}",
            label=f"{model}  ·  cloud",
            backend="gemini",
            available=installed and bool(settings.gemini_api_key),
            reason=reason,
        )
        for model in GEMINI_MODELS
    ]
