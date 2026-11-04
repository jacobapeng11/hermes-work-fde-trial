# Platform presence tests

This module guards the admin presence dashboard. Unit tests cover TTL-based
Web/desktop presence windows, unique-user deduplication, organization isolation,
Redis failure handling, short-lived caching, daily login aggregation, display
names, the personal Runtime ready gate, cross-day carry-over presence, and the
admin API response contract.

Run with:

```bash
backend/.venv/bin/pytest -q backend/tests/unit/platform_presence
```
