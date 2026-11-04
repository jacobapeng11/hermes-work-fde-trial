# Runtime Channel attachment tests

This module covers Backend selection between the Channel Fast Lane and the
ordinary Runtime command path, as well as recovery when an ordinary Runtime
command event stream is still open at a terminal Run snapshot. Attachments
require the ordinary command path because the deployed Fast Lane v1 protocol
does not carry runtime attachment payloads into the Supervisor Run.

Run with:

```bash
backend/.venv/bin/pytest -q backend/tests/unit/runtime_channel_attachment/
```
