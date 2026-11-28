#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BOOTSTRAP_DIR}/config"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"

PG_CONFIG="${PG_CONFIG:-${CONFIG_DIR}/postgresql.external.env}"
MONGO_CONFIG="${MONGO_CONFIG:-${CONFIG_DIR}/mongodb.external.env}"
REDIS_CONFIG="${REDIS_CONFIG:-${CONFIG_DIR}/redis.external.env}"

log() { printf '[db-provision-template] %s\n' "$*"; }
die() { printf '[db-provision-template][error] %s\n' "$*" >&2; exit 1; }

bool_true() {
  case "${1:-false}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

main() {
  [[ -x "${DBCTL_BIN}" ]] || die "DBCTL_BIN not executable: ${DBCTL_BIN}"

  if bool_true "${ENABLE_POSTGRESQL:-false}"; then
    [[ -f "${PG_CONFIG}" ]] || die "Missing config: ${PG_CONFIG}"
    "${DBCTL_BIN}" --config "${PG_CONFIG}" --target external --action deprovision
  fi
  if bool_true "${ENABLE_MONGODB:-false}"; then
    [[ -f "${MONGO_CONFIG}" ]] || die "Missing config: ${MONGO_CONFIG}"
    "${DBCTL_BIN}" --config "${MONGO_CONFIG}" --target external --action deprovision
  fi
  if bool_true "${ENABLE_REDIS:-false}"; then
    [[ -f "${REDIS_CONFIG}" ]] || die "Missing config: ${REDIS_CONFIG}"
    "${DBCTL_BIN}" --config "${REDIS_CONFIG}" --target external --action deprovision
  fi

  if [[ -n "${OUT_ENV:-}" && -f "${OUT_ENV}" ]]; then
    rm -f "${OUT_ENV}"
    log "Removed env: ${OUT_ENV}"
  fi
  log "Deprovision complete"
}

main "$@"
