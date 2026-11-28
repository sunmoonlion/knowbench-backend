ARG PYTHON_VERSION=3.12
# Docker 镜像仓库；CI 传 Harbor 地址，本地默认同 Harbor（内网可用）；纯外网传 --build-arg REGISTRY= 退回 DockerHub
ARG REGISTRY=harbor.sunmoonai.com:30443/k8s-images

# ============================================================================
# Stage 1: Type-check — 安装所有依赖（含 dev），运行 pyright 类型检查
# ============================================================================
FROM ${REGISTRY:-docker.io/library}/python:${PYTHON_VERSION}-slim AS type-check

ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG UV_VERSION=0.11.32
ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security
ENV PIP_INDEX_URL=${PYPI_INDEX_URL} \
    UV_DEFAULT_INDEX=${PYPI_INDEX_URL} \
    UV_LINK_MODE=copy

WORKDIR /app

RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "uv==${UV_VERSION}"

COPY app/pyproject.toml app/uv.lock ./
# 安装全量依赖（含 dev），确保 pyright 与业务依赖均可用
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

COPY app/ .

# 运行 lint、format 与 pyright；任一失败则 Docker build 中断
RUN uv run ruff check app core \
    && uv run ruff format --check app core \
    && uv run pyright

# ============================================================================
# Stage 2: Builder — 在 type-check 基础上裁剪 dev 依赖，生成精简 .venv
# 继承 type-check 确保 pyright 必须通过才能进入后续阶段
# ============================================================================
FROM type-check AS builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen

# ============================================================================
# Stage 3: Runtime — 最小化运行镜像
# ============================================================================
ARG PYTHON_VERSION=3.12
ARG REGISTRY=harbor.sunmoonai.com:30443/k8s-images
FROM ${REGISTRY:-docker.io/library}/python:${PYTHON_VERSION}-slim AS run

RUN groupadd --system --gid 1001 appgroup \
    && useradd --system --uid 1001 --gid appgroup \
       --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --chown=appuser:appgroup app/ ./
COPY --from=builder --chown=appuser:appgroup /app/.venv ./.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
