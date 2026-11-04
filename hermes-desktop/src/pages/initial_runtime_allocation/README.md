# Initial runtime allocation tests

This module guards the first-login personal chat experience while Runtime is
still being allocated. It verifies that the control-plane session is created
early enough for the composer to accept a turn, and that the turn uses the
existing exact-session prewarm queue instead of submitting before the grant is
ready. Startup copy is shown only after a read-only backend context identifies
first allocation or scaled-down recovery; uncertain and warm states stay silent.

Run with:

```bash
npm --prefix frontend/user-web test -- src/pages/initial_runtime_allocation/UserChatPage.test.tsx
```
