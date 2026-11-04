#!/usr/bin/env bash
set -euo pipefail

# kubectl must never inherit a developer/CI HTTP or SOCKS proxy.  This entry
# point is also covered by the project-wide shell contract test.
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

usage() {
  cat <<'EOF'
用法: configure-knative-resource-profile.sh [--profile PATH] [--kubeconfig PATH] [--apply]

默认只输出 plan；只有显式 --apply 才会调用 kubectl set resources。
EOF
}

script_dir="$(cd "$(dirname "$0")" && pwd)"
profile="$script_dir/../deploy/kubernate/single-node-4c/resource-profile.env"
python_bin="${PYTHON_BIN:-$script_dir/../backend/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3 || true)"
fi
[[ -n "$python_bin" ]] || { echo "需要 python3 解析 Kubernetes JSON" >&2; exit 1; }
kubeconfig=""
apply=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --kubeconfig) kubeconfig="$2"; shift 2 ;;
    --apply) apply=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -r "$profile" ]] || { echo "资源档位不可读: $profile" >&2; exit 1; }
kubectl_args=()
[[ -n "$kubeconfig" ]] && kubectl_args+=(--kubeconfig "$kubeconfig")

while IFS='|' read -r deployment namespace container cpu; do
  [[ -z "${deployment//[[:space:]]/}" || "$deployment" == \#* ]] && continue
  [[ "$deployment" =~ ^[a-z0-9.-]+$ && "$namespace" =~ ^[a-z0-9.-]+$ && "$container" =~ ^[a-z0-9.-]+$ && "$cpu" =~ ^[0-9]+m$ ]] || {
    echo "资源档位行格式无效: $deployment|$namespace|$container|$cpu" >&2; exit 1;
  }
  json="$(kubectl "${kubectl_args[@]}" -n "$namespace" get deployment "$deployment" -o json)"
  found="$("$python_bin" -c 'import json,sys; d=json.load(sys.stdin); print(any(c.get("name")==sys.argv[1] for c in d.get("spec",{}).get("template",{}).get("spec",{}).get("containers",[])))' "$container" <<<"$json")"
  [[ "$found" == True ]] || { echo "未找到目标容器: $namespace/$deployment:$container" >&2; exit 1; }
  current="$("$python_bin" -c 'import json,sys; d=json.load(sys.stdin); cs=d.get("spec",{}).get("template",{}).get("spec",{}).get("containers",[]); c=next(x for x in cs if x.get("name")==sys.argv[1]); print((c.get("resources") or {}).get("requests",{}).get("cpu","未设置"))' "$container" <<<"$json")"
  printf '%s/%s:%s  %s -> %s\n' "$namespace" "$deployment" "$container" "$current" "$cpu"
  if "$apply"; then
    kubectl "${kubectl_args[@]}" -n "$namespace" set resources "deployment/$deployment" -c "$container" --requests="cpu=$cpu" >/dev/null
  fi
done < "$profile"

if "$apply"; then
  echo "已应用资源档位；请按组件逐一观察 Ready、Pending 和实际使用量。"
else
  echo "plan-only：未修改 Kubernetes。"
fi
