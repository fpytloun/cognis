.PHONY: dev serve test lint format typecheck build clean ui ui-dev help

PYTHON ?= uv run python
COGNIS ?= uv run cognis

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Development setup
# ---------------------------------------------------------------------------

dev: ## Install Python + UI deps, build UI, stage assets
	uv pip install -e ".[dev]"
	cd ui && npm ci
	$(MAKE) ui

# ---------------------------------------------------------------------------
# UI build
# ---------------------------------------------------------------------------

ui: ## Build SvelteKit UI and stage into cognis/ui_dist
	cd ui && npm run build
	@# Stage built assets so the Python server can serve them
	rm -rf cognis/ui_dist
	cp -r ui/build cognis/ui_dist
	@echo "UI built and staged in cognis/ui_dist"

ui-dev: ## Run SvelteKit dev server (hot reload on :5173)
	cd ui && npm run dev

ui-check: ## Run SvelteKit checks (types, lint)
	cd ui && npm run check

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

serve: ## Start Cognis
	$(COGNIS) serve

run: ## Start Cognis (assumes deps already installed)
	$(COGNIS) serve

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test: ## Run unit tests
	uv run pytest tests/unit/ -v

test-integration: ## Run integration tests (requires Mnemory + Intaris)
	uv run pytest tests/integration/ -v

test-contract: ## Run contract tests (requires Mnemory + Intaris)
	uv run pytest tests/contract/ -v

test-all: ## Run all tests
	uv run pytest -v

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

lint: ## Lint Python code
	uv run ruff check cognis/ tests/

format: ## Format Python code
	uv run ruff format cognis/ tests/

typecheck: ## Type-check Python code
	uv run mypy cognis/

check: lint typecheck ui-check ## Run all checks (lint + types + UI)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

db-migrate: ## Apply all database migrations
	uv run alembic -c cognis/store/migrations/alembic.ini upgrade head

db-revision: ## Create a new migration (usage: make db-revision MSG="description")
	uv run alembic -c cognis/store/migrations/alembic.ini revision --autogenerate -m "$(MSG)"

db-downgrade: ## Rollback one migration
	uv run alembic -c cognis/store/migrations/alembic.ini downgrade -1

# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

build: dev ## Build Python wheel (includes bundled UI)
	COGNIS_SKIP_UI_BUILD=1 uv build

clean: ## Remove build artifacts
	rm -rf cognis/ui_dist ui/build ui/.svelte-kit dist .pytest_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
