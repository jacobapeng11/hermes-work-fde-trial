# IM Gateway ordering tests

This module covers durable ingress ticket ordering for personal and application
WeCom messages. It verifies that a successor attachment can be staged after its
predecessor has a durable Runtime outcome, without waiting for the
predecessor's full model response to finish.

Run with:

```bash
backend/.venv/bin/pytest -q backend/tests/unit/im_gateway_ordering/
```
