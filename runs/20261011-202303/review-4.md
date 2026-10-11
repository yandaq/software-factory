# Review 4

**Verdict:** changes_requested

## Findings

- **[major]** (public/app.js:57-80) Input handlers are enabled before the initial GET completes, so a delayed response can overwrite an edit made during loading and the pending debounce will then persist the old server value. This violates plan acceptance criteria A2/A3; prevent stale load results from replacing user input and cover the race with a browser test.
- **[minor]** (public/app.js:67-69) The blur flush calls save() without keepalive:true, violating Plan step 4's explicit requirement that both blur and pagehide flushes use keepalive.
- **[minor]** (docs/architecture.md:3) The architecture claims store.js is the only filesystem owner, while server.js uses readFileSync for static assets. Document the narrower invariant that store.js exclusively owns persistent greeting-file access.
