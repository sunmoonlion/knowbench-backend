#!/usr/bin/env bash

external_write_output() (
  local out="${OUTPUT_ENV_FILE:-/tmp/${SERVICE_NAME:-service}-${DB_ENGINE}.env}"
  local out_dir
  out_dir="$(dirname "$out")"

  if [[ "${ACTION:-provision}" == "deprovision" ]]; then
    if bool_true "${DRY_RUN:-false}"; then
      log "DRY_RUN=true, will remove external output env: $out"
      return 0
    fi
    rm -f -- "$out" || return 1
    log "Removed external output env if exists: $out"
    return 0
  fi

  if bool_true "${DRY_RUN:-false}"; then
    log "DRY_RUN=true, will write external output env: $out"
    return 0
  fi
  umask 077
  mkdir -p "$out_dir" || return 1
  local tmp
  tmp="$(mktemp "$out_dir/.dbctl-env.XXXXXXXX")" || return 1
  trap 'rm -f -- "$tmp"' EXIT
  cat >"$tmp" <<EOF || return 1
SERVICE_NAME=${SERVICE_NAME:-}
ENVIRONMENT=${ENVIRONMENT:-}
DB_ENGINE=${DB_ENGINE}
DB_HOST=${DB_HOST:-}
DB_PORT=${DB_PORT:-}
APP_DB_NAME=${APP_DB_NAME:-}
APP_DB_USER=${APP_DB_USER:-}
APP_DB_PASSWORD=${APP_DB_PASSWORD:-}
APP_DB_URI=${APP_DB_URI:-}
OUTPUT_GENERATED_AT=$(now_rfc3339)
EOF

  mv -fT -- "$tmp" "$out" || return 1
  log "Wrote external output env: $out"
)
