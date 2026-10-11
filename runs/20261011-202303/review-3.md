# Review 3

**Verdict:** changes_requested

## Findings

- **[major]** (public/app.js:34) `save()` clears `pending` before the request completes. If `blur` starts a normal fetch and `pagehide` follows, the pagehide handler sees no pending work and does not issue the required keepalive request; navigation may cancel the in-flight save and lose the latest edit. This violates plan step 4's requirement to flush pending changes on pagehide.
- **[major]** (store.js:13) The store parses reads as JSON and JSON-stringifies writes instead of preserving the supplied text as the file's UTF-8 contents. This breaks the step 2 store contract and A3's requirement that the artifact literally contain the edited text; tests/store.test.js:30 was changed to expect the divergent representation rather than testing the specified contract.
