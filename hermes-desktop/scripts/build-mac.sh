#!/usr/bin/env bash
# One-command macOS packaging entry, mirroring build-win-from-backend-env.ps1.
# Run on a Mac with the monorepo checked out and Node >= 22.12 installed:
#
#   scripts/build-mac.sh [UPDATE_URL] [options]
#
# Options:
#   --cert <p12-path>          Set CSC_LINK to a local Developer ID p12 file
#   --cert-password <password> Set CSC_KEY_PASSWORD (see warning below)
#   --allow-unsigned           Local-only build without the enterprise identity
#
# UPDATE_URL defaults to the desktop-update endpoint derived from PUBLIC_WEB_BASE
# in the selected backend environment when omitted.
#
# DMG/hdiutil/codesign only exist on macOS — this cannot run from Windows.
# The bundle ships no builtin Python: macOS machines use their system python3
# (builtinPythonDir in src/main is win32-only). For distribution-grade builds,
# install a Developer ID Application identity in the keychain, or provide
# CSC_LINK/CSC_KEY_PASSWORD. electron-builder recursively signs the app and its
# nested helpers/frameworks; Apple Notary submission is done by an external
# operator with the matching Apple Developer team credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  cat >&2 <<'EOF'
用法:
  scripts/build-mac.sh [UPDATE_URL] [--cert <p12-path>] [--cert-password <password>] [--allow-unsigned]

说明:
  UPDATE_URL 缺省时从 backend/.env 的 PUBLIC_WEB_BASE 推导。
  --cert 会导出为 CSC_LINK；--cert-password 会导出为 CSC_KEY_PASSWORD。
  --cert-password 会进入 shell history 和进程参数，CI 建议改用环境变量。
  --allow-unsigned 仅用于本机调试，产物禁止分发。
EOF
}

update_url_arg=""
cert_path=""
cert_password=""
cert_password_provided=false
allow_unsigned_flag=false

while (($# > 0)); do
  case "$1" in
    --cert)
      if (($# < 2)); then
        echo "--cert 缺少 <p12-path> 参数。" >&2
        exit 2
      fi
      cert_path=$2
      shift 2
      ;;
    --cert=*)
      cert_path=${1#*=}
      shift
      ;;
    --cert-password)
      if (($# < 2)); then
        echo "--cert-password 缺少 <password> 参数。" >&2
        exit 2
      fi
      cert_password=$2
      cert_password_provided=true
      shift 2
      ;;
    --cert-password=*)
      cert_password=${1#*=}
      cert_password_provided=true
      shift
      ;;
    --allow-unsigned)
      allow_unsigned_flag=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while (($# > 0)); do
        if [[ -n "$update_url_arg" ]]; then
          echo "UPDATE_URL 只能指定一次。" >&2
          exit 2
        fi
        update_url_arg=$1
        shift
      done
      break
      ;;
    --*)
      echo "未知参数: $1" >&2
      usage
      exit 2
      ;;
    *)
      if [[ -n "$update_url_arg" ]]; then
        echo "UPDATE_URL 只能指定一次。" >&2
        exit 2
      fi
      update_url_arg=$1
      shift
      ;;
  esac
done

if [[ -n "$cert_path" && ! -f "$cert_path" ]]; then
  echo "证书文件不存在: $cert_path" >&2
  exit 2
fi
if [[ -n "$cert_path" ]]; then
  export CSC_LINK="$cert_path"
fi
if [[ "$cert_password_provided" == true ]]; then
  echo "警告：--cert-password 可能保留在 shell history/进程参数中；CI 建议使用 CSC_KEY_PASSWORD 环境变量。" >&2
  export CSC_KEY_PASSWORD="$cert_password"
elif [[ -n "$cert_path" && "${CSC_KEY_PASSWORD+x}" != "x" && -t 0 ]]; then
  read -rsp "P12 password: " CSC_KEY_PASSWORD
  printf '\n' >&2
  export CSC_KEY_PASSWORD
fi

BACKEND_ENV="${BACKEND_ENV:-../backend/.env}"

# Same intranet-friendly mirrors as the Windows script.
export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}"
export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"

if [[ ! -f "$BACKEND_ENV" ]]; then
  echo "缺少 $BACKEND_ENV（Enterprise SSO 登录配置）" >&2
  exit 1
fi
# Pull only the Enterprise SSO SSO settings, like the Windows script does.
while IFS='=' read -r key value; do
  value="${value%$'\r'}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  export "$key=$value"
done < <(grep -E '^[[:space:]]*(VITE_LEGACY_SSO_LOGIN_BASE_URL|VITE_LEGACY_SSO_APP_NAME|PUBLIC_WEB_BASE)[[:space:]]*=' "$BACKEND_ENV" | sed 's/^[[:space:]]*//')
: "${VITE_LEGACY_SSO_LOGIN_BASE_URL:?backend/.env 缺少 VITE_LEGACY_SSO_LOGIN_BASE_URL}"
: "${VITE_LEGACY_SSO_APP_NAME:?backend/.env 缺少 VITE_LEGACY_SSO_APP_NAME}"
: "${PUBLIC_WEB_BASE:?backend/.env 缺少 PUBLIC_WEB_BASE}"

export VITE_API_BASE_URL="/api/v1"
export VITE_AUTH_MODE="enterprise_idpAES"
export COWORK_SERVER_ORIGIN="${COWORK_SERVER_ORIGIN:-$PUBLIC_WEB_BASE}"
UPDATE_URL="${update_url_arg:-${PUBLIC_WEB_BASE%/}/api/v1/desktop-updates/}"
export COWORK_DESKTOP_UPDATE_URL="$UPDATE_URL"

# write-update-config stages the empty build/python directory required by the
# shared extraResources configuration; the Windows build fills it with CPython.
npm run write-update-config
npm run build

# The builder configuration makes code signing mandatory. Keep an explicit,
# local-only escape hatch for developers without the enterprise certificate;
# it must never be used for distribution artifacts.
builder_args=(--mac --publish never)
if [[ "$allow_unsigned_flag" == true ]]; then
  ALLOW_UNSIGNED_MAC=1
fi
case "${ALLOW_UNSIGNED_MAC:-0}" in
  0)
    ;;
  1)
    echo "ALLOW_UNSIGNED_MAC=1：仅生成本机测试包，禁止分发。" >&2
    builder_args+=(--config.mac.forceCodeSigning=false)
    ;;
  *)
    echo "ALLOW_UNSIGNED_MAC 只能为 0 或 1。" >&2
    exit 2
    ;;
esac
if npm exec electron-builder -- "${builder_args[@]}"; then
  unset CSC_KEY_PASSWORD cert_password 2>/dev/null || true
else
  status=$?
  unset CSC_KEY_PASSWORD cert_password 2>/dev/null || true
  exit "$status"
fi
