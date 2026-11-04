# Desktop renderer tests

This directory contains Node-based tests for renderer business modules that do
not require a DOM renderer. `chat-image-input.test.mjs` guards desktop paste
validation before it reaches the generic attachment upload queue.
`plaza-skill-parity.test.mjs` guards the copied User Web Skill Plaza modules
against accidental desktop drift.

Run the desktop main/renderer unit tests with:

```bash
npm --prefix cowork-hermes-desktop run test:desktop
```
