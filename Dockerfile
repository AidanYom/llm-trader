# syntax=docker/dockerfile:1

# uv is pinned here and in .github/workflows/ci.yml; bump both together.
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

# ---- dev: tooling image for docker compose, which bind-mounts the repo over /app ----
FROM python:3.12-slim AS dev
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
# Dependencies only, so this layer is reused until pyproject.toml or uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-install-project
COPY . .
# Then the project itself, installed editable so the bind-mounted src/ is what runs.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# ---- lambda: production image, built with --platform linux/amd64 ----
FROM public.ecr.aws/lambda/python:3.12 AS lambda-deps
ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_INSTALLER_METADATA=1 \
    UV_LINK_MODE=copy
WORKDIR /build
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv export --frozen --no-dev --no-emit-workspace --no-editable -o requirements.txt && \
    uv pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

FROM public.ecr.aws/lambda/python:3.12 AS lambda
COPY --from=lambda-deps ${LAMBDA_TASK_ROOT} ${LAMBDA_TASK_ROOT}
COPY src/trader ${LAMBDA_TASK_ROOT}/trader
COPY config/ ${LAMBDA_TASK_ROOT}/config/
CMD ["trader.lambda_handler.handler"]
