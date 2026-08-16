AQUA_INSTALL_URL := https://aquaproj.github.io/docs/install

.PHONY: setup
setup: tools hooks doctor

.PHONY: tools
tools: ## Install the pinned toolchain from aqua.yaml
	@command -v aqua >/dev/null 2>&1 || { \
		echo "aqua is not installed"; \
		echo "Install it, then re-run 'make setup':"; \
		echo "  $(AQUA_INSTALL_URL)"; \
		exit 1; \
	}
	aqua install
	@echo "if a command still resolves to an old version, restart your shell"

.PHONY: hooks
hooks: ## Point git at .githooks
	git config core.hooksPath .githooks
	@echo "git hooks enabled (core.hooksPath=.githooks)"

.PHONY: doctor
doctor: ## Check the pinned tools are installed and git hooks are enabled
	@command -v aqua >/dev/null 2>&1 || { \
		echo "aqua is not installed: $(AQUA_INSTALL_URL)"; \
		exit 1; \
	}; \
	aqua_bin="$$(aqua root-dir)/bin"; \
	tools="$$(ls "$$aqua_bin" 2>/dev/null)"; \
	if [ -z "$$tools" ]; then \
		echo "no tools installed, run 'make tools'"; \
		exit 1; \
	fi; \
	missing=""; \
	for tool in $$tools; do \
		command -v $$tool >/dev/null 2>&1 || missing="$$missing $$tool"; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "missing tools:$$missing"; \
		echo "run 'make tools' and restart your shell; if that does not help,"; \
		echo "check aqua's bin dir is on your PATH:"; \
		echo "  export PATH=\"$$aqua_bin:\$$PATH\""; \
		exit 1; \
	fi; \
	if [ "$$(git config core.hooksPath)" != ".githooks" ]; then \
		echo "core.hooksPath is not set to .githooks, run 'make hooks'"; \
		exit 1; \
	fi; \
	echo "ok: $$(echo $$tools | wc -w) tools installed; git hooks enabled"

.PHONY: fmt
fmt: ## Format the repo
	dprint fmt

.PHONY: check-fmt
check-fmt: ## Check formatting
	dprint check

.PHONY: lint
lint: ## Lint k8s manifests
	uv run --quiet scripts/lint-k8s.py

.PHONY: check
check: check-fmt lint ## Run every check CI runs

.PHONY: help
help: ## List targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'
