.DEFAULT_GOAL := help
VENV := .venv
PY   := $(VENV)/bin/python
ifeq ($(OS),Windows_NT)
PY := $(VENV)/Scripts/python.exe
endif

.PHONY: help install demo test test-all lint typecheck check serve migrate up down clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install the project with dev extras
	python -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

demo: ## Run the bundled incident end to end (no credentials needed)
	$(PY) -m aic.cli demo

test: ## Offline test suite
	$(PY) -m pytest -q

test-all: ## Offline suite plus integration tests (needs `make up`)
	$(PY) -m pytest -q
	$(PY) -m pytest -m integration -q

lint: ## Ruff
	$(PY) -m ruff check .

typecheck: ## Mypy in strict mode
	$(PY) -m mypy

check: lint typecheck test ## Everything CI runs on the offline path

serve: ## Run the API locally
	$(PY) -m aic.cli serve

migrate: ## Apply pending database migrations
	$(PY) -m aic.cli migrate

up: ## Start Postgres and Redis
	docker compose up -d postgres redis

down: ## Stop the stack and drop its volumes
	docker compose down -v

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build
