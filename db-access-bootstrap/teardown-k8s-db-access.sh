#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BOOTSTRAP_DIR}/config"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"

PG_CONFIG="${PG_K8S_CONFIG:-${CONFIG_DIR}/postgresql.k8s.env}"
REDIS_CONFIG="${REDIS_K8S_CONFIG:-${CONFIG_DIR}/redis.k8s.env}"
MONGO_CONFIG="${MONGO_K8S_CONFIG:-${CONFIG_DIR}/mongodb.k8s.env}"

log() { printf '[db-access-bootstrap][k8s] %s\n' "$*"; }
die() { printf '[db-access-bootstrap][k8s][error] %s\n' "$*" >&2; exit 1; }

bool_true() {
  case "${1:-false}" in 1|true|TRUE|yes|YES|on|ON) return 0 ;; *) return 1 ;; esac
}

main() {
  [[ -x "${DBCTL_BIN}" ]] || die "DBCTL_BIN not executable: ${DBCTL_BIN}"
  command -v kubectl >/dev/null 2>&1 || die "Missing kubectl"

  if bool_true "${ENABLE_POSTGRESQL:-false}"; then
    [[ -f "${PG_CONFIG}" ]] || die "Missing config: ${PG_CONFIG}"
    "${DBCTL_BIN}" --config "${PG_CONFIG}" --target k8s --action deprovision
  fi
  if bool_true "${ENABLE_MONGODB:-false}"; then
    [[ -f "${MONGO_CONFIG}" ]] || die "Missing config: ${MONGO_CONFIG}"
    "${DBCTL_BIN}" --config "${MONGO_CONFIG}" --target k8s --action deprovision
  fi
  if bool_true "${ENABLE_REDIS:-false}"; then
    [[ -f "${REDIS_CONFIG}" ]] || die "Missing config: ${REDIS_CONFIG}"
    "${DBCTL_BIN}" --config "${REDIS_CONFIG}" --target k8s --action deprovision
  fi
  rm -f -- "${K8S_OUT_ENV:-${BOOTSTRAP_DIR}/.env.local.k8s.db}"
  log "Done. k8s secrets removed (and users deprovisioned where supported)."
}

main "$@"
