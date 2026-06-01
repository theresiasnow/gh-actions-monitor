.PHONY: install build run clean

install: ## Build wheel and (re)install to ~/.local/bin via uv tool
	uv build --wheel
	uv tool install . --force

build: ## Build the wheel only
	uv build --wheel

run: ## Run directly from source (no install)
	uv run main.py

clean: ## Remove build artefacts
	rm -rf dist/ build/ *.egg-info __pycache__

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
