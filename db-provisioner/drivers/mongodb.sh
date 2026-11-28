#!/usr/bin/env bash

mongo_validate() {
  require_non_empty "APP_DB_NAME" "${APP_DB_NAME:-}"
  require_non_empty "APP_DB_USER" "${APP_DB_USER:-}"
  require_non_empty "APP_DB_PASSWORD" "${APP_DB_PASSWORD:-}"

  if [[ -z "${MONGO_ADMIN_URI:-}" ]]; then
    require_non_empty "DB_HOST" "${DB_HOST:-}"
    require_non_empty "DB_PORT" "${DB_PORT:-}"
    require_non_empty "MONGO_ADMIN_USER" "${MONGO_ADMIN_USER:-}"
    require_non_empty "MONGO_ADMIN_PASSWORD" "${MONGO_ADMIN_PASSWORD:-}"
  fi
}

mongo_use_k8s_client() {
  [[ "${TARGET:-}" == "k8s" ]] && ! bool_true "${DBCTL_K8S_USE_LOCAL_CLIENT:-false}"
}

mongo_client_namespace() {
  if [[ -n "${MONGO_CLIENT_NAMESPACE:-}" ]]; then
    printf '%s\n' "${MONGO_CLIENT_NAMESPACE}"
  elif [[ "${DB_HOST}" =~ \.([a-z0-9-]+)\.svc(\.|$) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    printf '%s\n' "${K8S_PRECHECK_NAMESPACE:-${NAMESPACE:-default}}"
  fi
}

mongo_client_image() {
  printf '%s\n' "${MONGO_CLIENT_IMAGE:-harbor.sunmoonai.com:30443/k8s-images/mongodb:8.0.13-debian-12-r0}"
}

mongo_run_k8s_client() {
  local pod_name="$1"
  local namespace image timeout pull_policy pull_secret overrides
  namespace="$(mongo_client_namespace)"
  image="$(mongo_client_image)"
  timeout="${MONGO_CLIENT_POD_RUNNING_TIMEOUT:-5m0s}"
  pull_policy="${MONGO_CLIENT_IMAGE_PULL_POLICY:-IfNotPresent}"
  pull_secret="${MONGO_CLIENT_IMAGE_PULL_SECRET:-harbor-registry-secret}"
  overrides="$(printf '{"spec":{"imagePullSecrets":[{"name":"%s"}]}}' "${pull_secret}")"

  require_cmd "kubectl"
  log "[mongo] using temporary MongoDB client pod: ${namespace}/${pod_name} (${image})"
  kubectl run "${pod_name}" --rm -i --restart=Never -n "${namespace}" \
    --image="${image}" \
    --image-pull-policy="${pull_policy}" \
    --overrides="${overrides}" \
    --pod-running-timeout="${timeout}" \
    --command -- bash -se
}

mongo_admin_uri() {
  if [[ -n "${MONGO_ADMIN_URI:-}" ]]; then
    printf '%s' "${MONGO_ADMIN_URI}"
    return
  fi

  local auth_db="${MONGO_AUTH_DB:-admin}"
  local tls_qs=""
  if bool_true "${DB_TLS_ENABLED:-false}"; then
    tls_qs="&tls=true"
  fi
  db_uri mongodb "$MONGO_ADMIN_USER" "$MONGO_ADMIN_PASSWORD" "$DB_HOST" "$DB_PORT" "$auth_db" ""
  printf '%s' "$tls_qs"
}

mongo_provision() {
  if bool_true "${DRY_RUN:-false}"; then
    local admin_uri_dry
    admin_uri_dry="$(mongo_admin_uri)"
    log "Provision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"
    log "DRY_RUN=true, skip executing mongosh"
    log "Admin URI (redacted): $(printf '%s' "${admin_uri_dry}" | sed -E "s#(mongodb://[^:]+:)[^@]+#\\1****#")"
    return 0
  fi

  wait_k8s_pods_ready
  local admin_uri
  admin_uri="$(mongo_admin_uri)"

  log "Provision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"

  local mongo_script
  mongo_script="
const dbName='${APP_DB_NAME}';
const username='${APP_DB_USER}';
const password='${APP_DB_PASSWORD}';
const appDb=db.getSiblingDB(dbName);
const existing=appDb.getUser(username);
if (existing) {
  appDb.updateUser(username, {
    pwd: password,
    roles: [
      { role: 'readWrite', db: dbName },
      { role: 'dbAdmin', db: dbName }
    ]
  });
  print('[mongo] updated existing user: ' + username);
} else {
  appDb.createUser({
    user: username,
    pwd: password,
    roles: [
      { role: 'readWrite', db: dbName },
      { role: 'dbAdmin', db: dbName }
    ]
  });
  print('[mongo] created user: ' + username);
}
try {
  appDb.createCollection('_init_marker');
} catch (e) {}
"

  APP_DB_URI=""
  if mongo_use_k8s_client; then
    local pod_name="dbctl-mongo-provision-${SERVICE_NAME:-app}-$(date +%s)"
    local mongo_script_b64
    mongo_script_b64="$(printf '%s' "${mongo_script}" | base64 -w 0)"
    mongo_run_k8s_client "${pod_name}" <<EOF
mongo_script=\$(printf '%s' '${mongo_script_b64}' | base64 -d)
mongosh '${admin_uri}' --quiet --eval "\${mongo_script}"
EOF
    APP_DB_URI="$(db_uri mongodb "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" "")"
  else
    require_cmd "mongosh"
    mongo_precheck "${admin_uri}"
    mongosh "${admin_uri}" --quiet --eval "${mongo_script}"
    APP_DB_URI="$(db_uri mongodb "$APP_DB_USER" "$APP_DB_PASSWORD" "$DB_HOST" "$DB_PORT" "$APP_DB_NAME" "")"
  fi

  require_non_empty "APP_DB_URI(mongo)" "${APP_DB_URI}"
}

mongo_deprovision() {
  if bool_true "${DRY_RUN:-false}"; then
    local admin_uri_dry
    admin_uri_dry="$(mongo_admin_uri)"
    log "Deprovision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"
    log "DRY_RUN=true, skip executing mongosh"
    log "Admin URI (redacted): $(printf '%s' "${admin_uri_dry}" | sed -E "s#(mongodb://[^:]+:)[^@]+#\\1****#")"
    APP_DB_URI=""
    return 0
  fi

  wait_k8s_pods_ready
  local admin_uri
  admin_uri="$(mongo_admin_uri)"

  log "Deprovision MongoDB: db=${APP_DB_NAME}, user=${APP_DB_USER}"
  local mongo_script
  mongo_script="
const dbName='${APP_DB_NAME}';
const username='${APP_DB_USER}';
const dropDb='${DEPROVISION_DROP_DATABASE:-false}' === 'true';
const appDb=db.getSiblingDB(dbName);
const existing=appDb.getUser(username);
if (existing) {
  appDb.dropUser(username);
  print('[mongo] dropped user: ' + username);
} else {
  print('[mongo] user not found, skip: ' + username);
}
if (dropDb) {
  appDb.dropDatabase();
  print('[mongo] dropped database: ' + dbName);
}
"
  if mongo_use_k8s_client; then
    local pod_name="dbctl-mongo-deprovision-${SERVICE_NAME:-app}-$(date +%s)"
    local mongo_script_b64
    mongo_script_b64="$(printf '%s' "${mongo_script}" | base64 -w 0)"
    mongo_run_k8s_client "${pod_name}" <<EOF
mongo_script=\$(printf '%s' '${mongo_script_b64}' | base64 -d)
mongosh '${admin_uri}' --quiet --eval "\${mongo_script}"
EOF
  else
    require_cmd "mongosh"
    mongo_precheck "${admin_uri}"
    mongosh "${admin_uri}" --quiet --eval "${mongo_script}"
  fi
  APP_DB_URI=""
}

mongo_precheck() {
  local admin_uri="$1"
  local timeout="${DB_PRECHECK_TIMEOUT_SECONDS:-60}"
  local interval="${DB_PRECHECK_INTERVAL_SECONDS:-3}"

  if ! precheck_enabled; then
    log "MongoDB precheck disabled"
    return 0
  fi

  log "MongoDB precheck: waiting for readiness (timeout=${timeout}s, interval=${interval}s)"
  if wait_until "${timeout}" "${interval}" mongosh "${admin_uri}" --quiet --eval "db.adminCommand({ ping: 1 }).ok" >/dev/null 2>&1; then
    log "MongoDB precheck passed"
    return 0
  fi

  die "MongoDB precheck failed: service not ready or unreachable"
}
