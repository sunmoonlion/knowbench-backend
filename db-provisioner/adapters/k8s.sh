#!/usr/bin/env bash

k8s_write_output() {
  require_cmd "kubectl"

  local ns="${OUTPUT_NAMESPACE:-default}"
  local secret_name="${OUTPUT_SECRET_NAME:-${SERVICE_NAME:-service}-${DB_ENGINE}-conn}"

  if [[ "${ACTION:-provision}" == "deprovision" ]]; then
    if bool_true "${DRY_RUN:-false}"; then
      log "DRY_RUN=true, will print kubectl delete secret command only"
      log "kubectl -n ${ns} delete secret ${secret_name} --ignore-not-found"
      return 0
    fi
    kubectl -n "${ns}" delete secret "${secret_name}" --ignore-not-found >/dev/null || return 1
    log "Deleted k8s secret if exists: ${ns}/${secret_name}"
    return 0
  fi

  if bool_true "${DRY_RUN:-false}"; then
    log "DRY_RUN=true, will print kubectl apply command only"
    log "kubectl -n ${ns} create secret generic ${secret_name} --from-literal=... --dry-run=client -o yaml | kubectl apply -f -"
    return 0
  fi

  local -a secret_args=(
    --from-literal=SERVICE_NAME="${SERVICE_NAME:-}"
    --from-literal=ENVIRONMENT="${ENVIRONMENT:-}"
    --from-literal=DB_ENGINE="${DB_ENGINE}"
    --from-literal=DB_HOST="${DB_HOST:-}"
    --from-literal=DB_PORT="${DB_PORT:-}"
    --from-literal=APP_DB_NAME="${APP_DB_NAME:-}"
    --from-literal=APP_DB_USER="${APP_DB_USER:-}"
    --from-literal=APP_DB_PASSWORD="${APP_DB_PASSWORD:-}"
    --from-literal=APP_DB_URI="${APP_DB_URI:-}"
  )

  case "${DB_ENGINE}" in
    postgresql)
      secret_args+=(--from-literal=DATABASE_URL="${APP_DB_URI:-}")
      ;;
    mongodb)
      secret_args+=(--from-literal=MONGODB_URI="${APP_DB_URI:-}")
      ;;
    redis)
      secret_args+=(
        --from-literal=REDIS_HOST="${DB_HOST:-}"
        --from-literal=REDIS_PORT="${DB_PORT:-}"
        --from-literal=REDIS_DB="${REDIS_DB_INDEX:-${APP_DB_NAME:-0}}"
        --from-literal=REDIS_USER="${APP_DB_USER:-}"
        --from-literal=REDIS_PASSWORD="${APP_DB_PASSWORD:-}"
        --from-literal=REDIS_URI="${APP_DB_URI:-}"
      )
      ;;
  esac

  kubectl -n "${ns}" create secret generic "${secret_name}" \
    "${secret_args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f -

  log "Applied k8s secret: ${ns}/${secret_name}"
}
