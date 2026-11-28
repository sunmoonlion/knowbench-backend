#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$BOOTSTRAP_DIR/config"
source "$CONFIG_DIR/common.env"

bool_true() {
  case "${1:-false}" in 1|true|TRUE|yes|YES|on|ON) return 0 ;; *) return 1 ;; esac
}

setup_cluster() {
  local k8s_root="${SUNMOONAI_K8S_ROOT:-${HOME}/k8s}"
  source "$k8s_root/utils/unified-deployment-template.sh"
  setup_kubectl_environment
}

run_enabled() {
  local action="$1"
  local dry_run="${2:-false}"
  local args=()
  [[ "$dry_run" == "true" ]] && args+=(--dry-run)
  if bool_true "${ENABLE_POSTGRESQL:-false}"; then
    "$DBCTL_BIN" --config "$PG_K8S_CONFIG" --target k8s --action "$action" "${args[@]}"
  fi
  if bool_true "${ENABLE_MONGODB:-false}"; then
    "$DBCTL_BIN" --config "$MONGO_K8S_CONFIG" --target k8s --action "$action" "${args[@]}"
  fi
  if bool_true "${ENABLE_REDIS:-false}"; then
    "$DBCTL_BIN" --config "$REDIS_K8S_CONFIG" --target k8s --action "$action" "${args[@]}"
  fi
}

check_secret() {
  local config="$1"
  local namespace secret
  namespace="$(sed -n 's/^OUTPUT_NAMESPACE=//p' "$config")"
  secret="$(sed -n 's/^OUTPUT_SECRET_NAME=//p' "$config")"
  kubectl get secret "$secret" -n "$namespace" >/dev/null
}

case "${1:-}" in
  validate) run_enabled provision true ;;
  provision) setup_cluster; "$BOOTSTRAP_DIR/setup-k8s-db-access.sh" ;;
  status)
    setup_cluster
    if bool_true "${ENABLE_POSTGRESQL:-false}"; then check_secret "$PG_K8S_CONFIG"; fi
    if bool_true "${ENABLE_MONGODB:-false}"; then check_secret "$MONGO_K8S_CONFIG"; fi
    if bool_true "${ENABLE_REDIS:-false}"; then check_secret "$REDIS_K8S_CONFIG"; fi
    ;;
  *) echo "Usage: $0 <validate|provision|status>" >&2; exit 1 ;;
esac
