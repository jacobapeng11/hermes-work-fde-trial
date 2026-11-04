#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

# Builds and publishes the two independent static frontends for a formal
# server. It intentionally does not restart services, start Vite, initialize
# a database, or run broad frontend test suites.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_WEB_DIR="$ROOT_DIR/frontend/user-web"
ADMIN_WEB_DIR="$ROOT_DIR/frontend/admin-web"
release_revision="$(git -C "$ROOT_DIR" rev-parse HEAD)"
[[ "$release_revision" =~ ^[0-9a-f]{40}$ ]] || {
  echo "build-public-prod: unable to determine the 40-character release revision" >&2
  exit 1
}

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-frontend-auth-config.sh"
public_prod_acquire_lock
public_prod_load_backend_env
public_prod_require_topology
public_frontend_require_legacy_sso_config

for command in npm install cp mv mktemp rm; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "build-public-prod: required command is missing: $command" >&2
    exit 1
  }
done

for package_dir in "$USER_WEB_DIR" "$ADMIN_WEB_DIR"; do
  [[ -d "$package_dir/node_modules" ]] || {
    echo "build-public-prod: missing dependencies: $package_dir/node_modules" >&2
    echo "Install the locked frontend dependencies before building." >&2
    exit 1
  }
done

echo "build-public-prod: building user and admin static bundles"
(
  cd "$USER_WEB_DIR"
  VITE_AUTH_MODE="$HERMES_AUTH_MODE" \
    VITE_LEGACY_SSO_LOGIN_BASE_URL="$VITE_LEGACY_SSO_LOGIN_BASE_URL" \
    VITE_LEGACY_SSO_APP_NAME="$VITE_LEGACY_SSO_APP_NAME" npm run build
)
(
  cd "$ADMIN_WEB_DIR"
  VITE_AUTH_MODE="$HERMES_AUTH_MODE" \
    VITE_LEGACY_SSO_LOGIN_BASE_URL="$VITE_LEGACY_SSO_LOGIN_BASE_URL" \
    VITE_LEGACY_SSO_APP_NAME="$VITE_LEGACY_SSO_APP_NAME" npm run build
)

for path in "$USER_WEB_DIR/dist/index.html" "$ADMIN_WEB_DIR/dist/index.html"; do
  [[ -f "$path" ]] || {
    echo "build-public-prod: expected build output is missing: $path" >&2
    exit 1
  }
done

for dist_dir in "$USER_WEB_DIR/dist" "$ADMIN_WEB_DIR/dist"; do
  printf '%s\n' "$release_revision" >"$dist_dir/.hermes-release-revision"
done

install -d "$PUBLIC_RELEASE_ROOT"
stage_root="$(mktemp -d "$PUBLIC_RELEASE_ROOT/.build-public-prod.XXXXXX")"
cleanup() {
  rm -rf "$stage_root"
}
trap cleanup EXIT

install -d "$stage_root/frontend/user-web" "$stage_root/frontend/admin-web"
cp -a "$USER_WEB_DIR/dist" "$stage_root/frontend/user-web/dist"
cp -a "$ADMIN_WEB_DIR/dist" "$stage_root/frontend/admin-web/dist"

publish_bundle() {
  local name="$1"
  local staged="$stage_root/frontend/$name/dist"
  local target="$PUBLIC_RELEASE_ROOT/frontend/$name/dist"
  local rollback_root=""

  install -d "$(dirname "$target")"
  if [[ -e "$target" ]]; then
    rollback_root="$(mktemp -d "$PUBLIC_RELEASE_ROOT/.build-public-prod.rollback-${name}.XXXXXX")"
    mv "$target" "$rollback_root/dist"
  fi
  if ! mv "$staged" "$target"; then
    echo "build-public-prod: failed to publish $name bundle" >&2
    if [[ -n "$rollback_root" && -e "$rollback_root/dist" ]]; then
      if ! mv "$rollback_root/dist" "$target"; then
        echo "build-public-prod: failed to restore previous $name bundle from $rollback_root/dist" >&2
        return 1
      fi
    fi
    if [[ -n "$rollback_root" ]]; then
      rm -rf -- "$rollback_root"
    fi
    return 1
  fi
  if [[ -n "$rollback_root" ]]; then
    if ! rm -rf -- "$rollback_root"; then
      echo "build-public-prod: failed to remove temporary $name rollback directory: $rollback_root" >&2
      return 1
    fi
  fi
  echo "build-public-prod: published $name bundle without retaining the previous build"
}

publish_bundle user-web
publish_bundle admin-web
printf '%s\n' "$release_revision" >"$PUBLIC_RELEASE_ROOT/.hermes-release-revision"
echo "build-public-prod: static bundles published under $PUBLIC_RELEASE_ROOT"
