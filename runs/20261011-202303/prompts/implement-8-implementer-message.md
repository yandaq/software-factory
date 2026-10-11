Implement this plan in the repository.

# Plan

# Plan: Editable, persistent, visually striking Hello World web app

## Outcome

A zero-dependency Node.js web app. Serving `GET /` shows a large, animated, full-viewport **hello world** that a visitor can click and edit in place. Every edit is saved to the server (debounced) into a JSON file on disk, so the text is still there after a page reload, a browser restart, an incognito window, or a server restart.

## Fixed design decisions (do not re-litigate during implementation)

1. **Server-side persistence to a file, not `localStorage`.** `localStorage` would satisfy 'persist across sessions' only per-browser and is invisible to automated verification. A file store gives an observable artifact (`data/hello.json`), survives server restarts and works across browsers/devices. See Risks R1 for the rejected alternative.
2. **Zero runtime and zero dev dependencies.** Node 26.8.1 is installed (mise) and provides `node:http`, `node:fs` and the built-in test runner `node --test`. Playwright 1.63.0 + Chromium 1228 are already installed globally (`~/.local/share/mise/installs/npm-playwright/1.63.0`, browsers in `~/.cache/ms-playwright`); `VALIDATION.md` explicitly forbids installing browser packages that already exist. Do not add React/Vite/Express/Tailwind/Prettier/ESLint.
3. **Editing via `contenteditable="plaintext-only"` on an `<h1>`.** Read and write **only `textContent`**, never `innerHTML` — this is both the XSS boundary and what keeps stored data plain text. Browsers without `plaintext-only` fall back to `true`; since we always save/read `textContent`, pasted markup is flattened on save anyway.
4. **Single user, last-write-wins.** No auth, no sessions, no CRDT, no optimistic-concurrency ETags. Documented as an explicit assumption (R3).
5. **Default text is exactly `hello world`** (lowercase, one space).

## File layout to create

```
package.json          # type=module, scripts: start | test | check ; no dependencies
main.js               # entry point: wires store + server, reads PORT/DATA_FILE env, listens
store.js              # file-backed text store: createFileStore(path) -> {read, write}
server.js             # createServer(store) -> http.Server ; routing + JSON API ; no listen()
public/index.html     # markup: h1#text contenteditable, status pill, layer elements
public/app.js         # load text, edit, debounced save, status indicator
public/styles.css     # the visual design (see Visual spec)
tests/store.test.js   # unit tests for the file store
tests/api.test.js     # HTTP integration tests via createServer + fetch on port 0
.gitignore            # node_modules/, data/
README.md             # how to run, how to test, data location, limitations
docs/architecture.md  # fill in the template: modules, contracts, dependency rule, compromise
data/hello.json       # runtime-created, gitignored
```

Do **not** touch `init.txt` (pre-existing, empty) or the other `docs/` files.

---

## Steps

### 1. Scaffold → outcome: `npm test` and `npm run check` run cleanly on an empty suite

**Files:** `package.json`, `.gitignore`, `README.md`

- `package.json`: `{ "name": "hello-world", "private": true, "type": "module", "scripts": { "start": "node main.js", "test": "node --test tests/", "check": "node --check store.js && node --check server.js && node --check main.js && node --check public/app.js" } }`. No `dependencies` / `devDependencies` keys at all.
- `.gitignore`: `node_modules/` and `data/`.
- `README.md`: title + placeholder Run/Test sections (completed in step 7).

**Test:** `npm run check` exits 0 (may be a no-op until files exist — create them in later steps and re-run); `npm test` exits 0 with 0 tests. Commit scaffold.

---

### 2. Persistence layer → outcome: text round-trips to disk and survives re-instantiation

**Files:** `store.js`, `tests/store.test.js`  ·  **Write the tests first and watch them fail for the expected reason (missing module / missing export) before implementing.**

Contract:

```js
export const DEFAULT_TEXT = 'hello world'
export function createFileStore(filePath)   // -> { read(): string, write(text: string): void }
```

- `read()` — return the file contents as utf-8 string; if the file does not exist (`ENOENT`) return `DEFAULT_TEXT`. Any other `fs` error propagates (do **not** swallow it — AGENTS §8, coding-standards).
- `write(text)` — `mkdirSync(dirname(filePath), { recursive: true })`, write to `filePath + '.tmp'`, then `renameSync` onto `filePath`. The temp+rename is deliberate: it prevents a truncated/corrupt `hello.json` if the process dies mid-write. Only a string is accepted; throw `TypeError` otherwise.
- The store owns the path; nothing else in the codebase touches the filesystem.

**Tests (`tests/store.test.js`, `node:test` + `node:assert`, each test uses `mkdtempSync(join(tmpdir(),'hello-'))` and cleans up):**
1. `read()` on a fresh empty dir returns `'hello world'`.
2. `write('bonjour')` then a **new** `createFileStore(samePath)` reads `'bonjour'` (proves on-disk persistence, not memory).
3. Unicode + newline round-trip: `'héllo 🌍\nline2'` survives write→read byte-for-byte.
4. `write()` creates missing parent directories (`data/nested/x.json`).
5. `read()` propagates a real IO error: point the store at a path whose parent is an existing *file*, assert `read()` throws (guards against silent error swallowing).
6. `write(42)` throws `TypeError`.

**Test command:** `node --test tests/store.test.js` (all pass).

---

### 3. HTTP server + JSON API → outcome: `curl` can read and overwrite the greeting

**Files:** `server.js`, `main.js`, `tests/api.test.js`, plus a one-line placeholder `public/index.html` (replaced in step 4)

**Public contract (implement exactly):**

| Method | Path | Response |
|---|---|---|
| GET | `/` | 200 `public/index.html` — `text/html; charset=utf-8` |
| GET | `/styles.css` | 200 — `text/css; charset=utf-8` |
| GET | `/app.js` | 200 — `text/javascript; charset=utf-8` |
| GET | `/api/text` | 200 `{"text":"<current>"}` |
| PUT | `/api/text` | body `{"text":"..."}` → 200 `{"text":"<stored>"}` |
| any | other | 404 `{"error":"not found"}` |

Error semantics: PUT with unparseable JSON or a non-string `text` → `400 {"error":"text must be a string"}`. Body larger than `MAX_TEXT_BYTES = 10000` → `413 {"error":"text too long"}` (stop accumulating and consume the rest). Method other than GET/PUT on any path → `405` with an `Allow` header. An unexpected throw from the store → `500 {"error":"internal"}` **and** `console.error` including method, path and the error (never silent).

Structure: `server.js` exports `createServer(store)` returning an `http.Server` and never calls `listen` (keeps it testable). Static files are read once from an explicit asset map (`'/'`, `'/styles.css'`, `'/app.js'` → file + content-type) rather than a generic path-resolving static handler — smaller code and no path-traversal surface; note this trade-off in `docs/architecture.md`. `main.js` reads `PORT` (default 3000) and `DATA_FILE` (default `./data/hello.json` resolved from `import.meta.url`), builds the store, listens and logs one startup line: `Editing at http://localhost:3000  (data: /abs/path/data/hello.json)`.

**Tests (`tests/api.test.js`)** — build `createServer(createFileStore(join(tmpdir…,'hello.json')))` and `listen(0)` in `before`, close in `after`, use global `fetch`:
1. `GET /` is 200 and `content-type` starts with `text/html`.
2. `GET /api/text` on a fresh store → `{"text":"hello world"}`.
3. `PUT /api/text {"text":"hi there"}` → 200, and a following `GET` returns `"hi there"`.
4. **Restart proof:** close the server, create a *second* server over the same file, `GET` still returns `"hi there"`.
5. `PUT` body `{"text":123}` → 400 with an `error` field.
6. `PUT` body `not json` → 400.
7. `PUT` text of 10001 chars → 413.
8. `DELETE /api/text` → 405 with `allow` header.
9. `GET /nope` → 404.

**Test command:** `node --test tests/api.test.js`, then `node --test tests/`. Manual smoke: `PORT=3100 node main.js` + `curl -s localhost:3100/api/text` and `curl -s -X PUT -d '{"text":"x"}' localhost:3100/api/text`; then `cat data/hello.json`.

---

### 4. Editable client → outcome: typing in the headline persists after a reload

**Files:** `public/index.html`, `public/app.js` (delete the step-3 placeholder markup)

Markup requirements: `<!doctype html>`, `<html lang="en">`, `<meta charset="utf-8">`, `<meta name="viewport" content="width=device-width, initial-scale=1">`, `<title>hello world</title>`, one `<main>`, an `<h1 id="text" contenteditable="plaintext-only" spellcheck="false" role="textbox" aria-multiline="false" aria-label="Greeting text">hello world</h1>`, a `<p id="status" role="status" aria-live="polite">` save indicator, and `<noscript>` stating editing needs JavaScript.

Behaviour (`public/app.js`, plain ES module, no framework):
1. On load: `const el = document.querySelector('#text')`; `el.textContent = (await fetch('/api/text')).text` — **`textContent` only**. If the fetch fails, show the failure in the status element and keep the default text visible.
2. Focus/select-all on first focus is not required; do not auto-focus.
3. `el.addEventListener('input', …)` → update the status to `Saving…` and (re)start a **400 ms debounce**; on fire, `PUT /api/text` with `JSON.stringify({ text: el.textContent })`.
4. Flush pending changes on `blur` and on `pagehide` using `fetch(..., { keepalive: true })` so the last ≤400 ms of typing is not lost when the tab closes.
5. On a successful save, set status to `Saved HH:MM` (24h, local) and add a `.saved` class for ~1.2 s to drive the CSS pulse. On failure set `Not saved — will retry`, log the error to `console.error`, and let the next `input` event retry (do not build a background retry queue).
6. Pressing **Enter** is `preventDefault`ed — the greeting is single-line, this prevents the layout breaking and avoids stray `<div>`/`<br>` nodes in a contenteditable.
7. The client never writes `innerHTML` and never renders stored data as HTML.

**Test:** this behaviour is proven by the browser E2E in step 6 (a headless Playwright script), because it is DOM-level; there is no DOM in the Node test runner and we are not adding jsdom. In this step verify manually with `node main.js` in a real browser plus `curl` + `cat data/hello.json`, and confirm the browser console is clean.

---

### 5. Visual design → outcome: a screenshot that would pass as a polished landing hero

**Files:** `public/styles.css` (new), `public/index.html` (add the decorative layer elements)

Concrete, checkable spec (each item becomes an acceptance criterion or a screenshot check):

- **Canvas:** `html,body{height:100%}`, `body{min-height:100svh; display:grid; place-items:center; overflow:hidden; margin:0}`, `color-scheme: dark`, palette in CSS custom properties. Text container `width:min(1100px, 92vw)`, `text-align:center`, `padding: clamp(1rem, 6vw, 4rem)`.
- **Layered background** (three stacked layers, all `position:fixed; inset:0; pointer-events:none; z-index:-1`):
  1. base `radial-gradient` at 50% 20% `#1b1f4b` → `#0a0a14` over a `linear-gradient` to `#05060d`;
  2. three blurred aurora blobs (`.blob` divs, `border-radius:50%`, `filter: blur(90px)`, `opacity:.5`, `mix-blend-mode:screen`, hues violet `#6d5efc`, cyan `#22d3ee`, magenta `#f472b6`), each with a slow `@keyframes drift` (24 s / 33 s / 41 s, `alternate`, translate+scale) and `will-change:transform`;
  3. film grain: an inline SVG `feTurbulence` **data-URI** as `background-image` on a pseudo-element at `opacity:.05`, plus a vignette `radial-gradient(transparent 45%, rgba(0,0,0,.55))`.
  No external images, fonts or CDNs.
- **Typography:** `font-family: ui-rounded, system-ui, -apple-system, "Segoe UI", Inter, sans-serif`; `#text{font-size: clamp(3rem, 12vw, 11rem); font-weight:800; letter-spacing:-.04em; line-height:.95}`; `white-space: pre-wrap; overflow-wrap: break-word` so a long edit never overflows.
- **Text treatment:** `background: linear-gradient(100deg,#ffffff,#b7c3ff 35%,#7dd3fc 70%,#f0abfc)` + `background-size:220%` + `background-clip:text` + `-webkit-text-fill-color:transparent`, with a 12 s `background-position` animation; add `filter: drop-shadow(0 0 40px rgba(125,211,252,.35))` for glow and **`caret-color:#fff`** so the caret stays visible against the transparent fill. On `#text:focus` add a subtle underline/ring so the editable affordance is discoverable.
- **Focus visible:** `#text:focus-visible{ outline:2px solid #7dd3fc; outline-offset:.2em; border-radius:.1em }` (WCAG 2.4.7 — this is a required check, not a nice-to-have).
- **Status pill:** fixed `bottom: clamp(1rem,5vh,3rem)`, centred, small uppercase `letter-spacing` label with a coloured dot per state (`.saving`, `.saved`, `.error`), `transition: opacity .3s`, low-opacity by default and higher on change.
- **Micro-interaction:** `@keyframes pulse` on `.saved` applied to the pill (and a brief brightness bump on the headline). CSS-only.
- **Responsive:** verify at 1440×900 and 390×844 — below 640 px reduce blur to 50 px and confirm **zero horizontal scroll**.
- **Reduced motion:** `@media (prefers-reduced-motion: reduce){ *,*::before,*::after{ animation:none !important; transition:none !important } }`.
- Contrast: all foreground text (status pill, headline) must meet 4.5:1 against its backdrop.
- **Out of scope (state explicitly):** a light theme, user-selectable colours/fonts, animations triggered on hover.

**Test:** no unit test. Evidence = screenshots captured in step 6 at 1440×900 and 390×844, plus the reduced-motion screenshot; a human reviews them against the checklist above (this is the one criterion that needs human eyes — flag it as such).

---

### 6. End-to-end validation → outcome: the full journey is proven by an automated browser run with zero console errors

**Files:** `/tmp/hello-e2e.mjs` (**throwaway — do not commit**, per `VALIDATION.md`: no committed Playwright config/deps/tests).

Playwright is already installed globally. Import it from the absolute path `~/.local/share/mise/installs/npm-playwright/1.63.0/node_modules/playwright/index.js` (Chromium 1228 is already in `~/.cache/ms-playwright`). If that import path fails, `playwright screenshot` from the mise shim is the fallback for screenshots only, and the persistence journey must then be proven with `curl` + `cat data/hello.json` with the blocker disclosed.

Start the app (`PORT=3100 node main.js`, backgrounded, capture stdout/stderr), then assert:

1. `goto` page → `#text` textContent equals `hello world` (use a fresh `DATA_FILE` env pointing at a temp file so the run is reproducible).
2. `page.on('console')` and `page.on('pageerror')` are collected for the whole run; **any `error`/`warning` or page error fails the run** (record it as evidence per VALIDATION §2).
3. Click `#text`, `page.keyboard.press('Control+A')`, type `hey you`, wait for the status pill to read `Saved` (≤2 s).
4. Assert `data/hello.json` (or the temp data file) on disk literally contains `hey you` — an artifact check independent of the UI.
5. `page.reload()` → `#text` reads `hey you`.
6. **Server restart proof:** close the server process, re-launch, `goto` again → still `hey you`.
7. New browser **context** (fresh profile, same server) → still `hey you` (proves cross-session, cross-profile persistence).
8. Keyboard-only path: from `about:blank`, `Tab` reaches `#text` and typing changes it.
9. Screenshots: `desktop-1440.png` (idle), `desktop-focus.png` (while editing), `mobile-390.png`, `desktop-reduced-motion.png` (with `reducedMotion: 'reduce'`); assert `document.documentElement.scrollWidth <= clientWidth` at both viewports.
10. Reduced-motion run: assert computed `animation-name` on a `.blob` is `none`.

**Commands:** `node --test tests/` (unit+API) · `node /tmp/hello-e2e.mjs` (E2E) · `npm run check`.

**Evidence to report** in the VALIDATION.md Completion Evidence block: each check PASS/FAIL with output, browser console output (must be empty), file contents of the data file, screenshot paths, and an explicit criterion-by-criterion acceptance table.

---

### 7. Documentation → outcome: a newcomer can run and test the app from the README alone

**Files:** `README.md`, `docs/architecture.md`

- `docs/architecture.md` (fill the template): module table (`main` → composition root, `server` → HTTP routing/API, `store` → the only filesystem owner, `public/*` → client), the dependency rule (`main → server → store`; `public/*` depends only on the HTTP contract; no back-edges, no cycles), the API contract table, and **two recorded compromises**: (a) explicit static asset map instead of a generic static handler — less code, no traversal risk, cost = one line per new asset; (b) last-write-wins single-user file store — trivially simple, cost = concurrent editors silently overwrite each other.
- `README.md`: what it is, `npm start` (→ http://localhost:3000), `npm test`, `npm run check`, where data lives (`data/hello.json`, `DATA_FILE`/`PORT` overrides), and the limitation list (single user, no auth, one line).

**Test:** every command in the README is copy-pasteable and actually works; `docs/architecture.md` matches the code as written.

---

## Acceptance criteria

| # | Criterion | Evidence |
|---|---|---|
| A1 | `npm start` serves the app; `GET /` returns 200 HTML containing a `contenteditable` element | api test 1, manual curl, E2E step 1 |
| A2 | A user can click the headline and edit it with the keyboard, seeing the change immediately | E2E steps 3 + 8, `desktop-focus.png` |
| A3 | ≤1 s after the last keystroke the new text is on disk in the data file | E2E step 4 (`cat` the file) |
| A4 | Text survives page reload, browser/server restart, and a fresh browser profile | E2E steps 5–7, api test 4 |
| A5 | Visual spec items in step 5 all present (layered background + blobs + grain + vignette, gradient/glow `clamp()` type ≥ 12vw, status pill, visible focus ring, no horizontal scroll at 390 px) | 4 screenshots + human review |
| A6 | Reduced-motion disables all animations | E2E step 10 |
| A7 | Zero unexpected browser console/page errors during E2E | E2E step 2 log |
| A8 | `node --test tests/` and `npm run check` pass | command output |
| A9 | Stored content is rendered as text only (`textContent`); an edit containing `<img src=x onerror=alert(1)>` is displayed literally, not executed | add an E2E step 11 typing that string and asserting it appears as literal text with no `dialog` event |

## Assumptions

- **A1** "Persist across sessions" = server-side, shared by every browser/device and surviving server restarts (not per-browser `localStorage`). Rejected alternative: a static page with `localStorage` only — fewer moving parts, but invisible to automated verification and lost when the profile is cleared. If the requester actually wanted per-browser persistence with no backend, step 2/3 shrink to a single static file served by any static server; flag before building.
- **A2** Node 26 (`node:http`, `node --test`) and the pre-installed Playwright 1.63 are the toolchain; nothing is installed. Python 3.14 exists but is unused.
- **A3** Data file lives at `./data/hello.json` inside the repo (gitignored) for discoverability; overridable via `DATA_FILE`.
- **A4** Single line of text, max 10 KB; Enter is suppressed.
- **A5** Dark theme only; no i18n, no build step, no CI in scope. `init.txt` is left alone.

## Risks / open questions

- **R1 Persistence semantics ambiguity** (A1 above) — cheapest thing to get wrong; the server-side choice is the safer superset but is more machinery than a pure client app.
- **R2 "Visually stunning" is subjective.** Mitigated by the itemised visual spec in step 5 plus screenshots, but final judgement needs a human glance. Screenshot review is the weakest link in the evidence chain — disclose it rather than claim it as an objective pass.
- **R3 Concurrency:** last-write-wins. Two open tabs silently overwrite each other. Accepted for a single-user demo; recorded in `docs/architecture.md`.
- **R4 Playwright reachability:** importing from the mise global path is unusual; if it fails, E2E degrades to `curl` + file inspection + `playwright screenshot`, and DOM-level assertions (A2, A9) must be reported as *Not run* with the exact blocker, per VALIDATION "Blocked Validation".
- **R5 No linter/formatter is configured in this repo** and none is added (no new dependencies). `node --check` is the only static gate; coding-standards' "standard formatter" requirement is unmet — surface this to the requester rather than silently adopting Prettier.
- **R6 `contenteditable` quirks** (paste of rich text, IME composition, stray `<br>`). Mitigated by `plaintext-only` + `textContent` + suppressing Enter; IME composition is not exhaustively testable here — worth a manual check.
- **R7 `-webkit-text-fill-color: transparent`** can hide the caret and is fragile in older engines; `caret-color` is set and a screenshot with focus proves caret visibility.

## Definition of done

All steps 1–7 merged; `node --test tests/` green; `npm run check` green; Playwright E2E script passes with an empty console log; four screenshots reviewed; README + `docs/architecture.md` accurate; the Completion Evidence block from `docs/VALIDATION.md` produced, with anything not run explicitly disclosed.


# Latest review findings

# Review 4

**Verdict:** changes_requested

## Findings

- **[major]** (public/app.js:57-80) Input handlers are enabled before the initial GET completes, so a delayed response can overwrite an edit made during loading and the pending debounce will then persist the old server value. This violates plan acceptance criteria A2/A3; prevent stale load results from replacing user input and cover the race with a browser test.
- **[minor]** (public/app.js:67-69) The blur flush calls save() without keepalive:true, violating Plan step 4's explicit requirement that both blur and pagehide flushes use keepalive.
- **[minor]** (docs/architecture.md:3) The architecture claims store.js is the only filesystem owner, while server.js uses readFileSync for static assets. Document the narrower invariant that store.js exclusively owns persistent greeting-file access.


When finished, end your final message with a single JSON object that validates against this JSON Schema:
```json
{
  "type": "object",
  "required": [
    "summary"
  ],
  "properties": {
    "summary": {
      "type": "string",
      "description": "What was changed and why"
    }
  }
}
```