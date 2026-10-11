# Plan: Visually stunning, editable "hello world" web app

## Context
The repository is nearly empty: `AGENTS.md`, plus `docs/architecture.md`, `docs/coding-standards.md` and `docs/VALIDATION.md` (all template text), and an empty `init.txt`. There is no existing stack, build tool, linter or test runner, so this plan picks the smallest stack that works: a static site in plain HTML, CSS and ES modules, with no build step and no runtime dependencies. Unit tests use Node's built-in test runner.

## Outcome
Opening the app in a browser shows a full-screen page with "hello world" as the centrepiece. The user can click the text and edit it in place. After a reload, or after closing and reopening the browser, the page shows the last edited text.

## Fixed decisions (normative)
1. **Persistence scope:** the text is saved per browser using `window.localStorage`. "Across sessions" means it survives a page reload and a browser restart in the same browser profile. It is not shared between users or devices, and there is no backend.
2. **Storage contract:** one key, `hello-world:text`, holding a plain-text string value. If the key is missing, the app shows the default text `hello world`.
3. **Save timing:** the text is saved on every `input` event, so nothing is lost if the tab closes mid-edit.
4. **Empty text:** if the edited text is empty or whitespace-only, the key is removed (not stored). When the element loses focus while empty, it shows `hello world` again.
5. **Plain text only:** the text is single-line plain text. Enter does not insert a newline; it ends editing (blur). Pasted content ends up as plain text. The app reads with `textContent` and writes with `textContent`, never `innerHTML`.
6. **Storage failures are surfaced, not swallowed:** if `localStorage` is unavailable or throws (blocked, private mode, quota exceeded), the app still renders and stays editable. It shows the default (or the current edit) and logs a `console.error` that names the failed operation. It must not throw an uncaught error.
7. **No external network requests:** no CDN fonts, scripts or images. Use system font stacks and CSS-only visuals, so the app works offline and an offline load produces no console errors.
8. **No new dependencies:** `package.json` exists only to declare `"type": "module"` and a `test` script. It has no `dependencies` or `devDependencies`.

## Acceptance criteria
- **AC1:** On first load with empty storage, the page shows `hello world` as the dominant visual element.
- **AC2:** The text can be edited in place by clicking or focusing it, using mouse or keyboard. It has an accessible name and a visible focus style.
- **AC3:** After editing the text and reloading the page, the edited text is shown (persistence via `localStorage` under `hello-world:text`).
- **AC4:** Clearing the text and leaving the field shows `hello world` again, and after a reload `hello world` is still shown.
- **AC5:** Enter does not create a line break. Pasted HTML is shown as plain text, and no markup is injected into the DOM.
- **AC6:** If storage throws, the page still loads, shows text and stays editable, and a descriptive `console.error` is logged.
- **AC7:** The design is visually stunning and meets these concrete checks: a full-viewport, animated, multi-colour background (for example a gradient or aurora/mesh effect); large, fluid, responsive headline typography with a gradient or glow treatment; a polished editing affordance (for example a subtle hint, caret colour or focus glow); and it looks good from about 360 px mobile width to desktop. All motion respects `prefers-reduced-motion: reduce`.
- **AC8:** Normal use produces no browser console errors or unhandled page errors.
- **AC9:** `npm test` passes.

## Target structure
The layout below is illustrative; the module split and responsibilities are normative.
```
index.html          # markup: one editable headline, hint text, links CSS + module script
styles.css          # all visual design
src/storage.js      # persistence module (pure, storage injected)
src/app.js          # DOM wiring: load → render, input → save, Enter/blur/paste handling
test/storage.test.js
package.json        # {"private": true, "type": "module", "scripts": {"test": "node --test"}}
README.md           # how to run and test
docs/architecture.md
```
**Dependency rule:** `app.js` depends on `storage.js`. `storage.js` has no DOM or global dependencies; the storage object is passed in as a parameter.

## Steps

### 1. Persistence module and unit tests
- **Files:** `package.json`, `src/storage.js`, `test/storage.test.js`.
- **Contract (normative):** export the default text and two functions:
  - one that loads the text from a given Storage-like object (`getItem`/`setItem`/`removeItem`). It returns the stored string, or `hello world` when the key is missing or the read throws.
  - one that saves text to that object. Whitespace-only text removes the key; any other text is stored unchanged. A write or remove error is caught and reported with `console.error` and must not reach the caller.
  - The exact names and signatures are up to the implementer.
- **Expected result:** `npm test` passes.
- **Tests (each uses an in-memory fake storage, with `console.error` stubbed through the runner's mocking):**
  - load with an empty store returns `hello world` (AC1)
  - save then load round-trips the text, using key `hello-world:text` (AC3)
  - saving whitespace-only text removes the key, and load then returns the default (AC4)
  - a read that throws returns the default and logs an error (AC6)
  - a write that throws does not throw and logs an error (AC6)

### 2. Markup and app wiring
- **Files:** `index.html`, `src/app.js`.
- **Expected result:**
  - The page renders the loaded text into an editable headline. A suggested approach is an `h1` with `contenteditable` (`plaintext-only` where supported, plus paste handling as a fallback); an input styled as a headline is also acceptable if AC5 and AC7 are still met.
  - The headline has `role`/`aria-label` (or equivalent) so its purpose is announced, plus `spellcheck="false"`.
  - Wiring:
    - on `input`, save the element's `textContent`
    - on Enter, prevent the default action and blur
    - on blur, if the text is empty, show the default
    - pasted content is inserted as plain text
  - `app.js` accesses `window.localStorage` inside a try/catch. If that access throws, it reports the error and continues without persistence (AC6).
  - Set `<title>`, `lang`, the viewport meta tag and a meta description.
- **Tests:** the E2E checks in step 5 cover AC2–AC6. No DOM unit-test framework is added, because the logic worth unit-testing lives in `storage.js`.

### 3. Visual design
- **Files:** `styles.css`, and `index.html` only if decorative elements are needed.
- **Expected result:** meets AC7. Ideas (illustrative, not mandated):
  - a dark base with slowly drifting, blurred colour blobs or a conic or mesh gradient
  - headline sized with `clamp()`, using an animated gradient fill (`background-clip: text`) or a soft glow
  - glassmorphism or noise texture for depth
  - a subtle "click to edit" hint that fades while focused
  - a coloured caret and a glowing focus ring
  - CSS-only, using system fonts, with all animation disabled or reduced under `prefers-reduced-motion`
- Keep text contrast readable (the WCAG AA intent) over the animated background.
- Prefer GPU-friendly properties (transform, opacity) for animation.

### 4. Documentation
- **Files:** `README.md`, `docs/architecture.md`.
- **Expected result:**
  - The README says how to serve the app locally with any static file server (for example `python3 -m http.server`). It notes that ES modules do not load over `file://`, and it documents `npm test`.
  - `docs/architecture.md` describes the two modules, their responsibilities, the dependency rule, and the storage key and format. It records the compromise of per-browser `localStorage` persistence (no backend, so the text is not shared across devices).

### 5. Validation
These are the proportional checks, per `docs/VALIDATION.md`:
- **Unit:** `npm test` (AC9, plus storage behaviour for AC1, AC3, AC4 and AC6).
- **Static:** `node --check` on each JS file. If a formatter or linter (Prettier or ESLint) is already available in the environment, run it on the changed files. Do not add one as a dependency; if none is available, record that it was not run.
- **Runtime:** serve the folder with a static server, load the page, and confirm the module loads with no 404s or console errors.
- **E2E** (using Pi's browser automation, capturing console messages throughout; no committed Playwright files):
  1. With fresh storage, `hello world` is visible (AC1).
  2. Focus the headline, select all, type new text, and confirm `localStorage['hello-world:text']` matches (AC2, AC3).
  3. Reload and confirm the new text is shown (AC3).
  4. Press Enter and confirm there is still no line break or `<br>`/`<div>` child (AC5).
  5. Paste or insert `<b>x</b>`-style HTML and confirm it appears as literal text with no element children (AC5).
  6. Clear the text, blur, and confirm `hello world` is shown; reload and confirm it again (AC4).
  7. Override `localStorage` to throw (for example with an init script), reload, and confirm the text renders, can be edited and logs a `console.error` (AC6). This is the only case where console errors are expected.
  8. Confirm there are no other console errors or page errors (AC8).
- **Visual:** take screenshots at a mobile width (about 360–400 px) and a desktop width (about 1440 px), plus one while focused or editing, and check them against the AC7 checklist. Also check the reduced-motion emulation.
- **Acceptance:** report each AC with its evidence, using the VALIDATION.md completion format.

## Assumptions
- Node.js with the built-in test runner and its mocking support is available. A static file server is available, for example Python's `http.server` or any equivalent.
- "Sessions" means browser sessions on the same device and profile. If cross-device or shared persistence is intended, that needs a backend and is out of scope for this plan.
- Single-line text is acceptable, since it is a headline.
- Per AGENTS.md, the implementer may keep a brief `PLAN.md` that mirrors these steps.

## Open risks
- **Subjective design bar:** "visually stunning" is subjective. AC7 turns it into a concrete checklist checked by screenshots, but a human may still want changes.
- **contenteditable quirks:** browsers differ in how they handle stray `<br>` elements, pastes and `plaintext-only` support. These are covered by the E2E checks for Enter, paste and empty text.
- **No linter configured:** with no linter in the repo, style conformance rests on `node --check` plus whatever tool the environment provides.
- **Animation cost:** heavy blur or filter animations can be expensive on low-end devices. Keep the number of animated layers small and respect reduced motion.
