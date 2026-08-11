"""Gemini provider (free tier) via google-genai, with structured output."""

from __future__ import annotations

from ..models import FormatContext, NoteDraft
from .base import NOTE_DRAFT_SCHEMA, LLMError, generate_draft


class GeminiProvider:
    def __init__(self, model: str, api_key: str, timeout: float = 180.0):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.id = f"gemini:{model}"
        self.label = f"Gemini — {model}"
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise LLMError(self.id, "GEMINI_API_KEY is not set")
            try:
                from google import genai
            except ImportError as exc:
                raise LLMError(
                    self.id, "google-genai not installed — `pip install 'notemcp[gemini]'`"
                ) from exc
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def format_note(self, raw: str, ctx: FormatContext) -> NoteDraft:
        return generate_draft(self.id, raw, ctx, self._call)

    def _call(self, system: str, user: str, repair: str | None) -> str:
        """Call Gemini and return the raw response text.

        A 429 is common on the free tier, so it gets its own message instead
        of the generic "API error" — it is the most likely failure a user
        will hit here.
        """
        client = self._get_client()
        from google.genai import errors as genai_errors
        from google.genai import types

        contents: list = [user]
        if repair:
            contents.append(repair)

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=NOTE_DRAFT_SCHEMA,
            temperature=0.2,
            http_options=types.HttpOptions(timeout=int(self.timeout * 1000)),
        )

        try:
            resp = client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except genai_errors.ClientError as exc:
            if getattr(exc, "code", None) == 429:
                raise LLMError(
                    self.id, "free tier rate limit reached — wait a moment and try again"
                ) from exc
            raise LLMError(self.id, f"API error: {exc}") from exc

        text = (resp.text or "").strip()
        if not text:
            raise LLMError(self.id, "empty response (possibly blocked by a safety filter)")
        return text
