"""`LLMProvider` port and registry.

The provider choice is always explicit (the `LLM_PROVIDER` config or the UI
dropdown). **There is no automatic fallback between providers**: if the
selected one fails, the error propagates as `LLMError` for the caller to
decide what to do.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import ValidationError

from ..models import DOC_TYPES, FormatContext, NoteDraft
from . import prompts

NOTE_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
        "tags": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "body_md": {"type": "string"},
    },
    "required": ["title", "doc_type", "tags", "summary", "body_md"],
}
"""Schema handed to the provider as structured output. Written by hand
instead of via `NoteDraft.model_json_schema()` because the subset Gemini
accepts does not include everything Pydantic emits (maxLength, $defs,
etc)."""


class LLMError(RuntimeError):
    """Failure generating/validating the draft. Carries the provider id so
    the error message makes clear *which* backend failed."""

    def __init__(self, provider_id: str, message: str) -> None:
        super().__init__(f"[{provider_id}] {message}")
        self.provider_id = provider_id


@runtime_checkable
class LLMProvider(Protocol):
    id: str
    label: str

    def format_note(self, raw: str, ctx: FormatContext) -> NoteDraft: ...


def _strip_fences(text: str) -> str:
    """Strip Markdown code fences a small model may wrap around the JSON
    despite structured output being requested."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return text


def generate_draft(
    provider_id: str,
    raw: str,
    ctx: FormatContext,
    call: Callable[[str, str, str | None], str],
) -> NoteDraft:
    """Shared loop: generate, validate, and attempt one repair.

    `call(system, user, repair)` is what each adapter implements — it
    receives the prompts and returns the model's raw text. Both providers
    use native structured output, so the repair path is a safety net, not
    the normal path. Any exception `call` raises other than `LLMError`
    itself is translated into `LLMError` at this boundary, so callers only
    ever have to handle one exception type from this function.
    """
    system = prompts.system_prompt(ctx)
    user = prompts.user_prompt(raw, ctx)

    try:
        text = call(system, user, None)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(provider_id, f"model call failed: {exc}") from exc

    try:
        return NoteDraft.model_validate_json(_strip_fences(text))
    except (ValidationError, ValueError) as first_error:
        try:
            retry = call(system, user, prompts.repair_prompt(text, str(first_error)))
            return NoteDraft.model_validate_json(_strip_fences(retry))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise LLMError(
                provider_id,
                f"model did not return valid JSON even after repair: {exc}",
            ) from exc
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(provider_id, f"repair call failed: {exc}") from exc


def build_provider(spec: str, settings: Any) -> LLMProvider:
    """Instantiate the provider from `"<backend>:<model>"`.

    Deliberately a late import: `--doctor`/`--version` and anything else
    that never actually formats a note should not pay the ~1s `google.genai`
    costs to import, just because this module was imported.
    """
    backend, _, model = spec.partition(":")
    backend = backend.strip().lower()
    model = model.strip()

    if backend == "ollama":
        raise ValueError(
            f"invalid provider: {spec!r} — the local provider (Ollama) was removed; "
            "change LLM_PROVIDER to 'gemini:<model>' in your .env"
        )
    if not model:
        raise ValueError(f"invalid provider: {spec!r} (use 'gemini:<model>')")

    if backend == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(
            model=model, api_key=settings.gemini_api_key, timeout=settings.llm_timeout
        )
    raise ValueError(f"unknown backend: {backend!r} (supported: gemini)")
