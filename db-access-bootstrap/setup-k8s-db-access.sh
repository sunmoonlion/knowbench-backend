#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BOOTSTRAP_DIR}/config"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"

PG_CONFIG="${PG_K8S_CONFIG:-${CONFIG_DIR}/postgresql.k8s.env}"
REDIS_CONFIG="${REDIS_K8S_CONFIG:-${CONFIG_DIR}/redis.k8s.env}"
MONGO_CONFIG="${MONGO_K8S_CONFIG:-${CONFIG_DIR}/mongodb.k8s.env}"

OUT="${K8S_OUT_ENV:-${BOOTSTRAP_DIR}/.env.local.k8s.db}"

log() { printf '[db-access-bootstrap][k8s] %s\n' "$*"; }
die() { printf '[db-access-bootstrap][k8s][error] %s\n' "$*" >&2; exit 1; }

bool_true() {
  case "${1:-false}" in 1|true|TRUE|yes|YES|on|ON) return 0 ;; *) return 1 ;; esac
}

require_file() { [[ -f "$1" ]] || die "Missing file: $1"; }

source "${BOOTSTRAP_DIR}/env-values.sh"

write_k8s_out() {
  printf '# k8s 场景连接串（内部 Service DNS），由 setup-k8s-db-access.sh 生成。\n' > "${OUT}"
  printf '# 仅在 Pod 内部可用，不可直接用于本机开发。\n' >> "${OUT}"
  printf 'APP_ENV=production\n' >> "${OUT}"

  if bool_true "${ENABLE_POSTGRESQL:-false}" && [[ -f "${PG_CONFIG}" ]]; then
    # shellcheck disable=SC1090
    source "${PG_CONFIG}"
    replace_env_value "${OUT}" "DATABASE_URL" \
      "$(db_uri postgresql "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')"
  fi

  if bool_true "${ENABLE_REDIS:-false}" && [[ -f "${REDIS_CONFIG}" ]]; then
    # shellcheck disable=SC1090
    source "${REDIS_CONFIG}"
    replace_env_value "${OUT}" "REDIS_HOST"     "${DB_HOST}"
    replace_env_value "${OUT}" "REDIS_PORT"     "${DB_PORT}"
    replace_env_value "${OUT}" "REDIS_DB"       "${REDIS_DB_INDEX}"
    replace_env_value "${OUT}" "REDIS_USER"     "${APP_DB_USER}"
    replace_env_value "${OUT}" "REDIS_PASSWORD" "${APP_DB_PASSWORD}"
  fi

  if bool_true "${ENABLE_MONGODB:-false}" && [[ -f "${MONGO_CONFIG}" ]]; then
    # shellcheck disable=SC1090
    source "${MONGO_CONFIG}"
    replace_env_value "${OUT}" "MONGODB_URI" \
      "$(db_uri mongodb "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')"
  fi


}

main() {
  [[ -x "${DBCTL_BIN}" ]] || die "DBCTL_BIN not executable: ${DBCTL_BIN}"
  command -v kubectl >/dev/null 2>&1 || die "Missing kubectl"

  bool_true "${ENABLE_POSTGRESQL:-false}" && require_file "${PG_CONFIG}" && "${DBCTL_BIN}" --config "${PG_CONFIG}" --target k8s --action provision
  bool_true "${ENABLE_MONGODB:-false}" && require_file "${MONGO_CONFIG}" && "${DBCTL_BIN}" --config "${MONGO_CONFIG}" --target k8s --action provision
  bool_true "${ENABLE_REDIS:-false}" && require_file "${REDIS_CONFIG}" && "${DBCTL_BIN}" --config "${REDIS_CONFIG}" --target k8s --action provision

  local destination="$OUT"
  umask 077
  OUT="$(mktemp -- "${destination}.tmp.XXXXXX")"
  trap 'rm -f -- "$OUT"' EXIT
  write_k8s_out
  mv -fT -- "$OUT" "$destination"
  trap - EXIT

  log "k8s env written: $destination"
  log "Done. k8s secrets applied in namespace: ${NAMESPACE}"
}

main "$@"
