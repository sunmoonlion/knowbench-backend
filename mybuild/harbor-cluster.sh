#!/bin/bash

# Harbor 地址解析：与 k8s/sunmoonai 平台一致（Traefik websecure → NodePort 30443）。
# WSL 上 docker pull/push 与 build 拉基础镜像、推 app-images 均须带端口，C1/KIND 相同。

_harbor_external_config_loaded=false

load_harbor_external_config() {
    if [[ "$_harbor_external_config_loaded" == "true" ]]; then
        return 0
    fi
    local config_files=(
        "$HOME/k8s/sunmoonai/cicd-platform/harbor/deploy-harbor/deploy-harbor.conf"
        "$HOME/k8s/sunmoonai/deploy-sunmoonai-all/deploy-sunmoonai-all.conf"
    )
    local file
    for file in "${config_files[@]}"; do
        [[ -f "$file" ]] || continue
        # shellcheck source=/dev/null
        source "$file" 2>/dev/null || true
    done
    HARBOR_EXTERNAL_HOST="${HARBOR_EXTERNAL_HOST:-harbor.sunmoonai.com}"
    HARBOR_EXTERNAL_PORT="${HARBOR_EXTERNAL_PORT:-30443}"
    SUNMOONAI_HARBOR_REGISTRY="${HARBOR_EXTERNAL_HOST}:${HARBOR_EXTERNAL_PORT}"
    _harbor_external_config_loaded=true
}

normalize_harbor_cluster() {
    local cluster="${1:-}"
    cluster="$(printf '%s' "$cluster" | tr '[:lower:]' '[:upper:]')"
    if [[ "$cluster" =~ ^[0-9]+$ ]]; then
        cluster="C${cluster}"
    fi
    printf '%s\n' "$cluster"
}

# 将 harbor.sunmoonai.com 或 host:port 规范为 host:port（默认端口来自 deploy-harbor.conf）
normalize_registry_host_port() {
    local reg="${1:-}"
    load_harbor_external_config
    reg="${reg%/}"
    if [[ -z "$reg" ]]; then
        printf '%s\n' "$SUNMOONAI_HARBOR_REGISTRY"
        return 0
    fi
    if [[ "$reg" =~ :[0-9]+$ ]]; then
        printf '%s\n' "$reg"
        return 0
    fi
    printf '%s:%s\n' "$reg" "$HARBOR_EXTERNAL_PORT"
}

prompt_harbor_cluster() {
    local cluster=""
    load_harbor_external_config
    echo "请选择镜像推送目标集群：" >&2
    echo "  C1/C2/C3 : 远程 Harbor (${SUNMOONAI_HARBOR_REGISTRY})" >&2
    echo "  KIND     : 本地 Kind Harbor (${SUNMOONAI_HARBOR_REGISTRY})" >&2
    printf "输入集群 [C1/C2/C3/KIND，默认 C1]: " >&2
    read -r cluster
    cluster="${cluster:-C1}"
    normalize_harbor_cluster "$cluster"
}

# 解析 push/pull 使用的 registry host:port（不含项目路径）
resolve_harbor_registry_for_push() {
    local configured_registry="${1:-}"
    local cluster="${CLUSTER:-}"
    local cluster_override=""
    local cluster_var

    load_harbor_external_config

    if [[ -z "$cluster" ]]; then
        if [[ -t 0 ]]; then
            cluster="$(prompt_harbor_cluster)"
        else
            echo "CLUSTER 未设置，非交互模式下无法选择 Harbor；请设置 CLUSTER=KIND 或 CLUSTER=C1" >&2
            return 1
        fi
    else
        cluster="$(normalize_harbor_cluster "$cluster")"
    fi

    export CLUSTER="$cluster"

    case "$cluster" in
        KIND)
            cluster_override="${KIND_HARBOR_REGISTRY:-}"
            ;;
        C[0-9]*)
            cluster_override="${REMOTE_HARBOR_REGISTRY:-}"
            ;;
        *)
            echo "无效 CLUSTER: $cluster，应为 KIND 或 C1/C2/C3" >&2
            return 1
            ;;
    esac

    if [[ -n "$cluster_override" ]]; then
        normalize_registry_host_port "$cluster_override"
        return 0
    fi
    if [[ -n "$configured_registry" ]]; then
        normalize_registry_host_port "$configured_registry"
        return 0
    fi
    printf '%s\n' "$SUNMOONAI_HARBOR_REGISTRY"
}

# 拉取 Dockerfile 基础镜像：k8s-images 项目（python/node/nginx 等）
resolve_k8s_images_registry() {
    local host_port
    host_port="$(resolve_harbor_registry_for_push "")" || return 1
    printf '%s/k8s-images' "$host_port"
}

load_harbor_credentials_for_push() {
    local config_files=(
        "$HOME/k8s/sunmoonai/deploy-sunmoonai-all/deploy-sunmoonai-all.conf"
        "$HOME/k8s/sunmoonai/kind-infrastructure/deploy-kind/deploy-kind.conf"
        "$HOME/k8s/sunmoonai/cicd-platform/harbor/deploy-harbor/secrets/harbor-secret/deploy-harbor-secret/deploy-harbor-secret.conf"
        "$HOME/k8s/sunmoonai/cicd-platform/harbor/utils/harbor-image-management/harbor-image.conf"
    )

    local file
    for file in "${config_files[@]}"; do
        [[ -f "$file" ]] || continue
        # shellcheck source=/dev/null
        source "$file" 2>/dev/null || true
    done

    local user="${HARBOR_USER:-${HARBOR_USERNAME:-${HARBOR_ADMIN_USERNAME:-${HARBOR_ADMIN_USER:-admin}}}}"
    local pass="${HARBOR_PASSWORD:-${HARBOR_ADMIN_PASSWORD:-}}"

    if [[ -z "$user" ]]; then
        read -rp "Harbor 用户名: " user
    fi
    if [[ -z "$pass" ]]; then
        read -rsp "Harbor 密码: " pass
        echo
    fi

    HARBOR_USER="$user"
    HARBOR_PASSWORD="$pass"
    export HARBOR_USER HARBOR_PASSWORD
}

push_image_with_harbor_verify() {
    local runtime_cmd="$1"
    local full_image="$2"
    local retry_count="${PUSH_RETRY_COUNT:-2}"
    local retry_delay="${PUSH_RETRY_DELAY:-3}"
    local attempt

    for ((attempt=1; attempt<=retry_count; attempt++)); do
        if $runtime_cmd push "$full_image"; then
            return 0
        fi

        if (( attempt < retry_count )); then
            echo "推送失败，${retry_delay}s 后重试 (${attempt}/${retry_count}): $full_image" >&2
            sleep "$retry_delay"
        fi
    done

    return 1
}
