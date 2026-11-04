# Desktop tests

Node-based tests for the cross-platform desktop client. They cover main-process
capability protocol contracts, command runner behavior, desktop routing, window
bounds and drag-region contracts, preload capability guards, workspace file
handling, browser snapshots, and built-in Python detection — all without
launching Electron.

## Layout

- `*.test.mjs` — main-process and shared module tests.
- `renderer/` — renderer business module tests that do not require a DOM
  renderer; see `renderer/README.md`.
- `packaging/` — release asset and packaging entry point tests; see
  `packaging/README.md`.
- `authentication/` — gateway admission, login configuration recovery and
  cookie/navigation boundaries; see `authentication/README.md`.
- `ts-resolver.mjs`, `ts-resolver-register.mjs` — test helpers that register the
  TypeScript strip-types resolver used by `node --experimental-strip-types`;
  they are not tests themselves.

## Running

```bash
npm run test:desktop
```

Run from `cowork-hermes-desktop/`. The script executes
`tests/*.test.mjs`, `tests/renderer/*.test.mjs`, and `tests/packaging/*.test.mjs`
via the Node test runner. New tests follow the naming pattern `<module>.test.mjs`
and must be picked up by this single entry command.
