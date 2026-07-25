FROM node:20-slim AS ui-build
WORKDIR /app/ui

COPY ui/package.json ui/package-lock.json* ./
RUN npm ci

COPY ui/ ./
COPY docs/ ../docs/
RUN npm run build
RUN npm run build:standalone


FROM python:3.12-slim AS runtime
WORKDIR /app

ENV COGNIS_SKIP_UI_BUILD=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md build.py ./
COPY cognis/ ./cognis/
COPY docs/ ./docs/
COPY scripts/ ./scripts/
COPY ui/ ./ui/
COPY --from=ui-build /app/ui/build ./ui/build
COPY --from=ui-build /app/ui/standalone-build ./ui/standalone-build

ENV UV_PROJECT_ENVIRONMENT=/usr/local

RUN pip install --no-cache-dir uv \
    && uv sync --locked --no-dev --extra postgres --extra s3 --extra redis --extra knowledgebase

EXPOSE 8080

HEALTHCHECK CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["cognis-controller", "serve"]
