# Review 2

**Verdict:** changes_requested

## Findings

- **[major]** (server.js:71) new URL(req.url, 'http://localhost') is parsed outside the try block at line 72. A request target the HTTP parser accepts but the URL parser rejects (e.g. 'GET http:// HTTP/1.1', 'GET https://:80 HTTP/1.1') makes the async handler reject; the promise is unhandled, so Node's default --unhandled-rejections=throw kills the whole process. Breaks the documented error contract in docs/architecture.md:46-49 (unexpected throw -> 500 + console.error, never silent) and docs/coding-standards.md 'Errors are surfaced, never silently swallowed'. Fix: move the URL parse inside the try, or catch it and reply 400/404.
- **[minor]** (server.js:73) The method gate runs before routing, so HEAD / and HEAD /api/text return 405 instead of behaving like GET (RFC 9110 9.3.2). The behaviour is absent from the HTTP contract table in docs/architecture.md:36-43 - either treat HEAD as GET or document the restriction.
- **[minor]** (tests/api.test.js:1) No test exercises the documented store-failure path (500 {"error":"internal"} plus console.error) described in docs/architecture.md:46-49; injecting a store whose read()/write() throws would cover it. Violates docs/coding-standards.md 'Every behaviour change has a test that exercises it'.
- **[minor]** (store.js:24) write() stores raw UTF-8 text into a file named *.json (plan fixed decision 1 and the file layout call it a JSON file). docs/architecture.md:50-52 records the mismatch as deliberate, but the extension is misleading for any future consumer; either write JSON or name the artifact hello.txt.
- **[minor]** (main.js:7) Number(process.env.PORT ?? 3000) accepts invalid values: PORT=abc makes listen(NaN) throw ERR_SOCKET_BAD_PORT, and an empty PORT= becomes listen(0) - a random ephemeral port while the startup log prints http://localhost:0. README.md documents PORT as a plain override, so this silently changes the default (AGENTS.md §5). Parse and validate explicitly.
- **[minor]** (README.md:31) docs/coding-standards.md requires 'the language's standard formatter and linter'; none is configured and none was added (plan R5 required surfacing this conflict). The deviation is disclosed only in the run report - nothing in the repository records that 'npm run check' is a syntax-only gate, so the standards conflict stays invisible to the next contributor.
