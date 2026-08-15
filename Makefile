.DEFAULT_GOAL := help
.PHONY: help install dev lint format typecheck test check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies into a local virtualenv
	uv sync --all-extras

dev: ## Run the development server with reload
	uv run uvicorn calon.main:app --reload --host 127.0.0.1 --port 8000

lint: ## Check formatting and lint rules
	uv run ruff check .
	uv run ruff format --check .

format: ## Apply formatting and safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Run mypy
	uv run mypy

test: ## Run the test suite
	uv run pytest

check: lint typecheck test ## Everything CI runs — do this before opening a PR

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
