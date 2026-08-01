# macOS ships Python 3.9 at this path. The sync script is stdlib-only so it can
# use it directly -- no virtualenv, which is what keeps the LaunchAgent simple.
PYTHON := /usr/bin/python3
SCRIPT := reminders_ha_sync.py

.DEFAULT_GOAL := help

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: test
test: ## Run the merge-engine unit tests (no HA, no Reminders access needed)
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

.PHONY: e2e
e2e: ha-up ## Run the end-to-end test against the dev HA and real Reminders
	$(PYTHON) tests/e2e.py $(E2E_ARGS)

.PHONY: e2e-create
e2e-create: ## Same, but create the RHS test lists in Reminders first
	$(MAKE) e2e E2E_ARGS=--create-lists

.PHONY: check
check: test ## Unit tests plus a syntax check of everything
	$(PYTHON) -m py_compile $(SCRIPT) dev/bootstrap.py dev/formula.py tests/e2e.py tests/test_merge.py
	@echo "ok"

# --- releasing -------------------------------------------------------------- #

.PHONY: formula
formula: ## Print the tap formula for a pushed tag: make formula TAG=v0.1.0
	@test -n "$(TAG)" || { echo "usage: make formula TAG=v0.1.0" >&2; exit 2; }
	@$(PYTHON) dev/formula.py --tag "$(TAG)"

# --- dev Home Assistant ---------------------------------------------------- #

.PHONY: ha-up
ha-up: ## Start the dev HA on :8124, onboard it, write dev/config.json
	docker compose up -d
	$(PYTHON) dev/bootstrap.py

.PHONY: ha-down
ha-down: ## Stop the dev HA
	docker compose down

.PHONY: ha-logs
ha-logs: ## Follow the dev HA log
	docker compose logs -f homeassistant

.PHONY: ha-reset
ha-reset: ## Stop the dev HA and wipe everything it generated
	docker compose down
	rm -f dev/secrets.json dev/config.json
	git clean -xdf dev/ha-config 2>/dev/null || \
		find dev/ha-config -mindepth 1 ! -name configuration.yaml -delete

.PHONY: ha-token
ha-token: ## Print a fresh access token for the dev HA
	@$(PYTHON) dev/bootstrap.py --print-token

# --- driving the sync against the dev instance ----------------------------- #

.PHONY: dev-sync
dev-sync: ## Sync once against the dev HA
	RHS_CONFIG=dev/config.json $(PYTHON) $(SCRIPT) -v sync

.PHONY: dev-dry-run
dev-dry-run: ## Show what a sync against the dev HA would change
	RHS_CONFIG=dev/config.json $(PYTHON) $(SCRIPT) sync --dry-run

.PHONY: dev-doctor
dev-doctor: ## Check the setup against the dev HA
	RHS_CONFIG=dev/config.json $(PYTHON) $(SCRIPT) doctor
