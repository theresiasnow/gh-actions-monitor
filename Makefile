.PHONY: install build run test verify clean

install: build ## Build wheel and (re)install to ~/.local/bin via uv tool
	uv tool install . --reinstall
	@$(MAKE) --no-print-directory verify

build: ## Build the wheel only
	uv build --wheel

run: ## Run directly from source (no install)
	uv run main.py

test: ## Run the test suite
	uv run pytest tests/ -q

verify: ## Check the installed copy matches this source tree
	@bin=$$(command -v gh-monitor) || { echo "gh-monitor not on PATH"; exit 1; }; \
	echo "gh-monitor -> $$bin"; \
	env -C / "$$(uv tool dir)/gh-actions-monitor/bin/python" -c \
		"import hashlib,main,sys;\
		 h=hashlib.sha256(open(main.__file__,'rb').read()).hexdigest();\
		 s=hashlib.sha256(open('$(CURDIR)/main.py','rb').read()).hexdigest();\
		 print('installed:', main.__file__);\
		 sys.exit(0) if h==s else sys.exit('STALE: installed main.py differs from source')"
	@echo "installed copy matches source"

clean: ## Remove build artefacts
	rm -rf dist/ build/ *.egg-info __pycache__

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
