#!/usr/bin/env bash

# The Enterprise SSO application is a build-time frontend identity.  It must be
# supplied by the selected deployment environment; never infer it from the
# public hostname because an Enterprise SSO application name and its callback origin
# are managed independently by Enterprise SSO.
public_frontend_require_legacy_sso_config() {
  if [[ "${HERMES_AUTH_MODE:-}" == "oauth2" ]]; then
    VITE_LEGACY_SSO_LOGIN_BASE_URL=""
    VITE_LEGACY_SSO_APP_NAME=""
    export VITE_LEGACY_SSO_LOGIN_BASE_URL VITE_LEGACY_SSO_APP_NAME
    return 0
  fi
  local login_base_url="${VITE_LEGACY_SSO_LOGIN_BASE_URL:-}"
  local app_name="${VITE_LEGACY_SSO_APP_NAME:-}"

  if [[ -z "$login_base_url" || -z "$app_name" ]]; then
    echo "public frontend: VITE_LEGACY_SSO_LOGIN_BASE_URL and VITE_LEGACY_SSO_APP_NAME are required." >&2
    return 1
  fi

  if ! python3 - "$login_base_url" "$app_name" <<'PY'
from urllib.parse import urlparse
import re
import sys

login_base_url, app_name = sys.argv[1:]
parsed = urlparse(login_base_url)
if (
    parsed.scheme != "https"
    or not parsed.hostname
    or parsed.query
    or parsed.fragment
    or parsed.username
    or parsed.password
):
    raise SystemExit(
        "public frontend: VITE_LEGACY_SSO_LOGIN_BASE_URL must be an absolute HTTPS URL without credentials/query/fragment."
    )
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", app_name):
    raise SystemExit("public frontend: VITE_LEGACY_SSO_APP_NAME contains invalid characters.")
PY
  then
    return 1
  fi

  export VITE_LEGACY_SSO_LOGIN_BASE_URL VITE_LEGACY_SSO_APP_NAME
}
