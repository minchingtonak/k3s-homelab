AQUA_INSTALL_URL := https://aquaproj.github.io/docs/install

AQUA_YAML := aqua.yaml

# Commands the repo's targets, hooks and CI invoke, read out of aqua.yaml's
# `# bin:` annotations so the pin and the executables it provides cannot drift
# apart. See the comment above `packages:` there for the convention.
AQUA_BINS := $(shell awk '/^packages:/ {p=1; next} /^[^ \t#-]/ {p=0} \
	p && /^[ \t]*-[ \t]*name:/ { \
		if (match($$0, /#[ \t]*bin:[ \t]*/)) print substr($$0, RSTART + RLENGTH); \
		else print "!" $$3 \
	}' $(AQUA_YAML))
TOOLS := $(filter-out !%,$(AQUA_BINS))
UNANNOTATED := $(patsubst !%,%,$(filter !%,$(AQUA_BINS)))

.PHONY: setup
setup: tools hooks doctor

# Guard, not a user-facing target: an unannotated package would make `tools`
# and `doctor` quietly ignore it, which is the silent-skip this convention
# exists to prevent. Underscore-prefixed so `help` does not list it.
.PHONY: _annotated
_annotated:
	@if [ -n "$(UNANNOTATED)" ]; then \
		echo "$(AQUA_YAML) packages missing a '# bin:' annotation:"; \
		for pkg in $(UNANNOTATED); do echo "  $$pkg"; done; \
		echo "add one listing the executables the package installs, e.g."; \
		echo "  - name: owner/repo@v1.2.3 # bin: repo"; \
		exit 1; \
	fi

.PHONY: tools
tools: _annotated ## Install the pinned toolchain from aqua.yaml
	@missing=""; \
	for tool in $(TOOLS); do \
		command -v $$tool >/dev/null 2>&1 || missing="$$missing $$tool"; \
	done; \
	if [ -z "$$missing" ]; then \
		echo "toolchain already on PATH, nothing to install"; \
		exit 0; \
	fi; \
	command -v aqua >/dev/null 2>&1 || { \
		echo "missing tools:$$missing"; \
		echo "aqua is not installed. Install it, then re-run 'make setup':"; \
		echo "  $(AQUA_INSTALL_URL)"; \
		exit 1; \
	}; \
	aqua install; \
	echo "if a command still resolves to an old version, restart your shell"

.PHONY: hooks
hooks: ## Point git at .githooks
	git config core.hooksPath .githooks
	@echo "git hooks enabled (core.hooksPath=.githooks)"

.PHONY: doctor
doctor: _annotated ## Check the pinned tools are installed and git hooks are enabled
	@missing=""; \
	for tool in $(TOOLS); do \
		command -v $$tool >/dev/null 2>&1 || missing="$$missing $$tool"; \
	done; \
	if [ -n "$$missing" ]; then \
		echo "missing tools:$$missing"; \
		echo "run 'make tools' and restart your shell; if aqua manages this"; \
		echo "checkout, check its bin dir ('aqua root-dir') is on your PATH"; \
		exit 1; \
	fi; \
	if [ "$$(git config core.hooksPath)" != ".githooks" ]; then \
		echo "core.hooksPath is not set to .githooks, run 'make hooks'"; \
		exit 1; \
	fi; \
	echo "ok: $$(echo $(TOOLS) | wc -w) tools on PATH; git hooks enabled"

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
