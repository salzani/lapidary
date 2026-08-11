"""Self-diagnosis for a running notemcp, source or frozen.

This module exists because of a failure that could not be observed. A packaged
build reported that no model was available while the same code, run from the
source tree, listed every provider as ready. Nothing inside the executable
could be inspected: there was no way to ask it which `.env` it had resolved,
whether the Gemini SDK had imported, or what discovery had actually returned.

So it lives in `src/notemcp/`, not in `packaging/`. The same function runs both
ways, which turns "works from source, fails packaged" into two comparable
outputs of one routine — the diff between them is the bug. It also gets
ordinary test coverage, which a diagnostic wired only into the frozen entry
point never would.

Two rules govern everything here:

**Every section prints, always.** A probe that fails prints its traceback; it
never prints nothing. A section silently skipped on exception reads as "this
part is fine", which is the exact failure mode this module was written to end.

**Secrets are redacted, once.** The output is meant to be pasted into a bug
report. Every secret goes through `_redact`, so there is exactly one place to
get that right and one place to review.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .config import Settings, load_settings

OK = 0
"""At least one provider is usable."""

BROKEN = 1
"""The SDK is missing or broken, or discovery raised. CI and build.py fail."""

NO_KEY = 2
"""The SDK works but no API key is configured.

Distinct from `BROKEN` on purpose: a CI runner legitimately has no key, and
collapsing this into `BROKEN` would make the build unable to gate on anything,
while collapsing it into `OK` would make the gate meaningless.
"""


def _redact(value: str | None) -> str:
    """Describe a secret without disclosing it."""
    if not value:
        return "NOT SET"
    prefix = value[:4]
    return f"set (len {len(value)}, prefix {prefix}…)"


@dataclass
class Probe:
    """One step of the SDK check: what was tried, and what came back."""

    name: str
    ok: bool
    detail: str = ""
    traceback_text: str = ""


@dataclass
class DoctorReport:
    runtime: dict[str, str] = field(default_factory=dict)
    env_candidates: list[tuple[Path, bool, bool]] = field(default_factory=list)
    """(path, exists, is_winner) for every candidate, in search order."""
    env_var: str | None = None
    resolved_env: str | None = None
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    """name -> {"env var": ..., ".env file": ..., "effective": ...}"""
    plain_settings: dict[str, str] = field(default_factory=dict)
    probes: list[Probe] = field(default_factory=list)
    providers: list[tuple[str, bool, str, str]] = field(default_factory=list)
    """(id, available, backend, reason)"""
    discovery_error: str = ""
    verdict: int = BROKEN
    verdict_text: str = ""


def _runtime_section() -> dict[str, str]:
    frozen = bool(getattr(sys, "frozen", False))
    return {
        "sys.executable": sys.executable,
        "frozen": str(frozen),
        "sys._MEIPASS": str(getattr(sys, "_MEIPASS", None)),
        "python": platform.python_version(),
        "platform": sys.platform,
        "sys.path[:3]": ", ".join(sys.path[:3]) or "(empty)",
    }


def _env_section(report: DoctorReport) -> None:
    resolved = config._resolve_env_file()
    report.resolved_env = resolved
    report.env_var = os.environ.get("NOTEMCP_ENV")
    for candidate in config._env_search_paths():
        exists = candidate.is_file()
        winner = resolved is not None and str(candidate) == resolved
        report.env_candidates.append((candidate, exists, winner))


def _dotenv_values(path: str | None) -> dict[str, str]:
    """Parse a .env by hand, only to compare it against what Settings saw.

    Deliberately not python-dotenv: the question this answers is "does the file
    contain something that never reached Settings", and reusing the same parser
    would make an agreement between them prove nothing.
    """
    values: dict[str, str] = {}
    if not path:
        return values
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


SECRET_FIELDS = (
    ("NOTION_TOKEN", "notion_token"),
    ("NOTION_PARENT_PAGE_ID", "notion_parent_page_id"),
    ("NOTION_DATA_SOURCE_ID", "notion_data_source_id"),
    ("GEMINI_API_KEY", "gemini_api_key"),
)

PLAIN_FIELDS = (
    ("LLM_PROVIDER", "llm_provider"),
    ("LLM_TIMEOUT", "llm_timeout"),
    ("NOTION_WRITER", "notion_writer"),
    ("NOTION_DESTINATION", "notion_destination"),
    ("NOTES_DB", "notes_db"),
)


def _settings_section(report: DoctorReport, settings: Settings) -> None:
    """Record configuration, splitting each secret across its three sources.

    Three lines per secret rather than one, because an empty environment
    variable outranks the dotenv file in pydantic-settings' precedence. Printing
    only the effective value makes that case indistinguishable from "the file
    was never read", and those two have opposite fixes.
    """
    from_file = _dotenv_values(report.resolved_env)
    for env_name, attr in SECRET_FIELDS:
        report.secrets[env_name] = {
            "env var": _redact(os.environ.get(env_name)),
            ".env file": _redact(from_file.get(env_name)),
            "effective": _redact(str(getattr(settings, attr, "") or "")),
        }
    for env_name, attr in PLAIN_FIELDS:
        report.plain_settings[env_name] = str(getattr(settings, attr, ""))

    try:
        from .ui import state

        saved = state.load().get("provider") or "(none)"
    except BaseException as exc:  # noqa: BLE001
        saved = f"(unreadable: {type(exc).__name__})"
    report.plain_settings["state.json provider"] = saved


def _probe(name: str, fn) -> Probe:
    """Run one SDK check, converting any outcome into a reportable result.

    `BaseException` rather than `Exception`: a `SystemExit` from a misbehaving
    import hook, or an `OSError` surfacing from a native library, is exactly
    the kind of thing that would otherwise take the whole diagnostic down with
    it — and it is also exactly the kind of thing worth reporting.
    """
    try:
        return Probe(name=name, ok=True, detail=str(fn()))
    except BaseException as exc:  # noqa: BLE001
        return Probe(
            name=name,
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            traceback_text=traceback.format_exc(),
        )


def _sdk_probes() -> list[Probe]:
    """Check every import `llm/gemini.py` performs, in the order it performs them.

    This list mirrors `llm/gemini.py` deliberately. That module imports the SDK
    lazily — `google.genai` in `_get_client`, then `types` and `errors` inside
    `_call` — so a bundle can import the first and still fail at the moment the
    user presses Format. If the provider ever imports something new, add it
    here too, or this will report green on a build that cannot format.
    """

    def import_google():
        import google

        return f"__path__={getattr(google, '__path__', '<none>')}"

    def find_genai():
        spec = importlib.util.find_spec("google.genai")
        if spec is None:
            raise ModuleNotFoundError("find_spec('google.genai') returned None")
        return f"origin={spec.origin} loader={type(spec.loader).__name__}"

    def import_genai():
        import google.genai as genai_mod

        return f"version={getattr(genai_mod, '__version__', '?')} file={genai_mod.__file__}"

    def has_client():
        from google import genai

        if not hasattr(genai, "Client"):
            raise AttributeError("google.genai imported but has no Client attribute")
        return "google.genai.Client present"

    def import_submodules():
        from google.genai import errors, types  # noqa: F401

        return "google.genai.types and .errors import cleanly"

    return [
        _probe("import google", import_google),
        _probe("find_spec google.genai", find_genai),
        _probe("import google.genai", import_genai),
        _probe("google.genai.Client", has_client),
        _probe("google.genai.types / .errors", import_submodules),
    ]


def _discovery_section(report: DoctorReport, settings: Settings) -> None:
    from .llm import registry

    try:
        for info in registry.discover(settings):
            report.providers.append((info.id, info.available, info.backend, info.reason))
    except BaseException:  # noqa: BLE001
        report.discovery_error = traceback.format_exc()


def _decide(report: DoctorReport) -> None:
    if report.discovery_error or not all(p.ok for p in report.probes):
        report.verdict = BROKEN
        report.verdict_text = "SDK or discovery is broken — this build cannot format notes."
        return
    if any(available for _, available, _, _ in report.providers):
        report.verdict = OK
        report.verdict_text = "At least one provider is ready."
        return
    report.verdict = NO_KEY
    report.verdict_text = "SDK is fine, but no provider is configured (missing API key?)."


def report(settings: Settings | None = None) -> DoctorReport:
    """Collect the full diagnosis. Pure: gathers, never prints."""
    result = DoctorReport()
    result.runtime = _runtime_section()
    _env_section(result)
    resolved = settings or load_settings()
    _settings_section(result, resolved)
    result.probes = _sdk_probes()
    _discovery_section(result, resolved)
    _decide(result)
    return result


def render(result: DoctorReport) -> str:
    lines: list[str] = ["Lapidary doctor", "=" * 60, "", "1. Runtime"]
    for key, value in result.runtime.items():
        lines.append(f"   {key:16} {value}")

    lines += ["", "2. .env resolution"]
    lines.append(f"   NOTEMCP_ENV      {result.env_var or '(unset)'}")
    for path, exists, winner in result.env_candidates:
        mark = "USED  " if winner else ("exists" if exists else "  --  ")
        lines.append(f"   [{mark}] {path}")
    lines.append(f"   resolved         {result.resolved_env or '(none — no .env found)'}")

    lines += ["", "3. Configuration"]
    for name, sources in result.secrets.items():
        lines.append(f"   {name}")
        for source, value in sources.items():
            lines.append(f"      {source:12} {value}")
    for name, value in result.plain_settings.items():
        lines.append(f"   {name:22} {value}")

    lines += ["", "4. Gemini SDK"]
    for probe in result.probes:
        status = "ok  " if probe.ok else "FAIL"
        lines.append(f"   [{status}] {probe.name}: {probe.detail}")
        if probe.traceback_text:
            lines += ["          " + t for t in probe.traceback_text.rstrip().splitlines()]

    lines += ["", "5. Provider discovery"]
    if result.discovery_error:
        lines.append("   discover() RAISED:")
        lines += ["      " + t for t in result.discovery_error.rstrip().splitlines()]
    elif not result.providers:
        lines.append("   (none returned)")
    else:
        for provider_id, available, backend, reason in result.providers:
            status = "ok  " if available else "FAIL"
            lines.append(f"   [{status}] {provider_id:34} {reason}")

    lines += ["", "6. Verdict", f"   exit {result.verdict}: {result.verdict_text}", ""]
    return "\n".join(lines)


def _run_live_probe(settings: Settings) -> None:
    """`--doctor --live`: make exactly ONE real call to Gemini and report
    latency plus whether a valid `NoteDraft` came back.

    Opt-in only, by design: everything else in this module — every probe,
    every test in `tests/test_doctor.py`, `packaging/build.py`'s smoke
    test, and CI — must never touch the network (see the top-level
    project's rule on that) or spend a real API call. This function is the
    one deliberate exception, gated behind an explicit flag nobody calls
    for you.
    """
    print()
    print("7. Live probe (--live)")
    if not settings.gemini_api_key:
        print("   skipped: GEMINI_API_KEY not set.")
        return

    import time

    from .llm.base import LLMError, build_provider
    from .models import FormatContext

    spec = settings.llm_provider
    if not spec.startswith("gemini:"):
        from .llm.registry import DEFAULT_GEMINI_MODEL

        spec = f"gemini:{DEFAULT_GEMINI_MODEL}"

    try:
        provider = build_provider(spec, settings)
    except ValueError as exc:
        print(f"   FAIL: could not build provider {spec!r}: {exc}")
        return

    start = time.monotonic()
    try:
        draft = provider.format_note(
            "Reunião rápida às 10h sobre o roadmap do projeto para o próximo trimestre.",
            FormatContext(),
        )
    except LLMError as exc:
        print(f"   FAIL after {time.monotonic() - start:.2f}s: {exc}")
        return
    except BaseException as exc:  # noqa: BLE001
        print(f"   FAIL after {time.monotonic() - start:.2f}s: {type(exc).__name__}: {exc}")
        return

    print(f"   OK in {time.monotonic() - start:.2f}s — {spec!r} returned a valid NoteDraft")
    print(f"   title: {draft.title!r}")


def main(argv: list[str] | None = None) -> int:
    """Print the diagnosis and return the exit code callers gate on.

    `--live` (in `argv`) additionally runs `_run_live_probe` after the
    report — opt-in, and the only thing in this module that spends a real
    API call. Every other caller (tests, CI, `build.py`) must never pass it.
    """
    argv = argv or []
    result = report()
    print(render(result))
    if "--live" in argv:
        _run_live_probe(load_settings())
    return result.verdict
