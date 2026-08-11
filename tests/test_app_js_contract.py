"""Structural regression tests for the three silent `app.js` bugs
(Batch 1, Part A: C1, C2, C3).

There is no JavaScript runtime in this project (`node` is not available,
see ESTADO.md, "the JS<->Python boundary has no automated test coverage")
— so these tests do NOT execute `app.js`. They perform the cheapest check
that can be done from the Python side without a real JS engine: parse the
source text and confirm that the pattern which fixes each bug is still
there. This is a structural safety net, not a behavioral one — it catches
an accidental revert of the fix, not a new logic regression written in a
textually different way. Real behavioral coverage is left for when (if) an
alternative such as Playwright/QtWebEngine joins the project; that is
recorded debt, not something this file solves.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).parent.parent / "src" / "notemcp" / "ui" / "web" / "app.js"


def _read_app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """Body of `function name(...) { ... }`, found by brace counting.

    Not a real JS parser — just enough for the tests in this file, which
    look for substrings inside the body of a specific function.
    """
    match = re.search(rf"function {re.escape(name)}\([^)]*\)\s*{{", source)
    assert match, f"função `{name}` não encontrada em app.js"
    depth = 1
    i = match.end()
    while depth > 0:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[match.end() : i - 1]


def test_bridge_failed_clears_a_pending_chain_request():
    """Regression test for bug C1: `chainRequest` never went back to `null`
    on the error path, so a SINGLE network failure during the chain left
    `chain-add`/`chain-reload` disabled and `handlePageTree` discarding
    every future response for the rest of the session. The fix connects
    `bridge.failed` to a named handler that clears `chainRequest` when a
    request is in flight."""
    source = _read_app_js()
    connect_match = re.search(r"bridge\.failed\.connect\((\w+)\)", source)
    assert connect_match, "bridge.failed precisa estar conectado a uma função nomeada, não a uma arrow inline"

    body = _function_body(source, connect_match.group(1))
    assert "chainRequest = null" in body, (
        "o handler de bridge.failed precisa limpar chainRequest quando há uma navegação em voo"
    )


def test_create_child_page_call_sites_arm_the_chain_request_first():
    """Regression test for bug C2: success of `createChildPage` emits
    `pageTreeReady` AND `parentPageChanged`, but without `chainRequest`
    armed BEFORE the call, `handlePageTree` discarded the first one as an
    orphan response — the label changed, the deepest `<select>` did not.
    The two known call sites (`submitChainCreate` and the "Criar outra
    assim mesmo" button) must arm the request before calling
    `bridge.createChildPage`."""
    source = _read_app_js()
    calls = [m.start() for m in re.finditer(r"bridge\.createChildPage\(", source)]
    assert len(calls) == 2, "esperava os dois call sites conhecidos do plano (linhas originais 410 e 487)"

    for pos in calls:
        window = source[max(0, pos - 400) : pos]
        assert "requestPageTree(" in window or "armDeepestLevelRefresh()" in window, (
            "bridge.createChildPage precisa ser precedido de um arme de chainRequest "
            "(diretamente ou via armDeepestLevelRefresh)"
        )


def test_no_provider_available_message_is_identical_in_python_and_js():
    """R4: `ui/bridge.py::formatNote` and `ui/web/app.js`'s
    `providersReady` handler show the same failure for the same situation
    (no available provider) — divergent wording here is invisible to the
    test suite (nothing executes the JS), so pin both sides to the exact
    same string instead of trusting them to stay in sync by hand."""
    bridge_path = APP_JS.parent.parent / "bridge.py"
    bridge_source = bridge_path.read_text(encoding="utf-8")

    match = re.search(r'"(Nenhum provedor disponível[^"]*)"', bridge_source)
    assert match, "mensagem de 'nenhum provedor' não encontrada em bridge.py"
    python_message = match.group(1)

    app_js_source = _read_app_js()
    assert python_message in app_js_source, (
        "app.js precisa mostrar exatamente a mesma mensagem que bridge.py emite "
        f"para 'nenhum provedor disponível': {python_message!r}"
    )


def test_providers_ready_handler_has_a_single_exit_path():
    """H2: the old handler `return`ed right after disabling the provider
    `<select>` on an empty list — which also skipped disabling the Format
    button and skipped the toast, so the UI looked merely idle instead of
    telling the user nothing is usable. The fix folds the empty-list case
    into `usable === false` instead of a separate early return, so both the
    button state and the toast are reachable from every case. Asserting
    there is no `return` in the body is the durable half of this test: it
    makes the whole *class* of bug (a branch that quietly skips the shared
    tail) impossible to reintroduce silently, not just this one instance.
    """
    source = _read_app_js()
    connect_match = re.search(
        r"bridge\.providersReady\.connect\(\(payload\)\s*=>\s*{", source
    )
    assert connect_match, "bridge.providersReady precisa estar conectado a uma arrow function"

    depth = 1
    i = connect_match.end()
    while depth > 0:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    body = source[connect_match.end() : i - 1]

    assert '$("format").disabled' in body
    assert "return" not in body, (
        "o handler de providersReady não pode ter um caminho de saída antecipado — "
        "isso é exatamente o que deixou o botão Formatar habilitado com a lista vazia"
    )


def test_ensure_chain_loaded_reverts_the_flag_on_failure():
    """Regression test for bug C3: `chainLoaded` was set to `true` BEFORE
    the `openPageTree()` response arrived — if it failed (e.g.
    NOTION_PARENT_PAGE_ID empty), `chainLoaded` stayed `true` forever and
    `ensureChainLoaded` never tried again, permanently stuck showing
    "carregando…" in the chain."""
    source = _read_app_js()
    body = _function_body(source, "ensureChainLoaded")
    assert "chainLoaded = false" in body, (
        "ensureChainLoaded precisa reverter chainLoaded quando a requisição armada falhar"
    )
