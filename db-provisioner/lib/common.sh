#!/usr/bin/env bash

source "$(dirname "${BASH_SOURCE[0]}")/env-values.sh"

set -euo pipefail

readonly DBP_VERSION="0.1.0"

log() {
  printf '[db-provisioner] %s\n' "$*"
}

warn() {
  printf '[db-provisioner][warn] %s\n' "$*" >&2
}

err() {
  printf '[db-provisioner][error] %s\n' "$*" >&2
}

die() {
  err "$*"
  exit 1
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "Missing command: $cmd"
}

require_non_empty() {
  local key="$1"
  local val="${2:-}"
  [[ -n "$val" ]] || die "Required config is empty: $key"
}

load_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || die "Config file not found: $env_file"
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
}

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

bool_true() {
  local v
  v="$(lower "${1:-false}")"
  [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" || "$v" == "on" ]]
}

bool_false() {
  ! bool_true "${1:-false}"
}

now_rfc3339() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

redact() {
  local s="${1:-}"
  local n=${#s}
  if (( n <= 4 )); then
    printf '****'
  else
    printf '%s****%s' "${s:0:2}" "${s:n-2:2}"
  fi
}

precheck_enabled() {
  bool_false "${DB_PRECHECK_ENABLED:-true}" && return 1
  return 0
}

wait_until() {
  local timeout="${1:-60}"
  local interval="${2:-3}"
  shift 2
  local start now
  start="$(date +%s)"

  while true; do
    if "$@"; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= timeout )); then
      return 1
    fi
    sleep "${interval}"
  done
}

k8s_precheck_enabled() {
  bool_true "${K8S_PRECHECK_ENABLED:-false}"
}

wait_k8s_pods_ready() {
  local timeout="${K8S_PRECHECK_TIMEOUT_SECONDS:-120}"
  local interval="${K8S_PRECHECK_INTERVAL_SECONDS:-3}"
  local ns="${K8S_PRECHECK_NAMESPACE:-default}"
  local selector="${K8S_PRECHECK_LABEL_SELECTOR:-}"

  if ! k8s_precheck_enabled; then
    return 0
  fi

  require_cmd "kubectl"
  require_non_empty "K8S_PRECHECK_LABEL_SELECTOR" "${selector}"

  log "K8s precheck: waiting pods ready (ns=${ns}, selector=${selector}, timeout=${timeout}s)"
  if wait_until "${timeout}" "${interval}" kubectl -n "${ns}" wait --for=condition=Ready pod -l "${selector}" --timeout=5s >/dev/null 2>&1; then
    log "K8s precheck passed"
    return 0
  fi

  err "K8s precheck failed. Pods status:"
  kubectl -n "${ns}" get pods -l "${selector}" -o wide || true
  die "K8s pod readiness precheck failed"
}
