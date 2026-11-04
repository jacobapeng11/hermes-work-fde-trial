#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

if [[ "$#" -lt 1 || "$#" -gt 2 || ! "$1" =~ ^[0-9]+$ ]]; then
  echo "usage: bash scripts/smoke-admin-kubernetes-resources.sh <runtime_workspace_id> [personal|application]" >&2
  exit 2
fi

RUNTIME_WORKSPACE_ID="$1"
RUNTIME_KIND="${2:-personal}"
if [[ "$RUNTIME_KIND" != "personal" && "$RUNTIME_KIND" != "application" ]]; then
  echo "smoke-admin-kubernetes-resources: runtime kind must be personal or application" >&2
  exit 2
fi
BASE_URL="${ADMIN_API_BASE_URL:-http://127.0.0.1:8011/api/v1}"
AUTH_HEADER=()
if [[ -n "${ADMIN_ACCESS_TOKEN:-}" ]]; then
  AUTH_HEADER=(-H "Authorization: Bearer $ADMIN_ACCESS_TOKEN")
fi

temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT

curl -fsS "${AUTH_HEADER[@]}" "$BASE_URL/admin/resources/cluster-summary" >"$temp_dir/cluster.json"
curl -fsS "${AUTH_HEADER[@]}" \
  "$BASE_URL/admin/resources/runtime-workspaces/kubernetes-snapshots" \
  >"$temp_dir/runtime-list.json"
curl -fsS "${AUTH_HEADER[@]}" \
  "$BASE_URL/admin/resources/runtime-workspaces/$RUNTIME_WORKSPACE_ID/kubernetes?runtime_kind=$RUNTIME_KIND" \
  >"$temp_dir/runtime.json"
curl -fsS "${AUTH_HEADER[@]}" \
  "$BASE_URL/admin/resources/runtime-workspaces/$RUNTIME_WORKSPACE_ID/events?runtime_kind=$RUNTIME_KIND" \
  >"$temp_dir/events.json"
curl -fsS "${AUTH_HEADER[@]}" \
  "$BASE_URL/admin/resources/runtime-workspaces/$RUNTIME_WORKSPACE_ID/kubernetes/yaml-summary?runtime_kind=$RUNTIME_KIND" \
  >"$temp_dir/yaml.json"

python3 - "$temp_dir" "$RUNTIME_WORKSPACE_ID" "$RUNTIME_KIND" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
workspace_id = int(sys.argv[2])
runtime_kind = sys.argv[3]
cluster = json.loads((root / "cluster.json").read_text())
runtime = json.loads((root / "runtime.json").read_text())
events = json.loads((root / "events.json").read_text())
runtime_list = json.loads((root / "runtime-list.json").read_text())
yaml_summary = json.loads((root / "yaml.json").read_text())
assert cluster["node_count"] >= cluster["ready_node_count"] >= 0
assert runtime["runtime_workspace_id"] == workspace_id
assert runtime["runtime_kind"] == runtime_kind
assert any(
    item["runtime_workspace_id"] == workspace_id
    and item["runtime_kind"] == runtime_kind
    for item in runtime_list
)
assert isinstance(events, list)
assert yaml_summary["runtime_workspace_id"] == workspace_id
assert yaml_summary["runtime_kind"] == runtime_kind
serialized = "\n".join(
    (root / name).read_text().lower()
    for name in ("cluster.json", "runtime-list.json", "runtime.json", "events.json", "yaml.json")
)
for forbidden in ("kubeconfig", "secretkeyref", "hostpath", "api_key_encrypted"):
    assert forbidden not in serialized, forbidden
PY

echo "admin kubernetes resources smoke: ok"
