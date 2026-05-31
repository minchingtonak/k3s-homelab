.PHONY: setup
setup:
	git config core.hooksPath .githooks

.PHONY: fmt
fmt:
	dprint fmt

.PHONY: check-fmt
check-fmt:
	dprint check
