# Frontend Architecture & Development

AstinusLab's UI is server-rendered and progressively enhanced. There is no
single-page app and no client-side router — pages are Jinja2 templates, HTMX
handles in-place updates, and page-specific behavior lives in small vanilla
ES modules.

## Stack

*   **Templates**: Jinja2, served by FastAPI.
*   **Interactivity**: [HTMX](https://htmx.org/) for partial updates and form posts.
*   **Scripting**: Vanilla ES modules (`<script type="module">`). No framework, no bundler, no build step — files under `src/astinuslab/web/static/` are served as-is.
*   **Styling**: Plain CSS under `src/astinuslab/web/static/css/`.

### Why no framework

The Content-Security-Policy set in `src/astinuslab/main.py` uses:

```
script-src 'self' 'nonce-{nonce}'
```

There is no `'unsafe-eval'` and no `'unsafe-inline'` for scripts. Frameworks
that evaluate expression strings at runtime (Alpine.js, and similar) need
`'unsafe-eval'`, so they cannot run under this policy. This is a deliberate
security constraint, not an oversight: keep client logic in real ES modules,
and give any inline `<script>` the request nonce.

## HTMX conventions

*   **Global error handling** lives in `src/astinuslab/web/static/toast.js`. It listens on
    `document.body` for `htmx:responseError` (non-2xx) and `htmx:sendError`
    (network failure) and surfaces a toast via `window.showToast`. Individual
    handlers do not need their own error toasts.
*   **Styled confirmations** live in `src/astinuslab/web/static/confirm-dialog.js`. It
    intercepts HTMX's `htmx:confirm` event and also handles plain
    `data-confirm="…"` on forms and buttons, replacing the browser's native
    `confirm()` with an accessible modal. Add `data-confirm` rather than wiring
    your own dialog.
*   **Loading states** pair `hx-indicator` (what to show while a request is in
    flight) with `hx-disabled-elt` (what to disable so it can't be
    double-submitted).
*   **Polling fragments** (e.g. status that refreshes on an interval)
    intentionally omit `hx-indicator` — a spinner that blinks every few seconds
    is noise, not feedback.

## Store / module pattern

Larger pages split their JavaScript into a small set of modules rather than one
file. `src/astinuslab/web/static/playground/` is the reference implementation:

*   `state.js` holds a **single state object** plus `getState()` and
    `setState(updater)`. `setState` accepts either a partial object or an
    updater function.
*   Render modules **subscribe** with a selector: `subscribe(selector, fn)`.
    After each `setState`, a subscriber's `fn` runs only when its selected slice
    changes (a shallow-equality check gates re-renders), so unrelated updates
    don't repaint the whole page.
*   `api.js` wraps all `fetch` calls so errors surface uniformly and modules
    consume one stable DTO shape.

**The gate only closes if the data holds still.** The check is
`Object.is` per selected key, so a poll that re-parses JSON hands every
subscriber brand-new arrays and objects and re-renders everything — which is
exactly what used to happen every five seconds on an idle Playground. Anything
writing a polled payload into the store passes it through
`playground/session-identity.js`'s `reconcile(previous, next)` first: it returns
the previous reference wherever the content is unchanged, so only what actually
moved propagates. Two rules follow from that:

*   Do not park data on a state object that nobody renders. A metrics sample
    carrying `sampled_at` and byte counters was being copied onto every running
    VM, and those fields alone changed the VM's identity on every tick.
*   An updater that finds nothing new should `return prev` unchanged rather than
    spread a fresh object; `setState` then notifies no one.

`src/astinuslab/web/static/volatility/` follows the same module layout — `state.js`,
`api.js`, and feature modules (`catalog`, `run`, `results`, `queue`, `isf`)
wired together by `index.js`. Its `state.js` is a simpler shared mutable object
(render is called imperatively rather than through `subscribe`); the split into
state / api / feature modules is the pattern to copy.

## Rule of thumb

New page-level JavaScript starts as **one module**. Split it into
`state` / `api` / `render-*` (or feature) modules once it grows past roughly
**300 lines** — that is when a single file starts hiding its own structure.
