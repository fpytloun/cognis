# Cognis development and e2e test targets
#
# E2E test workflow:
#   make e2e-up      # Start the deterministic e2e stack
#   make e2e-seed    # Seed e2e agent + scenarios
#   make e2e-events  # Run L2 golden event-stream tests (fast, no browser)
#   make e2e-browser # Run L3 Playwright browser tests
#   make e2e-down    # Stop the e2e stack
#
# Interactive debugging workflow:
#   make e2e-up && make e2e-seed
#   # Attach Playwright MCP to http://localhost:8080
#   # Use POST http://localhost:8090/__mock/active to inject scenarios
#   # Edit code, run: make e2e-ui-build (or make e2e-ui-dev for hot-reload)
#   make e2e-down

# Use docker-compose (v1 standalone) if available, otherwise docker compose (v2 plugin)
DOCKER_COMPOSE := $(shell which docker-compose 2>/dev/null || echo "docker compose")
COMPOSE_BASE := $(DOCKER_COMPOSE) -f compose.local.yml
COMPOSE_E2E  := $(COMPOSE_BASE) -f compose.e2e.yml
HA_E2E_PROJECT ?= cognis-ha-e2e
COMPOSE_HA_E2E := $(DOCKER_COMPOSE) --project-name $(HA_E2E_PROJECT) --env-file .local/cognis-ha-e2e/current/compose.env -f compose.local.yml -f compose.e2e.yml -f compose.ha-e2e.yml
COMPOSE_REDIS_HA_E2E := $(COMPOSE_HA_E2E) -f compose.redis-ha-e2e.yml
PYTHON ?= python3

# ---------------------------------------------------------------------------
# E2E stack lifecycle
# ---------------------------------------------------------------------------

.PHONY: e2e-up
e2e-up:
	$(COMPOSE_E2E) up -d --build
	@echo ""
	@echo "E2E stack started:"
	@echo "  Cognis:   http://localhost:8080"
	@echo "  Mock LLM: http://localhost:8090"
	@echo "  Mock LLM control plane: http://localhost:8090/__mock/scenarios"
	@echo ""
	@echo "Next: make e2e-seed"

.PHONY: e2e-seed
e2e-seed:
	COGNIS_LOCAL_HOST_TOKEN_UID=$$(id -u) COGNIS_LOCAL_HOST_TOKEN_GID=$$(id -g) $(COMPOSE_E2E) run --rm seed-e2e
	$(COMPOSE_E2E) up -d --force-recreate --no-deps cognis-executor
	@EMAIL=$${COGNIS_LOCAL_ADMIN_EMAIL:-admin@cognis-e2e.localdev.me}; \
	PASSWORD=$${COGNIS_LOCAL_ADMIN_PASSWORD:-cognis-local-admin}; \
	READY=0; \
	for i in $$(seq 1 24); do \
		TOKEN=$$(curl -s -X POST http://localhost:8080/api/auth/login -H 'Content-Type: application/json' --data "{\"email\":\"$$EMAIL\",\"password\":\"$$PASSWORD\"}" | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null || true); \
		STATE=$$(curl -s -H "Authorization: Bearer $$TOKEN" http://localhost:8080/api/v1/executors | $(PYTHON) -c 'import json,sys; data=json.load(sys.stdin); print(next((e.get("runtime_state") for e in data if e.get("executor_id")=="local-compose-executor"), "missing"))' 2>/dev/null || true); \
		echo "E2E executor state: $$STATE"; \
		if [ "$$STATE" = "active" ]; then READY=1; break; fi; \
		sleep 5; \
	done; \
	if [ "$$READY" != "1" ]; then \
		echo "Timed out waiting for local-compose-executor to become active"; \
		exit 1; \
	fi
	$(PYTHON) scripts/wait_e2e_chat_ready.py
	@echo "E2E environment seeded."

.PHONY: e2e-down
e2e-down:
	$(COMPOSE_E2E) down

.PHONY: e2e-logs
e2e-logs:
	$(COMPOSE_E2E) logs -f

.PHONY: e2e-status
e2e-status:
	$(COMPOSE_E2E) ps

# ---------------------------------------------------------------------------
# L2: Canonical Chat v2 event-stream tests (fast, no browser)
# ---------------------------------------------------------------------------

.PHONY: e2e-events
e2e-events: e2e-events-capture e2e-events-replay

.PHONY: e2e-events-capture
e2e-events-capture:
	@echo "Capturing golden event streams from live stack..."
	uv run pytest tests/e2e/ -m e2e -v --tb=short

.PHONY: e2e-events-replay
e2e-events-replay:
	@echo "Replaying every promoted canonical Chat v2 capture through the production sync engine..."
	cd ui && npm test -- --reporter=verbose --run \
		src/lib/chat-v2/capture-replay.invariants.test.ts \
		src/lib/chat-v2/sync-engine.invariants.test.ts

# ---------------------------------------------------------------------------
# L3: Playwright browser tests
# ---------------------------------------------------------------------------

.PHONY: e2e-browser
e2e-browser:
	cd ui && npx playwright test e2e/ --workers=1

.PHONY: e2e-browser-ui
e2e-browser-ui:
	cd ui && npx playwright test e2e/ --ui

# ---------------------------------------------------------------------------
# UI iteration during interactive debugging
# ---------------------------------------------------------------------------

.PHONY: e2e-ui-build
e2e-ui-build:
	@echo "Building UI (served by Cognis)..."
	cd ui && npm run build
	@echo "UI built. Cognis serves the updated assets at http://localhost:8080"

.PHONY: e2e-ui-dev
e2e-ui-dev:
	@echo "Starting Vite dev server (hot-reload, proxies /api to localhost:8080)..."
	cd ui && npm run dev -- --port 5173

# ---------------------------------------------------------------------------
# Promote an interactively-reproduced scenario to a static test
# ---------------------------------------------------------------------------

.PHONY: e2e-promote
e2e-promote:
	@if [ -z "$(SCENARIO)" ]; then \
		echo "Usage: make e2e-promote SCENARIO=my-scenario-name"; \
		exit 1; \
	fi
	python scripts/promote_e2e_scenario.py $(SCENARIO)

# ---------------------------------------------------------------------------
# Regular test targets
# ---------------------------------------------------------------------------

.PHONY: test
test:
	uv run pytest tests/unit/ -v

.PHONY: test-ui
test-ui:
	cd ui && npm test

.PHONY: lint
lint:
	uv run ruff check cognis/ tests/
	uv run ruff format --check cognis/ tests/

.PHONY: deployment-validate
deployment-validate:
	./scripts/validate_deployment.sh

.PHONY: ha-e2e-prepare
ha-e2e-prepare:
	uv run python scripts/prepare_ha_e2e.py

.PHONY: ha-e2e-up
ha-e2e-up: ha-e2e-prepare
	$(COMPOSE_HA_E2E) up -d --build

.PHONY: ha-e2e-down
ha-e2e-down:
	$(COMPOSE_HA_E2E) down

.PHONY: redis-ha-e2e-up
redis-ha-e2e-up: ha-e2e-prepare
	$(COMPOSE_REDIS_HA_E2E) up -d --build

.PHONY: redis-ha-e2e-down
redis-ha-e2e-down:
	$(COMPOSE_REDIS_HA_E2E) down

.PHONY: redis-ha-e2e-clean
redis-ha-e2e-clean:
	$(COMPOSE_REDIS_HA_E2E) down --volumes --remove-orphans

.PHONY: redis-ha-e2e-config
redis-ha-e2e-config: ha-e2e-prepare
	$(COMPOSE_REDIS_HA_E2E) config

.PHONY: redis-ha-e2e-qualify
redis-ha-e2e-qualify:
	COGNIS_REDIS_HA_E2E_URL=http://localhost:$${COGNIS_HA_E2E_PORT:-18080} \
		uv run pytest tests/e2e/test_redis_ha_live.py -m e2e -v --tb=short

.PHONY: ha-e2e-qualify
ha-e2e-qualify:
	./scripts/qualify_ha_e2e.sh

.PHONY: typecheck
typecheck:
	uv run mypy cognis/
	cd ui && npm run check
