FROM node:20-slim AS ui-build
WORKDIR /app/ui

COPY ui/package.json ui/package-lock.json* ./
RUN npm ci

COPY ui/ ./
COPY docs/ ../docs/
RUN npm run build


FROM python:3.12-slim AS runtime
WORKDIR /app

ENV COGNIS_SKIP_UI_BUILD=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md build.py ./
COPY cognis/ ./cognis/
COPY docs/ ./docs/
COPY ui/ ./ui/
COPY --from=ui-build /app/ui/build ./ui/build

RUN pip install --no-cache-dir ".[postgres,s3,redis,knowledgebase]"

EXPOSE 8080

HEALTHCHECK CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["cognis-controller", "serve"]
