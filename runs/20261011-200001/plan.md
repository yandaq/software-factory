# Plan: Editable, persistent, visually striking `hello world` web app

## 1. Outcome

A single-page app that:
1. renders `hello world` as large display type on an animated dark canvas;
2. lets the user click it and edit it inline (caret, keyboard, paste);
3. saves every edit so the text survives reload, tab close and browser restart (same browser + profile);
4. is provable without a human eye: Node unit tests for persistence + a browser e2e run that edits, reloads and re-reads, with zero console errors.

No build step, no package install, no remote fonts or CDN.

## 2. Interpretation (AGENTS 1 and 2)

- **Editable** = one inline plain-text field. No rich text, no collaboration, no auth, no undo/reset UI (browser Ctrl+Z inside the field still works).
- **Persist across sessions** = survives reload, tab close and browser restart in the same browser + profile, via `localStorage` key `hello-world.text`. It does NOT sync across browsers or devices — see risk R1.
- Design is fixed dark (no light-theme variant), stated deliberately rather than by accident.
- Plain text is stored verbatim (no JSON encoding), so an empty string is distinguishable from `never set`.

## 3. Files (all new)

| Path | Purpose |
| --- | --- |
| `index.html` | markup: aurora layers, glass card, `#text` editable region, `#status` live region; loads `src/main.js` as a module |
| `styles.css` | all visual design; no styling that depends on JS |
| `src/text-store.js` | the only persistence boundary: `TEXT_KEY`, `DEFAULT_TEXT`, `loadText(storage)`, `saveText(storage, text)` |
| `src/main.js` | DOM wiring only: load on start, debounced save, plain-text paste, status indicator |
| `tests/text-store.test.js` | `node:test` + `node:assert` tests over an in-memory storage fake |
| `package.json` | 4 lines: `{"private":true,"type":"module","scripts":{"test":"node --test tests/"}}` — makes `.js` count as ESM for `node --test` / `node --check`. No dependencies, no `npm install` |
| `validation.md` | fill the currently empty `sh` block (step 8) |
| `agents.md` | add run/test commands |
| `architecture.md` | module map + recorded compromises |

`AGENTS.md`, `VALIDATION.md`, `coding-standards.md` and `docs/` are left untouched.

## 4. Contracts

`src/text-store.js` — pure, storage object injected (AGENTS 6: isolate external dependencies):
- `export const TEXT_KEY = 'hello-world.text'`
- `export const DEFAULT_TEXT = 'hello world'`
- `loadText(storage)` returns the stored string when present; `DEFAULT_TEXT` when the key is unset or when `getItem` throws.
- `saveText(storage, text)` calls `setItem`, returns `true`; on throw it does `console.error('[text-store] save failed', error)` and returns `false` (coding-standards: errors surfaced, never swallowed).

`src/main.js` — thin wiring, covered by e2e rather than mocks:
1. `el.innerText = loadText(localStorage)` at module evaluation (`type=module` is deferred, so the DOM is ready).
2. `input` → reset a 300 ms debounce → status `Saving…` → on fire, `saveText` then status `Saved` / `Couldn't save`.
3. `blur` → flush a pending save immediately.
4. `paste` → `preventDefault()` then insert `e.clipboardData.getData('text/plain')` through the active `Selection`/`Range` (`deleteContents` + `insertNode(createTextNode(...))`). No deprecated `document.execCommand`.
5. `#status` is `role=status aria-live=polite`, so save state is both visible and announced (AGENTS 8: observable state).
6. Read with `el.innerText` after stripping trailing newlines (see limitation R2).

## 5. Steps

**Step 0 — Environment probe (no files).** Record `command -v node python3 curl` and `node --version` (need >= 18 for `node:test`). Do not install anything (AGENTS 9). Node missing → drop steps 2/7 and declare unit tests blocked per VALIDATION `Blocked Validation`. python3 missing → substitute whichever static server already exists.
→ Outcome: known toolchain, zero installs.

**Step 1 — Write the failing unit tests.** `tests/text-store.test.js`, in-memory fake `{getItem, setItem}` over a `Map`, one `test()` per row:
| # | Assertion |
| 1 | empty storage → `loadText()` === `DEFAULT_TEXT` |
| 2 | stored value returned verbatim, incl. unicode/emoji/newlines |
| 3 | `saveText` returns `true` and writes under raw key `hello-world.text` (assert the map key, not the getter) |
| 4 | `saveText(s,'')` then `loadText(s)` === `''` — empty must NOT fall back to the default |
| 5 | `setItem` throws → `saveText` returns `false` and does not throw |
| 6 | `getItem` throws → `loadText` returns `DEFAULT_TEXT` and does not throw |

Run `node --test tests/` first and confirm it fails because the module does not exist (VALIDATION 2: confirm the failure reason before trusting the test).
→ Outcome: a committed, deterministic spec for persistence; 6 failing tests.

**Step 2 — Implement `src/text-store.js`** exactly to 4 (two small functions, no class, no extra config).
→ Outcome: `node --test tests/` reports 6 pass / 0 fail, exit 0.

**Step 3 — `index.html`.** `<!doctype html>`, `lang=en`, viewport meta, `<meta name=\"color-scheme\" content=\"dark\">`, `<title>hello world</title>`; three `.aurora` layer divs; `<main class=card>` holding `<div id=text class=text contenteditable=true role=textbox aria-multiline=true aria-label=Greeting text spellcheck=false autocapitalize=off autocorrect=off data-placeholder=hello world>` and `<p id=status class=status role=status aria-live=polite>`; `<link rel=stylesheet href=./styles.css>` + `<script type=module src=./src/main.js></script>` (attributes quoted normally in HTML).
→ Outcome: accessible markup with a named editable node; no inline styles or inline scripts.

**Step 4 — `src/main.js`** per 4. If `saveText` returns false the status reads `Couldn't save — changes are not stored`, and the step-2 `console.error` supplies the cause.
→ Outcome: an edit survives a reload; the save state is visible without opening devtools.

**Step 5 — `styles.css` (the visually stunning step).** Fully specified so no design decision is left open:
- `:root` vars: ink `#f4f6ff`, accents `#7cc4ff`, `#b78cff`, `#5ce1c0`, base `#05060a`.
- Canvas: `body{min-height:100vh; min-height:100svh; display:grid; place-items:center; overflow:hidden; background:var(--base)}`. Three `.aurora` blobs, absolutely positioned ~60vmax `border-radius:50%` circles, one accent hue each, `filter:blur(90px)`, `opacity:.5`, `mix-blend-mode:screen`, each running `@keyframes drift` (translate + scale) at 26 s / 34 s / 42 s `ease-in-out infinite alternate` so the field never visibly loops.
- Grain: `body::after` using an inline `data:image/svg+xml` `feTurbulence` turbulence, `opacity:.035`, `mix-blend-mode:overlay`, `pointer-events:none` — removes gradient banding.
- Card: `width:min(90vw,760px)`, `padding:clamp(28px,5vw,56px)`, `border-radius:28px`, `background:rgba(255,255,255,.06)`, `backdrop-filter:blur(28px) saturate(140%)` **plus the `-webkit-` prefix**, `border:1px solid rgba(255,255,255,.12)`, `box-shadow:0 30px 80px -20px rgba(0,0,0,.75)`; `::before` inset -1px with `border-radius:inherit`, a `conic-gradient` of the three accents masked to a 1 px ring, rotated slowly for a living edge; `:hover` intensifies the glow over a 250 ms ease transition.
- Editable text: `font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif`; `font-size:clamp(2.5rem,9vw,7rem)`; `font-weight:700`; `letter-spacing:-.04em`; `line-height:1.05`; `color:var(--ink)`; `text-shadow:0 0 40px rgba(124,196,255,.35)`; `white-space:pre-wrap; word-break:break-word`; `outline:none`; `caret-color:#7cc4ff`; `min-height:1em` so an empty value still shows a caret; `:focus-visible{box-shadow:0 0 0 2px rgba(124,196,255,.5); border-radius:8px}`; `::selection{background:rgba(124,196,255,.35)}`; `:empty::before{content:attr(data-placeholder); opacity:.35}` so a deliberately emptied value shows a ghost rather than a collapsed box.
  **Hard constraint:** do NOT gradient-clip the editable text (`background-clip:text` + `-webkit-text-fill-color:transparent` makes caret and selection unreliable). The gradient lives in the background and card edge; the text stays luminous near-white with a glow. Recorded as a compromise in `architecture.md`.
- Status pill: small uppercase, `letter-spacing:.14em`, 200 ms colour/opacity transition; `data-state=saving` amber, `saved` teal at 55 % opacity, `error` red.
- Responsive: at `max-width:480px` shrink card padding only; text must stay horizontally contained.
- `@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}`.
→ Outcome: aurora + glass + glowing display type, correct at 375 px and 1440 px, still legible and motionless under reduced motion.

**Step 6 — Docs.** `architecture.md`: module table, allowed dependency direction (`main.js → text-store.js → injected storage`, nothing else), compromise 1 = client-side `localStorage` (no server, offline-capable, zero deps; no cross-device sync), compromise 2 = gradients on background/card edge rather than on the editable text, plus limitation R2. `agents.md`: Run `python3 -m http.server 8123 --directory .` then open `http://127.0.0.1:8123/`; Test `node --test tests/`; note that there is no build, no dependencies, and that the app must be served over HTTP because Chromium blocks ES modules loaded from `file://`.
→ Outcome: an agent can run and test the app unaided.

**Step 7 — Static syntax gate.** `node --check src/main.js`, exit 0. This is the only cheap check covering `main.js`, which deliberately has no unit tests.

**Step 8 — `validation.md`.** Replace the commented placeholder inside the `sh` block with:
```sh
node --test tests/
node --check src/main.js
node --check src/text-store.js
```
(no `npm install` — there is nothing to install)
→ Outcome: `sh -e` from the repo root exits 0, and the factory now runs the tests after every loop.

**Step 9 — End-to-end browser validation.** Per VALIDATION 5, use Pi's built-in Playwright-compatible browser tools; do **not** commit Playwright config, dependencies or test files. Start `python3 -m http.server 8123 --directory .` in the background, attach console/page-error listeners, then:
| # | Action | Evidence |
| A1 | request `/`, `/styles.css`, `/src/main.js` | HTTP 200, JS served with a `text/javascript` mime |
| A2 | navigate `http://127.0.0.1:8123/` | snapshot shows `hello world` |
| A3 | evaluate `localStorage.getItem('hello-world.text')` | `null` before any edit — the default is a fallback, not a pre-seeded value |
| A4 | click `#text`, select-all, type `hello pi ✨`, wait >= 400 ms | status shows `Saved`; `getItem` returns `hello pi ✨` |
| A5 | reload | `#text` innerText === `hello pi ✨` |
| A6 | open a second tab at the same URL in the same context | same text → the store, not memory, is the source of truth |
| A7 | clear all text, wait, reload | element empty, ghost placeholder visible, `getItem` === `''`, default NOT restored |
| A8 | evaluate `localStorage.removeItem('hello-world.text')`, reload | `hello world` |
| A9 | viewports 375x812 and 1440x900 | `document.documentElement.scrollWidth <= clientWidth`; screenshots saved |
| A10 | emulate `prefers-reduced-motion: reduce`, reload | no errors; computed style of `.aurora-1` reports no animation |
| A11 | whole run | **zero** console errors / unhandled page errors |
| A12 | `git status --porcelain` | only the files listed in 3 appear |
| A13 | dependency audit | no `node_modules/`, no lockfile, no `dist/`, no remote asset reference (`grep -rn 'https://' index.html styles.css src` is empty) |

If no browser tool exists: run A1 with `curl --fail`, and record browser e2e as blocked with the exact reason (VALIDATION `Blocked Validation`) — do not claim persistence is proven.
→ Outcome: the actual requirement — text survives a reload — is demonstrated, plus aesthetic and overflow checks.

**Step 10 — Completion evidence** in the VALIDATION format (Validation / Acceptance criteria / Not run / Blockers), with the screenshots attached for the aesthetic criteria.

## 6. Tests that prove each step

| Step | Proof |
| 1-2 | `node --test tests/` — 6 unit tests covering default, verbatim round-trip, key name, empty-string boundary, write failure, read failure (fail → pass) |
| 3-4 | e2e A2/A4/A5/A6/A7/A8 exercise the wiring for real; `node --check` guards syntax |
| 5 | e2e A9 (no horizontal overflow at 375 px and 1440 px), A10 (reduced motion honoured), screenshots for human sign-off |
| 6-8 | `sh` block in `validation.md` executes green from the repo root |
| all | A11 (clean console), A12 (intended diff only), A13 (zero dependencies actually added) |

`main.js` is about 45 lines of single-purpose wiring and is covered end-to-end rather than mocked, so validation does not mock away the behaviour it is meant to prove.

## 7. Assumptions

- A1 `localStorage` is an acceptable store (single browser, single profile) — see R1.
- A2 Node >= 18 available; if not, unit tests are declared blocked, not skipped silently.
- A3 `python3 -m http.server` sends no cache headers, so reloads always fetch the edited code.
- A4 Plain text only; images/formatting were not requested.
- A5 Served over HTTP, never opened via `file://` (ES module CORS).
- A6 `contenteditable=true` + `innerText` (rather than `plaintext-only`) for browser coverage; plain-text paste is enforced by the paste handler.
- A7 No light theme, no i18n, no analytics, no test infra beyond built-in `node:test`.

## 8. Risks / open questions

- **R1 — persistence scope (needs confirmation).** If `across sessions` means across browsers or devices, client-side storage cannot satisfy it and this plan is wrong at the root. Fallback, still zero dependencies (~45 extra lines): `server.js` on `node:http` serving statics plus `GET/PUT /api/text` backed by `data/text.json`; `text-store.js` keeps the same shape with a fetch-backed implementation, the UI is unchanged and the step-1 tests survive by swapping the fake. **Confirm before implementation if cross-device sync is expected.**
- **R2 — `innerText` round-trip.** Chromium writes `<div>`/`<br>` for multi-line edits, so a trailing blank line may be normalised away on save. Accepted as out of scope for a one-line greeting; documented in `architecture.md` rather than papered over with fragile DOM walking.
- **R3 — `visually stunning` is subjective.** Automatable proxies only: animation active, no overflow at 375 px, contrast readable, console clean. A human must approve the screenshots; rejection means iterating on step 5 only.
- **R4 — browser tooling availability.** See step 9 fallback and VALIDATION `Blocked Validation`.
- **R5 — `backdrop-filter`.** Safari needs the `-webkit-` prefix; where unsupported the card still renders as a translucent panel, no broken layout.
- **R6 — private mode / quota.** Writes return `false`, the UI says so, unit test 5 covers it; no further speculative handling.

## 9. Explicitly deferred

Cross-device sync, reset button, rich/multi-line text, light theme, CI workflow, npm packages, caching/SEO/PWA, deployment. Each needs its own request.

## 10. Step shape

```text
1. probe environment         → outcome: known toolchain, zero installs
2. failing unit tests        → outcome: 6 deterministic failing tests define persistence
3. text-store.js             → outcome: 6 unit tests pass
4. index.html                → outcome: accessible markup + editable node
5. main.js                   → outcome: edits save, reload restores, state visible
6. styles.css                → outcome: aurora + glass + glowing type at 375 px and 1440 px
7. docs + validation.md      → outcome: factory runs the checks automatically
8. node --check + HTTP smoke → outcome: static and serving gates green
9. browser e2e A1-A13        → outcome: persistence + aesthetic criteria demonstrated with evidence
10. evidence summary         → outcome: acceptance criteria explicitly demonstrated
```
