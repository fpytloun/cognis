FROM node:20-slim AS ui-build
WORKDIR /src/ui

COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ ./
COPY docs/ ../docs/
RUN npm run build && npm run build:standalone


FROM python:3.12-slim AS common-wheel
WORKDIR /src/packages/common

COPY packages/common/ ./
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist


FROM python:3.12-slim AS controller-wheel
WORKDIR /src

COPY pyproject.toml README.md LICENSE build.py ./
COPY cognis/ ./cognis/
COPY --from=ui-build /src/ui/build ./ui/build
COPY --from=ui-build /src/ui/standalone-build ./ui/standalone-build

ENV COGNIS_SKIP_UI_BUILD=1
RUN pip install --no-cache-dir build hatchling \
    && pyproject-build --wheel --sdist --no-isolation --outdir /dist


FROM python:3.12-slim AS runtime
WORKDIR /app

COPY scripts/ ./scripts/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libcairo2 \
        libffi8 \
        libgdk-pixbuf-2.0-0 \
        libpango-1.0-0 \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1

RUN --mount=type=bind,from=common-wheel,source=/dist,target=/tmp/common-wheels \
    --mount=type=bind,from=controller-wheel,source=/dist,target=/tmp/controller-wheels \
    controller_wheel="$(find /tmp/controller-wheels -maxdepth 1 -name 'cognis_controller-*.whl' -print -quit)" \
    && test -n "$controller_wheel" \
    && pip install --no-cache-dir \
        /tmp/common-wheels/cognis_common-*.whl \
        "${controller_wheel}[postgres,s3,redis,knowledgebase]" \
    && python -c "import asyncpg, boto3, redis, qdrant_client" \
    && rm -rf /opt/venv/lib/python3.12/site-packages/pip* \
    && find /opt/venv -type d -name __pycache__ -prune -exec rm -rf '{}' +

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]

CMD ["cognis-controller", "serve"]
