#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${BOOTSTRAP_DIR}/config"

# shellcheck disable=SC1091
source "${CONFIG_DIR}/common.env"

PG_CONFIG="${PG_CONFIG:-${CONFIG_DIR}/postgresql.external.env}"
REDIS_CONFIG="${REDIS_CONFIG:-${CONFIG_DIR}/redis.external.env}"
MONGO_CONFIG="${MONGO_CONFIG:-${CONFIG_DIR}/mongodb.external.env}"

log() { printf '[db-provision-template] %s\n' "$*"; }
die() { printf '[db-provision-template][error] %s\n' "$*" >&2; exit 1; }

bool_true() {
  case "${1:-false}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

require_file() { [[ -f "$1" ]] || die "Missing file: $1"; }

source "${BOOTSTRAP_DIR}/env-values.sh"

run_postgresql() {
  require_file "${PG_CONFIG}"
  "${DBCTL_BIN}" --config "${PG_CONFIG}" --target external --action provision
  # shellcheck disable=SC1090
  source "${PG_CONFIG}"
  export PGPASSWORD="${APP_DB_PASSWORD}"
  psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${APP_DB_USER}" -d "${APP_DB_NAME}" -c "select current_user, current_database();" >/dev/null
  replace_env_value "${OUT_ENV}" "DATABASE_URL" "$(db_uri postgresql "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')"
  # Create required extensions as admin (app user lacks SUPERUSER privilege)
  export PGPASSWORD="${PG_ADMIN_PASSWORD}"
  psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${PG_ADMIN_USER}" -d "${APP_DB_NAME}" \
    -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";' >/dev/null
}

run_mongodb() {
  require_file "${MONGO_CONFIG}"
  "${DBCTL_BIN}" --config "${MONGO_CONFIG}" --target external --action provision
  # shellcheck disable=SC1090
  source "${MONGO_CONFIG}"
  if command -v mongosh >/dev/null 2>&1; then
    mongosh "$(db_uri mongodb "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')" --quiet --eval "db.adminCommand({ ping: 1 }).ok" >/dev/null
  fi
  replace_env_value "${OUT_ENV}" "MONGODB_URI" "$(db_uri mongodb "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" '')"
}

run_redis() {
  require_file "${REDIS_CONFIG}"
  "${DBCTL_BIN}" --config "${REDIS_CONFIG}" --target external --action provision
  # shellcheck disable=SC1090
  source "${REDIS_CONFIG}"
  replace_env_value "${OUT_ENV}" "REDIS_HOST" "${DB_HOST}"
  replace_env_value "${OUT_ENV}" "REDIS_PORT" "${DB_PORT}"
  replace_env_value "${OUT_ENV}" "REDIS_DB" "${REDIS_DB_INDEX}"
  replace_env_value "${OUT_ENV}" "REDIS_USER" "${APP_DB_USER}"
  replace_env_value "${OUT_ENV}" "REDIS_PASSWORD" "${APP_DB_PASSWORD}"
  replace_env_value "${OUT_ENV}" "REDIS_URI" "$(db_uri redis "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$REDIS_DB_INDEX" '')"
}

main() {
  [[ -x "${DBCTL_BIN}" ]] || die "DBCTL_BIN not executable: ${DBCTL_BIN}"
  bool_true "${STRICT_FILE_CHECK:-true}" && require_file "${BASE_ENV_TEMPLATE}"
  local destination="${OUT_ENV}"
  umask 077
  OUT_ENV="$(mktemp -- "${destination}.tmp.XXXXXX")"
  trap 'rm -f -- "${OUT_ENV}"' EXIT
  cat -- "${BASE_ENV_TEMPLATE}" > "${OUT_ENV}"
  if bool_true "${ENABLE_POSTGRESQL:-false}"; then run_postgresql; fi
  if bool_true "${ENABLE_MONGODB:-false}"; then run_mongodb; fi
  if bool_true "${ENABLE_REDIS:-false}"; then run_redis; fi
  mv -fT -- "${OUT_ENV}" "${destination}"
  trap - EXIT
  log "Provision complete. env ready: ${destination}"
}

main "$@"
