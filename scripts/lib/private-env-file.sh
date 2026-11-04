#!/usr/bin/env bash

hermes_require_private_env_file() {
  local env_file="$1"
  local mode

  mode="$(stat -Lc '%a' -- "$env_file" 2>/dev/null)" || {
    echo "安全检查失败，无法读取配置文件权限：$env_file" >&2
    return 1
  }
  if (( (8#$mode & 077) != 0 )); then
    echo "配置文件不能允许 group/other 访问：$env_file；请执行 chmod 600 \"$env_file\"" >&2
    return 1
  fi
}
