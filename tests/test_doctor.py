"""Tests for `src/notemcp/doctor.py`.

Two things make this module worth testing on its own, separately from
everything that already exercises `registry.discover`/`Settings`:

- **Redaction is asserted on the rendered text**, not on the dataclass. A
  `DoctorReport` field holding `"set (len 53, prefix secr…)"` proves nothing
  about what actually reaches a terminal or a pasted bug report — only
  `render()`'s output does.
- **A broken SDK must not take the diagnostic down with it.** The whole
  point of `doctor.py` is to survive the exact class of failure it exists to
  describe (see its module docstring): an import that fails deep inside a
  native library, the way the OpenSSL/libcrypto bug did.
"""

from __future__ import annotations

import sys

import pytest

from notemcp import cli, doctor
from notemcp.config import Settings
from notemcp.llm import registry


@pytest.fixture(autouse=True)
def _isolated_env_resolution(tmp_path, monkeypatch):
    """Same discipline as `tests/test_config.py`: nothing here should read
    this machine's real `.env` — a doctor test passing (or failing)
    depending on what happens to sit at the repo root is not a test.
    """
    monkeypatch.delenv("NOTEMCP_ENV", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("notemcp.config.Path.home", staticmethod(lambda: fake_home))


@pytest.fixture(autouse=True)
def _never_touch_the_network(monkeypatch):
    """`doctor.report()` calls `registry.discover()` for real.

    Before Fase 3 removes Ollama, `_discover_ollama` probes
    `http://localhost:11434` with a real `httpx.Client` — fake it exactly
    like `tests/test_ui.py::fake_ollama` does, so this file never attempts a
    connection, refused or not. Once Ollama is gone, `registry` has no
    `httpx` attribute left to patch and this is a no-op.
    """
    if not hasattr(registry, "httpx"):
        return

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise registry.httpx.ConnectError("blocked in tests")

    monkeypatch.setattr(registry.httpx, "Client", _FakeClient)


class _BoomFinder:
    """A meta path finder that fails the way the OpenSSL/libcrypto bug did:
    the import raises deep inside the loader, not a clean
    `ModuleNotFoundError` — exactly the shape `_probe` exists to survive.
    """

    def find_spec(self, fullname, path, target=None):
        if fullname == "google" or fullname.startswith("google."):
            raise OSError("libcrypto.so.3: version `OPENSSL_3.3.0' not found")
        return None


def _inject_broken_google_import(monkeypatch):
    for name in list(sys.modules):
        if name == "google" or name.startswith("google."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    finder = _BoomFinder()
    sys.meta_path.insert(0, finder)
    return finder


def test_no_key_gemini_is_listed_unavailable_with_verdict_no_key():
    settings = Settings(notion_token="t", notion_parent_page_id="p")

    result = doctor.report(settings=settings)

    assert result.providers, "discover() nunca deve devolver lista vazia"
    assert not any(available for _, available, _, _ in result.providers)
    assert result.verdict == doctor.NO_KEY


def test_redaction_never_leaks_a_secret_into_the_rendered_text():
    """Assert on `render()`'s output, not on `DoctorReport` fields — a
    dataclass holding the redacted string proves nothing about what a
    future line change might print unredacted."""
    token = "secret_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    key = "AIzaSyDprettylongfakegeminikey1234567890abcd"
    settings = Settings(notion_token=token, gemini_api_key=key, notion_parent_page_id="p")

    rendered = doctor.render(doctor.report(settings=settings))

    assert token not in rendered
    assert key not in rendered
    assert "set (len" in rendered, "a forma redigida precisa aparecer em algum lugar"


def test_injected_import_failure_yields_broken_verdict_without_raising(monkeypatch):
    finder = _inject_broken_google_import(monkeypatch)
    try:
        result = doctor.report(settings=Settings(notion_token="t", notion_parent_page_id="p"))
    finally:
        sys.meta_path.remove(finder)

    assert result.verdict == doctor.BROKEN
    assert not all(p.ok for p in result.probes)
    assert any("OSError" in p.detail for p in result.probes)


def test_render_names_every_env_search_path_candidate():
    from notemcp import config

    settings = Settings(notion_token="t", notion_parent_page_id="p")
    rendered = doctor.render(doctor.report(settings=settings))

    for candidate in config._env_search_paths():
        assert str(candidate) in rendered


def test_cli_doctor_runs_without_notion_token(capsys):
    exit_code = cli.main(["--doctor"])

    out = capsys.readouterr().out
    assert exit_code in (doctor.OK, doctor.NO_KEY, doctor.BROKEN)
    assert "Lapidary doctor" in out
    assert "NOTION_TOKEN" in out


def test_live_probe_is_never_run_without_the_flag(monkeypatch, capsys):
    """H7.2: `--live` is opt-in. Every other caller — including this test
    suite by default — must never spend a real API call."""
    calls = []
    monkeypatch.setattr(doctor, "_run_live_probe", lambda settings: calls.append(settings))

    doctor.main([])

    assert calls == []
    assert "Live probe" not in capsys.readouterr().out


def test_live_probe_runs_only_with_the_live_flag(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(doctor, "_run_live_probe", lambda settings: calls.append(settings))

    doctor.main(["--doctor", "--live"])

    assert len(calls) == 1


def test_live_probe_skips_without_a_key_and_touches_nothing(capsys):
    """With no `GEMINI_API_KEY`, `_run_live_probe` must return immediately
    — this is what keeps `doctor.main(["--live"])` safe to call in this
    test file's isolated environment without a real key."""
    doctor._run_live_probe(Settings(notion_token="t"))

    out = capsys.readouterr().out
    assert "skipped" in out
    assert "GEMINI_API_KEY" in out


def test_live_probe_reports_failure_without_raising(monkeypatch, capsys):
    """A broken/misconfigured provider must be reported like every other
    probe in this module, never propagated. `_run_live_probe` imports
    `build_provider` lazily from `llm.base` (same reasoning as everywhere
    else in this project — see `llm/base.py::build_provider`'s own
    docstring), so it is patched there, not on `doctor` itself."""
    from notemcp.llm import base as llm_base
    from notemcp.llm.base import LLMError

    class BoomProvider:
        id = "gemini:boom"
        label = "Boom"

        def format_note(self, raw, ctx):
            raise LLMError(self.id, "rate limit do free tier atingido")

    monkeypatch.setattr(llm_base, "build_provider", lambda spec, settings: BoomProvider())

    doctor._run_live_probe(Settings(notion_token="t", gemini_api_key="fake-key"))

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "rate limit" in out


def test_all_six_sections_appear_even_in_a_fully_broken_environment(monkeypatch):
    """A section skipped by an exception reads as "this part is fine" — the
    exact failure mode `doctor.py`'s own docstring says it exists to end.
    Every one of the six section headers must survive both a broken SDK
    import AND a `discover()` that raises.
    """
    finder = _inject_broken_google_import(monkeypatch)

    def boom_discover(settings):
        raise RuntimeError("discovery totalmente quebrada")

    monkeypatch.setattr(registry, "discover", boom_discover)

    try:
        result = doctor.report(settings=Settings(notion_token="t"))
    finally:
        sys.meta_path.remove(finder)

    rendered = doctor.render(result)
    for heading in (
        "1. Runtime",
        "2. .env resolution",
        "3. Configuration",
        "4. Gemini SDK",
        "5. Provider discovery",
        "6. Verdict",
    ):
        assert heading in rendered, f"seção ausente: {heading!r}"

    assert result.discovery_error
    assert result.verdict == doctor.BROKEN
