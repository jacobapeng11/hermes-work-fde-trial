#!/usr/bin/env bash

if [[ "${HERMES_NO_PROXY_ENV_LOADED:-0}" == "1" ]]; then
  hermes_clear_proxy_env
  return 0
fi

readonly HERMES_NO_PROXY_ENV_LOADED=1
readonly HERMES_PROXY_ENV_FILE="$(
  cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
)/proxy-env-vars.txt"

hermes_clear_proxy_env() {
  local variable
  while IFS= read -r variable; do
    [[ -n "$variable" ]] || continue
    unset "$variable"
  done < "$HERMES_PROXY_ENV_FILE"
}

hermes_clear_proxy_env
