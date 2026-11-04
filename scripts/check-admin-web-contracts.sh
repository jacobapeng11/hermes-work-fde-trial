#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGETS=(
  "frontend/admin-web/src"
  "packages/schemas/src"
)

FORBIDDEN_PATTERN='BusinessWorkspace|copaw_|worker[[:space:]_-]*pool|kubeconfig|host[[:space:]_-]*path'

if rg -n -i "$FORBIDDEN_PATTERN" "${TARGETS[@]}"; then
  echo "admin-web contract check: forbidden platform-internal or legacy term found" >&2
  exit 1
fi

python3 - <<'PY'
from __future__ import annotations

from pathlib import Path

root = Path.cwd()
schema = root / "packages" / "schemas" / "src" / "index.ts"
admin_src = root / "frontend" / "admin-web" / "src"

text = schema.read_text(encoding="utf-8")
marker = "export interface AdminModelConfigEntrySummary"
start = text.find(marker)
if start < 0:
    raise SystemExit("admin-web contract check: AdminModelConfigEntrySummary missing")
end = text.find("\n}", start)
summary_block = text[start:end]
for forbidden in ("apiKey?:", "apiKeyEncrypted", "providerApiKey", "runtimeToken"):
    if forbidden in summary_block:
        raise SystemExit(f"admin-web contract check: model config summary exposes {forbidden}")
if "credentialConfigured" not in summary_block:
    raise SystemExit("admin-web contract check: model config summary must expose credentialConfigured")

for path in admin_src.rglob("*"):
    if path.suffix not in {".ts", ".tsx"}:
        continue
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if ("localStorage.setItem" in line or "sessionStorage.setItem" in line) and any(
            marker in line for marker in ("apiKey", "api_key", "providerApiKey", "provider_api_key")
        ):
            rel = path.relative_to(root)
            raise SystemExit(f"admin-web contract check: model provider credential stored in frontend storage at {rel}:{lineno}")
PY

echo "admin-web contract check: ok"
