/**
 * Frontend controller for the NoteMCP desktop UI, hosted in a
 * QWebEngineView (see `src/notemcp/ui/app.py`).
 *
 * This module owns all DOM rendering and talks to the Python side through
 * the QWebChannel `bridge` object exposed by `src/notemcp/ui/bridge.py`.
 * Every `bridge.<method>(...)` call here must match, in argument count and
 * order, the corresponding `@Slot` signature on the Python `Bridge` class —
 * QWebChannel does not raise a visible error on an arity mismatch, it just
 * silently does nothing (see ESTADO.md, bug 12). There is no JS runtime in
 * this project's test suite, so `tests/test_app_js_contract.py` checks the
 * riskiest fixes structurally, by reading this file's source text.
 *
 * Layout of this file:
 *  - theme toggle
 *  - toasts / busy indicator
 *  - draft panel (form fields <-> markdown/preview tabs)
 *  - destination selector (Notion database vs. a loose page)
 *  - history drawer
 *  - parent-page dropdown chain (Phase 6 — see the section below for the
 *    contract between this file and `ui/bridge.py`)
 *  - QWebChannel wiring (signal handlers, initial slot calls)
 *
 * All user-facing strings (labels, toasts, placeholders) and this
 * documentation are in English.
 */

"use strict";

const $ = (id) => document.getElementById(id);
let bridge = null;
let toastTimer = null;

/**
 * The destination used by "Publish" and by the preview renderer.
 *
 * Read at the moment of the click (via `currentDestination`), never
 * inferred from anywhere else — this avoids publishing to a destination
 * that is no longer the one selected on screen (see `ui/bridge.py`, risk
 * 7 in the original plan).
 */
let currentDestination = "database";

/**
 * Publish target (Phase 6): the SINGLE SOURCE for both the "Page: X"
 * footer label and the third argument of `bridge.publish`. Both must be
 * derived from these variables only, never from parallel state — that
 * guarantee is what prevents the displayed label from diverging from the
 * value actually published (risk 11 in the original plan). Updated ONLY
 * inside `renderParentPage`, which itself only runs in response to the
 * bridge's `parentPageChanged` signal.
 */
let currentParentPageId = "";
let currentParentPageTitle = "root";

/**
 * Ancestors of the publish target (root-first, target itself excluded).
 * Mirrors `_parent_page_path` in `ui/bridge.py`; it is what lets the boot
 * sequence rebuild the entire dropdown chain, not just level 1 — see
 * `initRootLevel` / `restoreChainPath`.
 */
let currentParentPagePath = [];

/**
 * Availability of the "loose page" destination, used to disable the
 * dropdown chain with a visible reason — same pattern as the provider
 * dropdown.
 */
let pageDestinationAvailable = true;
let pageDestinationReason = "";


const savedTheme = localStorage.getItem("theme") || "dark";
document.documentElement.dataset.theme = savedTheme;
$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
};


/**
 * Show a toast message, replacing whatever is currently displayed.
 *
 * @param {string} message - Text (or HTML, if `html` is true) to display.
 * @param {string} [kind] - "ok" | "err" | "" — controls the accent color and,
 *   for "err", a longer auto-dismiss delay.
 * @param {boolean} [html] - When true, `message` is set via `innerHTML`
 *   (used for the "Published" toast, which embeds a link).
 */
function toast(message, kind, html) {
  const el = $("toast");
  el.className = "toast " + (kind || "");
  if (html) el.innerHTML = message; else el.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), kind === "err" ? 12000 : 9000);
  el.classList.remove("hidden");
}

/**
 * Toggle the busy indicator and disable the actions it would conflict with.
 *
 * @param {boolean} active - Whether a background operation is running.
 * @param {string} [label] - Text shown next to the spinner.
 */
function setBusy(active, label) {
  $("busy").classList.toggle("hidden", !active);
  $("busy-label").textContent = label || "";
  $("format").disabled = active;
  $("publish").disabled = active;
  if (active) $("toast").classList.add("hidden");
}


/**
 * Build a `NoteDraft`-shaped object from the current form fields.
 *
 * @returns {{title: string, doc_type: string, tags: string[], summary: string, body_md: string}}
 */
function currentDraft() {
  return {
    title: $("title").value.trim(),
    doc_type: $("doc_type").value,
    tags: $("tags").value.split(",").map((t) => t.trim()).filter(Boolean),
    summary: $("summary").value.trim(),
    body_md: $("source").value,
  };
}

/**
 * Populate the draft form from a `NoteDraft` payload and reveal the panel.
 *
 * @param {{title: string, doc_type: string, tags: string[], summary: string, body_md: string}} draft
 */
function showDraft(draft) {
  $("title").value = draft.title;
  $("doc_type").value = draft.doc_type;
  $("tags").value = draft.tags.join(", ");
  $("summary").value = draft.summary;
  $("source").value = draft.body_md;
  $("empty").classList.add("hidden");
  $("draft").classList.remove("hidden");
  refreshPreview();
}

/**
 * Ask the bridge to render the current draft as HTML for the preview tab.
 *
 * No-op if the QWebChannel bridge has not connected yet.
 */
function refreshPreview() {
  if (!bridge) return;
  bridge.renderPreview(JSON.stringify(currentDraft()), currentDestination, (html) => {
    $("preview").innerHTML = html;
  });
}

/**
 * Wire the "preview" / "markdown" tab buttons: toggle which pane is
 * visible and refresh the rendered preview when switching into it.
 */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const source = tab.dataset.tab === "source";
    $("source").classList.toggle("hidden", !source);
    $("preview").classList.toggle("hidden", source);
    if (!source) refreshPreview();
  };
});

/** Re-render the preview with a debounce while the markdown is edited. */
let previewTimer = null;
$("source").addEventListener("input", () => {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 220);
});

$("raw").addEventListener("input", () => {
  $("raw-count").textContent = $("raw").value.length + " characters";
});


$("format").onclick = () => bridge && bridge.formatNote($("raw").value, $("hint").value);
$("publish").onclick = () => bridge &&
  bridge.publish(JSON.stringify(currentDraft()), currentDestination, currentParentPageId);
$("retry").onclick = () => bridge && bridge.retryPending();

/**
 * Render the destination `<select>` (Notion database vs. loose page) from
 * the `destinationsReady` payload and refresh the parent-chain
 * availability derived from it.
 *
 * @param {string} payload - JSON string: `{ destinations: [{kind, label, available, reason}], selected: string }`.
 */
function renderDestinations(payload) {
  const { destinations, selected } = JSON.parse(payload);
  const sel = $("destination");
  sel.innerHTML = "";

  for (const d of destinations) {
    const opt = document.createElement("option");
    opt.value = d.kind;
    opt.textContent = d.available ? d.label : `${d.label} — ${d.reason}`;
    opt.disabled = !d.available;
    if (d.kind === selected) opt.selected = true;
    sel.appendChild(opt);
  }
  currentDestination = selected;

  const pageDest = destinations.find((d) => d.kind === "page");
  pageDestinationAvailable = pageDest ? pageDest.available : false;
  pageDestinationReason = pageDest ? pageDest.reason : "";
  updateParentChainAvailability();
}


/**
 * Escape `< > & "` for safe interpolation into `innerHTML` template
 * strings.
 *
 * Every piece of text that ultimately comes from Notion or from the LLM
 * (page titles, history entries, error messages, URLs) is rendered via
 * `innerHTML` somewhere in this file — never via `textContent` — because
 * some of it legitimately contains markup (e.g. the "Published" toast
 * embeds an `<a>`). `esc()` is therefore applied to every such value
 * individually at the point of interpolation; skipping it on any single
 * field would open an HTML/script injection hole through Notion content
 * the user does not control (e.g. a page title someone else set).
 *
 * @param {*} s - Value to escape (coerced to string).
 * @returns {string}
 */
const esc = (s) => String(s).replace(/[<>&"]/g, (c) =>
  ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

/**
 * Only one drawer open at a time, sharing a single `#scrim`.
 * @type {?string} "history" | null
 */
let openDrawerId = null;

/**
 * Reveal the drawer with the given id and the shared scrim.
 *
 * @param {string} id - Element id of the drawer (currently only "history").
 */
function showDrawer(id) {
  $(id).classList.remove("hidden");
  $("scrim").classList.remove("hidden");
  openDrawerId = id;
}

/**
 * Hide the drawer with the given id and the shared scrim.
 *
 * @param {string} id - Element id of the drawer to hide.
 */
function hideDrawer(id) {
  $(id).classList.add("hidden");
  $("scrim").classList.add("hidden");
  if (openDrawerId === id) openDrawerId = null;
}

/** Close whichever drawer is currently open, if any. */
function closeOpenDrawer() {
  if (openDrawerId) hideDrawer(openDrawerId);
}

/** Open the history drawer and request a fresh history payload. */
function openHistory() {
  showDrawer("history");
  if (bridge) bridge.loadHistory();
}

/** Close the history drawer. */
function closeHistory() {
  hideDrawer("history");
}

$("history-btn").onclick = openHistory;
$("history-close").onclick = closeHistory;
$("scrim").onclick = closeOpenDrawer;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeOpenDrawer();
});

/**
 * Render the history drawer list and the pending badge/retry button.
 *
 * @param {string} payload - JSON string: `{ notes: [...], pending: number }`.
 */
function renderHistory(payload) {
  const { notes, pending } = JSON.parse(payload);

  const badge = $("pending-badge");
  badge.textContent = pending;
  badge.classList.toggle("hidden", !pending);
  $("retry").classList.toggle("hidden", !pending);

  const list = $("history-list");
  if (!notes.length) {
    list.innerHTML = '<li class="history-empty">No notes yet.</li>';
    return;
  }
  list.innerHTML = notes.map((n) => {
    let sub = "";
    if (n.url) sub = `<div class="h-sub"><a href="${esc(n.url)}">${esc(n.url)}</a></div>`;
    else if (n.provider) sub = `<div class="h-sub">${esc(n.provider)}</div>`;
    const err = n.error ? `<div class="h-err">${esc(n.error)}</div>` : "";
    return `<li>
      <div class="h-top">
        <span class="h-status ${esc(n.status)}" title="${esc(n.status)}"></span>
        <span class="h-title">${esc(n.title)}</span>
        <span class="h-when">${esc(n.when)}</span>
      </div>${sub}${err}
    </li>`;
  }).join("");
}

/**
 * Ctrl/Cmd+Enter shortcut: publish while focus is in the draft pane,
 * format while focus is in the raw-note pane.
 */
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    if (!$("draft").classList.contains("hidden") && $("pane-draft").contains(document.activeElement)) {
      $("publish").click();
    } else {
      $("format").click();
    }
  }
});


/**
 * State for the parent-page dropdown chain.
 *
 * The bridge keeps exactly ONE navigation cursor server-side
 * (`_browse_path` in `ui/bridge.py`), guarded against stale responses by
 * its own `_browse_seq`. `_browse_seq` guards the TIP of the navigation:
 * it makes sure the Python side never commits a response that arrived
 * after a newer request was already issued for the same cursor.
 *
 * `chainRequest` is a DIFFERENT, complementary guard, not a duplicate of
 * `_browse_seq`. It exists because this file renders MULTIPLE dropdown
 * levels from that single server-side cursor, and re-selecting a level in
 * the MIDDLE of the chain (not just the deepest one) requires two
 * sequential bridge calls (`browseTo` to reposition, then `browseInto` to
 * descend) before the cursor even reflects the new selection — see
 * `repositionCursorTo`. `chainRequest` is what lets this file track "the
 * one in-flight response the chain currently cares about" across those two
 * hops, independently of whatever `_browse_seq` is doing on the Python
 * side. Removing either guard reopens a distinct bug: without
 * `_browse_seq`, the Python cursor itself could regress to an older path;
 * without `chainRequest`, this file could apply a late response to the
 * wrong dropdown level even though the Python cursor is internally
 * consistent.
 *
 * `chainLevels` mirrors the server cursor one entry per displayed level:
 * `chainLevels[0]` is always the root, `chainLevels[i]` (i>0) is the child
 * picked in select `i-1`. Changing the selection at level L discards
 * (`.slice`) everything after it — the UI's answer to "chain of arbitrary
 * depth" from the original plan.
 *
 * @type {{id: string, title: string, children: (object[]|null), selectedChildId: string}[]}
 */
let chainLevels = [];
let chainRootId = "";
/** Whether `openPageTree()` has been called (and answered) this session. */
let chainLoaded = false;
/** @type {?{expectId: (string|null), onArrive: function(object): void, onFail: (function(): void|undefined)}} */
let chainRequest = null;
let pendingChainCreateTitle = "";

/** Whether the parent-page chain applies: destination is "page" and it's available. */
function chainEnabled() {
  return currentDestination === "page" && pageDestinationAvailable;
}

/**
 * Arm `chainRequest` so the next `pageTreeReady` (or `bridge.failed`, via
 * `handleBridgeFailed`) that arrives is routed to this call's callbacks.
 *
 * `onFail`, when passed, runs if the in-flight bridge call ends in
 * `bridge.failed` instead of `pageTreeReady` (see `handleBridgeFailed`).
 * This exists because a network failure partway through ANY chain
 * navigation used to leave `chainRequest` armed forever (fixed bug,
 * ESTADO.md bug 13 / "C1"): `renderChain` never re-enables "+"/"Recarregar"
 * while `chainRequest` is set, and `handlePageTree` would discard every
 * subsequent response because its id never matched the fossilized request.
 *
 * @param {string|null} expectId - Node id the arriving `pageTreeReady`
 *   payload must have (`data.node.id`), or `null` to accept any response
 *   (used for the very first load, where there is nothing to compare yet).
 * @param {function(object): void} onArrive - Called with the parsed payload
 *   once a matching response arrives.
 * @param {function(): void} [onFail] - Called if `bridge.failed` fires
 *   while this request is the pending one, before `chainRequest` is cleared.
 */
function requestPageTree(expectId, onArrive, onFail) {
  chainRequest = { expectId, onArrive, onFail };
}

/**
 * Single handler for the bridge's `pageTreeReady` signal.
 *
 * Only accepts a response that matches what the chain is currently
 * expecting (`chainRequest`) — never trusts arrival order, because an
 * abandoned navigation (the user picked a different level before the
 * previous response came back) can resolve after a more recent one.
 * Returns immediately, discarding the payload, if nothing is pending
 * (`chainRequest` is `null`) — that is an orphan response.
 *
 * @param {string} payload - JSON string: `{ node, children, can_go_up, ... }`.
 */
function handlePageTree(payload) {
  const data = JSON.parse(payload);
  if (!chainRequest) return;
  if (chainRequest.expectId !== null && chainRequest.expectId !== data.node.id) return;
  const onArrive = chainRequest.onArrive;
  chainRequest = null;
  onArrive(data);
  renderChain();
}

/**
 * Single handler for the bridge's `failed` signal.
 *
 * `bridge.failed` is the ONLY per-operation error channel the bridge
 * exposes (see `ui/bridge.py`) — every chain navigation failure (network,
 * invalid id, empty `NOTION_PARENT_PAGE_ID`) surfaces here, never through a
 * dedicated signal. That makes this the right place to clear `chainRequest`
 * when it is waiting for a response that will never arrive (fixed bug,
 * ESTADO.md bug 13 / "C1"): without this, a single failure would leave
 * `chain-add`/`chain-reload` disabled for the rest of the session
 * (`renderChain` keys off `!chainRequest`) and `handlePageTree` would
 * discard every subsequent response for not matching the fossilized
 * `expectId`.
 *
 * Accepted trade-off: if `failed` fires for a reason totally unrelated to
 * the chain while a chain navigation happens to be in flight (rare —
 * nothing else runs concurrently with it), the legitimate response that
 * arrives afterwards is discarded as an orphan; the chain resumes
 * responding on the next click, which is preferable to freezing the whole
 * session. `onFail`, when the pending request defined one, runs before the
 * re-render (e.g. `ensureChainLoaded` reverting `chainLoaded`).
 *
 * @param {string} msg - Human-readable error message, shown as an "err" toast.
 */
function handleBridgeFailed(msg) {
  toast(msg, "err");
  if (chainRequest) {
    const onFail = chainRequest.onFail;
    chainRequest = null;
    if (onFail) onFail();
    renderChain();
  }
}

/**
 * Trigger the initial `openPageTree()` load, once per session.
 *
 * `chainLoaded` is set to `true` here but only meant to become permanently
 * true once the response actually arrives — if `openPageTree()` fails
 * (e.g. empty `NOTION_PARENT_PAGE_ID`), `bridge.failed` fires and no
 * `pageTreeReady` ever comes. The `onFail` callback passed to
 * `requestPageTree` reverts `chainLoaded` to `false` in that case (fixed
 * bug, ESTADO.md bug 15 / "C3"): without it, the guard at the top of this
 * function would block any further attempt for the rest of the session and
 * `renderChain` would show "loading…" forever. Reverting lets the next
 * trigger from `updateParentChainAvailability` (e.g. switching the
 * destination away and back) try again.
 */
function ensureChainLoaded() {
  if (!bridge || chainLoaded || !chainEnabled()) return;
  chainLoaded = true;
  requestPageTree(null, initRootLevel, () => { chainLoaded = false; });
  bridge.openPageTree();
}

/**
 * Seed `chainLevels` with the root level from the first `pageTreeReady`
 * response, then kick off restoring a previously saved parent page (if
 * any) into the chain.
 *
 * Full restoration matters because the saved parent page can be at any
 * depth, not just a direct child of the root. `currentParentPagePath`
 * (ancestors, root included at index 0) and `currentParentPageId` (the
 * target) arrive via `parentPageChanged`, emitted by `ready()` BEFORE the
 * worker that fetches the root's children responds (see ESTADO.md, Phase
 * 6). Without walking the saved path here, a parent page that is a
 * grandchild of the root would never be found among the level-1
 * children's `selected_id`, and the screen would keep showing "root"
 * while the footer label said something else (risk 11).
 *
 * @param {object} data - Parsed `pageTreeReady` payload for the root node.
 */
function initRootLevel(data) {
  chainRootId = data.node.id;
  chainLevels = [{ id: data.node.id, title: data.node.title, children: data.children, selectedChildId: "" }];
  renderChain();

  if (!currentParentPageId) return;
  const ancestorIds = currentParentPagePath.slice(1).map((p) => p.id);
  restoreChainPath([...ancestorIds, currentParentPageId], 0, 0);
}

/**
 * Walk down the saved parent-page path one level at a time, repeating
 * `browseInto` — the bridge only exposes SEQUENTIAL navigation, there is
 * no single-step "jump straight to X". Stops at the first point where the
 * expected id no longer matches the loaded children: stale saved state
 * (the parent page moved or was deleted in Notion) must not fake a
 * selection that no longer exists.
 *
 * After each `browseInto`, `data.can_go_up` confirms the bridge cursor
 * actually advanced past the root — if it comes back `false`, the bridge
 * state is not what the restoration assumes (a race with another
 * navigation, for instance) and the recursion stops instead of stacking a
 * level on top of a wrong base.
 *
 * @param {string[]} ids - Ancestor ids (root excluded) followed by the target id.
 * @param {number} level - Index into `chainLevels` currently being filled in.
 * @param {number} i - Index into `ids` being resolved in this step.
 */
function restoreChainPath(ids, level, i) {
  if (i >= ids.length) return;
  const id = ids[i];
  const exists = (chainLevels[level].children || []).some((c) => c.id === id);
  if (!exists) return;

  chainLevels[level].selectedChildId = id;
  renderChain();

  const isLast = i === ids.length - 1;
  fetchChildrenInto(level, id, (data) => {
    if (!data.can_go_up) return;
    if (data.children.length > 0 && !isLast) restoreChainPath(ids, level + 1, i + 1);
  });
}

/**
 * Re-render the `#parent-chain` container from `chainLevels`, and toggle
 * the "+"/"Recarregar" buttons based on whether a navigation is in flight.
 *
 * While a navigation response is pending (`chainRequest` is set), the
 * bridge cursor (`_browse_path`) may no longer correspond to the deepest
 * level shown, so the "+"/"Recarregar" buttons stay disabled until it
 * settles — otherwise `createChildPage`/`reloadPageTree` would act on the
 * wrong node.
 */
function renderChain() {
  const container = $("parent-chain");
  const enabled = chainEnabled();

  if (!enabled) {
    const reason = currentDestination !== "page"
      ? 'available only for "Loose page"'
      : (pageDestinationReason || "unavailable");
    container.innerHTML = `<span class="dim chain-reason">${esc(reason)}</span>`;
  } else if (!chainLevels.length) {
    container.innerHTML = '<span class="dim chain-reason">loading…</span>';
  } else {
    container.innerHTML = chainLevels.map((level, i) => {
      const options = [`<option value="">${i === 0 ? "— Root page —" : "— here —"}</option>`]
        .concat((level.children || []).map((c) => {
          const label = c.ambiguous ? `${c.title} #${c.id.slice(-4)}` : c.title;
          const selected = c.id === level.selectedChildId ? " selected" : "";
          return `<option value="${esc(c.id)}"${selected}>${esc(label)}</option>`;
        }));
      return `<select class="chain-select" data-level="${i}" title="${esc(level.title)}">${options.join("")}</select>`;
    }).join("");
  }

  const controlsEnabled = enabled && chainLevels.length > 0 && !chainRequest;
  $("chain-add").disabled = !controlsEnabled;
  $("chain-reload").disabled = !controlsEnabled;
}

/**
 * React to a destination change: (re)load the chain if it just became
 * enabled, close any open "create page" panel if it became disabled, and
 * re-render either way.
 */
function updateParentChainAvailability() {
  if (chainEnabled()) {
    ensureChainLoaded();
  } else {
    closeChainCreate();
  }
  renderChain();
}

/**
 * Tell the bridge which node is the publish target: the root sentinel
 * clears it, anything else selects it explicitly.
 *
 * @param {string} nodeId - Id of the node to target, or `chainRootId` for the root.
 */
function setTarget(nodeId) {
  if (!bridge) return;
  if (nodeId === chainRootId) bridge.clearParentPage();
  else bridge.selectParentPage(nodeId);
}

/**
 * Reposition the bridge's cursor (`_browse_path`) back to the node at
 * level `L` before descending into a new child. Needed whenever the level
 * being changed is NOT the deepest one, because earlier navigation left
 * the cursor deeper than `L`.
 *
 * @param {number} L - Index into `chainLevels` to reposition to.
 * @param {function(): void} then - Called once the reposition response arrives.
 */
function repositionCursorTo(L, then) {
  const nodeId = chainLevels[L].id;
  requestPageTree(nodeId, (data) => {
    chainLevels[L].children = data.children;
    then();
  });
  bridge.browseTo(nodeId);
}

/**
 * Descend one level via `bridge.browseInto` and push it onto `chainLevels`
 * if the child has children of its own.
 *
 * @param {number} L - Index of the level whose child is being entered.
 * @param {string} childId - Id of the child node to descend into.
 * @param {function(object): void} [after] - Called with the response payload
 *   after the new level (if any) has already been pushed. Used by
 *   `restoreChainPath` to chain several `browseInto` calls in sequence
 *   without duplicating the "only push if it has children" logic.
 */
function fetchChildrenInto(L, childId, after) {
  requestPageTree(childId, (data) => {
    if (data.children.length > 0) {
      chainLevels.push({ id: data.node.id, title: data.node.title, children: data.children, selectedChildId: "" });
      renderChain();
    }
    if (after) after(data);
  });
  bridge.browseInto(childId);
}

/**
 * Handle a `<select>` change at level `L`: truncate the chain past `L`,
 * set the new selection, and either reposition the bridge cursor (if `L`
 * was not the deepest level) or descend directly (if it was) before
 * fetching the next level's children.
 *
 * @param {number} L - Index into `chainLevels` whose select changed.
 * @param {string} value - New selected child id, or "" for the placeholder
 *   ("— Root page —" / "— here —").
 */
function handleLevelChange(L, value) {
  const wasLast = L === chainLevels.length - 1;
  chainLevels = chainLevels.slice(0, L + 1);
  chainLevels[L].selectedChildId = value;
  renderChain();
  closeChainCreate();

  if (value === "") {
    setTarget(chainLevels[L].id);
    if (!wasLast) repositionCursorTo(L, () => {});
    return;
  }

  setTarget(value);
  if (wasLast) fetchChildrenInto(L, value);
  else repositionCursorTo(L, () => fetchChildrenInto(L, value));
}

/**
 * Apply an existing duplicate match as the selection at the deepest level
 * (used by the "Usar existente" button in the duplicate-title panel).
 *
 * @param {string} id - Id of the existing page to select.
 */
function selectExistingAtDeepestLevel(id) {
  handleLevelChange(chainLevels.length - 1, id);
}

/**
 * Handler for the bridge's `parentPageChanged` signal: the single place
 * where `currentParentPageId`/`currentParentPageTitle`/`currentParentPagePath`
 * are updated and the "Page: X" footer label is written, per the
 * single-source-of-truth contract documented at the top of this file.
 *
 * @param {string} payload - JSON string: `{ id, title, path, is_root }`.
 */
function renderParentPage(payload) {
  const data = JSON.parse(payload);
  currentParentPageId = data.id || "";
  currentParentPageTitle = data.is_root ? "root" : data.title;
  currentParentPagePath = data.path || [];
  $("parent-page-label").textContent = `Page: ${currentParentPageTitle}`;
}

/** Hide the "create page" input and the duplicate-title warning panel. */
function closeChainCreate() {
  $("chain-create").classList.add("hidden");
  $("chain-duplicate").classList.add("hidden");
  pendingChainCreateTitle = "";
}

/**
 * Arm `chainRequest` for the `pageTreeReady` that `bridge.createChildPage`
 * is about to trigger, BEFORE calling it.
 *
 * Success of `bridge.createChildPage` emits TWO signals: `pageTreeReady`
 * (the refreshed listing for the level being created into) and
 * `parentPageChanged` (the new target). Without arming `chainRequest`
 * before the call, `handlePageTree` would discard the first one as an
 * "orphan" response (fixed bug, ESTADO.md bug 14 / "C2") — the "Page: X"
 * label would still change (via `parentPageChanged`, which does not go
 * through this guard), but the deepest `<select>` would neither gain the
 * new option nor reflect the selection, visibly desyncing the two things
 * that are supposed to be born together (risk 11).
 */
function armDeepestLevelRefresh() {
  const L = chainLevels.length - 1;
  requestPageTree(null, (data) => {
    chainLevels[L].children = data.children;
    chainLevels[L].selectedChildId = data.selected_id || "";
  });
}

/**
 * Submit the "create page" input: ask the bridge whether the title
 * already exists among the current level's children and either show the
 * duplicate-resolution panel or create the page directly.
 *
 * Note: this file never normalizes or slug-compares titles itself — that
 * comparison happens exclusively on the Python side (`checkChildTitle` /
 * `title_slug` in `ui/bridge.py` and `notion/browser.py`). That is
 * deliberate, not an oversight: JS and Python Unicode normalization
 * (NFKD) can disagree on accented input in ways that only surface with
 * real accented titles (e.g. "Cálculo I" vs "CALCULO I" comparing equal in
 * one language and not the other). Keeping the single comparison in
 * Python avoids that class of bug entirely.
 */
function submitChainCreate() {
  if (!bridge || !chainLevels.length) return;
  const title = $("chain-create-title").value.trim();
  if (!title) return;
  bridge.checkChildTitle(title, (result) => {
    const { duplicate, matches } = JSON.parse(result);
    if (duplicate) {
      showChainDuplicate(title, matches);
    } else {
      armDeepestLevelRefresh();
      bridge.createChildPage(title);
      closeChainCreate();
    }
  });
}

/**
 * Show the duplicate-title resolution panel with one "use existing" button
 * per match plus a "create anyway" option.
 *
 * @param {string} title - Title the user tried to create.
 * @param {{id: string, title: string}[]} matches - Existing pages with a colliding slug.
 */
function showChainDuplicate(title, matches) {
  pendingChainCreateTitle = title;
  const plural = matches.length > 1 ? "pages named" : "a page named";
  $("chain-duplicate-msg").textContent = `There is already ${plural} "${title}" here.`;

  const useButtons = matches.map((m) =>
    `<button type="button" class="ghost chain-use-existing" data-id="${esc(m.id)}">Use "${esc(m.title)}"</button>`
  ).join("");
  $("chain-duplicate-actions").innerHTML =
    useButtons + '<button type="button" id="chain-create-anyway" class="ghost">Create another one anyway</button>';
  $("chain-duplicate").classList.remove("hidden");
}

/** Toggle the "create page" input open/closed for the deepest chain level. */
$("chain-add").onclick = () => {
  if (!bridge || !chainEnabled() || !chainLevels.length) return;
  const panel = $("chain-create");
  const opening = panel.classList.contains("hidden");
  closeChainCreate();
  if (opening) {
    panel.classList.remove("hidden");
    $("chain-create-title").value = "";
    $("chain-create-title").focus();
  }
};

/**
 * Revalidate the deepest chain level against the server: refresh its
 * children and, if the current selection no longer exists among them,
 * fall back to the placeholder instead of keeping a phantom value.
 */
$("chain-reload").onclick = () => {
  if (!bridge || !chainLevels.length) return;
  const L = chainLevels.length - 1;
  const nodeId = chainLevels[L].id;
  requestPageTree(nodeId, (data) => {
    chainLevels[L].children = data.children;
    const stillValid = chainLevels[L].selectedChildId &&
      data.children.some((c) => c.id === chainLevels[L].selectedChildId);
    if (chainLevels[L].selectedChildId && !stillValid) {
      chainLevels[L].selectedChildId = "";
      chainLevels = chainLevels.slice(0, L + 1);
      setTarget(nodeId);
    }
    renderChain();
  });
  bridge.reloadPageTree();
};

$("chain-create-cancel").onclick = closeChainCreate;
$("chain-create-btn").onclick = submitChainCreate;

/** Enter submits the "create page" input, Escape cancels it. */
$("chain-create-title").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    submitChainCreate();
  } else if (e.key === "Escape") {
    e.preventDefault();
    closeChainCreate();
  }
});

/** Delegate `change` events from any chain `<select>` to `handleLevelChange`. */
$("parent-chain").addEventListener("change", (e) => {
  const sel = e.target.closest("select.chain-select");
  if (!sel || !bridge) return;
  handleLevelChange(Number(sel.dataset.level), sel.value);
});

/**
 * Delegate clicks inside the duplicate-title panel: either select one of
 * the existing matches or proceed with creating a new page anyway.
 */
$("chain-duplicate-actions").addEventListener("click", (e) => {
  if (!bridge) return;
  const useBtn = e.target.closest(".chain-use-existing");
  if (useBtn) {
    selectExistingAtDeepestLevel(useBtn.dataset.id);
    closeChainCreate();
    return;
  }
  if (e.target.id === "chain-create-anyway") {
    armDeepestLevelRefresh();
    bridge.createChildPage(pendingChainCreateTitle);
    closeChainCreate();
  }
});


/**
 * Connect to the Qt WebChannel, wire every bridge signal to its handler,
 * and kick off the initial `ready()`/`loadHistory()` calls.
 *
 * @param {object} channel - QWebChannel instance; `channel.objects.bridge`
 *   is the Python `Bridge` (see `ui/bridge.py`).
 */
new QWebChannel(qt.webChannelTransport, (channel) => {
  bridge = channel.objects.bridge;

  bridge.providersReady.connect((payload) => {
    // No early exit here: the empty-providers case used to bail out right
    // after disabling the <select>, which also skipped disabling the
    // Format button and skipped the toast — the app looked idle instead of
    // broken. Folding "list is empty" into `usable === false` below keeps
    // `$("format").disabled` and the toast on the single path every case
    // goes through.
    const { providers, selected } = JSON.parse(payload);
    const sel = $("provider");
    sel.innerHTML = "";

    if (!providers.length) {
      sel.innerHTML = "<option>no provider</option>";
    } else {
      for (const p of providers) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.available ? p.label : `${p.label} — ${p.reason}`;
        opt.disabled = !p.available;
        if (p.id === selected) opt.selected = true;
        sel.appendChild(opt);
      }
    }
    sel.disabled = !providers.length;

    const usable = providers.some((p) => p.available);
    $("format").disabled = !usable;
    if (!usable) {
      toast("No provider available — set `GEMINI_API_KEY` in .env and reopen the app.", "err");
    }
  });

  bridge.draftReady.connect((json) => {
    showDraft(JSON.parse(json));
    toast("Draft ready. Review and publish.", "ok");
  });

  bridge.published.connect((json) => {
    const { title, url } = JSON.parse(json);
    toast(`Published: <strong>${esc(title)}</strong><br><a href="${esc(url)}">${esc(url)}</a>`, "ok", true);
  });

  /**
   * The restored destination only arrives after `ready()`, so the first
   * preview (if a draft is already on screen) must be re-rendered with the
   * real value instead of the "database" default.
   */
  bridge.destinationsReady.connect((payload) => {
    renderDestinations(payload);
    refreshPreview();
  });
  bridge.historyReady.connect(renderHistory);
  bridge.failed.connect(handleBridgeFailed);
  bridge.busy.connect(setBusy);
  bridge.configWarning.connect((msg) => {
    const el = $("config-warning");
    el.textContent = msg;
    el.classList.remove("hidden");
  });
  bridge.pageTreeReady.connect(handlePageTree);
  bridge.parentPageChanged.connect(renderParentPage);

  $("provider").onchange = (e) => bridge.selectProvider(e.target.value);
  $("destination").onchange = (e) => {
    currentDestination = e.target.value;
    bridge.selectDestination(currentDestination);
    updateParentChainAvailability();
    refreshPreview();
  };
  bridge.ready();
  /**
   * Load history already at boot, so the pending badge appears without
   * needing to open the drawer.
   */
  bridge.loadHistory();
});
