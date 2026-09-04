# Common tasks. Every target is safe to run repeatedly.

.PHONY: help update setup sync lint fmt test fetch plan verify plant validate-plant physics-gate spatial-gate plant-gate state-gate sensor-gate loss-gate scenario-gate financial-gate dataquality-gate storage-gate curriculum db-up db-down clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

update: ## Update from a release archive, preserving .env and datasets/
	@test -n "$(ARCHIVE)" || { echo "usage: make update ARCHIVE=<path to zip>"; exit 1; }
	./scripts/update.sh "$(ARCHIVE)"

setup: ## Create the virtualenv and install the workspace
	uv venv --python 3.13
	uv sync

sync: ## Re-resolve the workspace after a dependency change
	uv sync

lint: ## Static checks across every package
	uv run ruff check packages/
	uv run ruff format --check packages/

fmt: ## Apply formatting and safe fixes
	uv run ruff check --fix packages/
	uv run ruff format packages/

test: ## Run the whole test suite
	uv run pytest

plan: ## Show what the fetch client would acquire (no network)
	uv run northstar-fetch --config config/northstar.toml plan

ercot-lz-compare: ## Compare LZ and LZEW price series for a load zone
	./scripts/ercot_lz_compare.sh

ercot-duplicates: ## Diagnose duplicate timestamps in ERCOT price data
	./scripts/ercot_duplicates.sh

ercot-retention: ## Find which years ERCOT actually has prices for
	./scripts/ercot_retention.sh

ercot-probe: ## Probe the ERCOT API directly to diagnose an empty response
	./scripts/ercot_probe.sh

fetch-smoke: ## Fetch one partition per provider to confirm credentials work
	uv run northstar-fetch --config config/northstar.toml fetch --limit 1

fetch: ## Acquire missing resource and market partitions
	uv run northstar-fetch --config config/northstar.toml fetch

verify: ## Offline cache integrity check
	uv run northstar-fetch --config config/northstar.toml verify

plant: ## Derived capacity and asset counts
	uv run northstar-sim --config config/northstar.toml summary

summary: ## Report derived capacity and asset counts
	uv run northstar-sim --config config/northstar.toml summary

validate-plant: ## Run the Phase 1 plant validation gate
	uv run northstar-sim --config config/northstar.toml validate

physics-gate: ## Run the Phase 2 physics oracle gate
	uv run northstar-sim --config config/northstar.toml physics-gate

spatial-gate: ## Run the Phase 3 spatial cloud field gate
	uv run northstar-sim --config config/northstar.toml spatial-gate

plant-gate: ## Run the Phase 4 full-plant gate
	uv run northstar-sim --config config/northstar.toml plant-gate

state-gate: ## Run the Phase 5 state and control gate
	uv run northstar-sim --config config/northstar.toml state-gate

sensor-gate: ## Run the Phase 6 sensor layer gate
	uv run northstar-sim --config config/northstar.toml sensor-gate

loss-gate: ## Run the Phase 7 loss attribution gate
	uv run northstar-sim --config config/northstar.toml loss-gate

scenario-gate: ## Run the Phase 8 fault engine gate
	uv run northstar-sim --config config/northstar.toml scenario-gate

financial-gate: ## Run the Phase 9 financial gate
	uv run northstar-sim --config config/northstar.toml financial-gate

dataquality-gate: ## Run the Phase 10 data quality gate
	uv run northstar-sim --config config/northstar.toml dataquality-gate

storage-gate: ## Run the Phase 11 storage gate
	uv run northstar-sim --config config/northstar.toml storage-gate

dataset: ## Export a development dataset to Parquet
	uv run northstar-sim --config config/northstar.toml storage-gate --out datasets/dev

drills: ## List the interview-shaped SQL drills
	@echo "SQL drills - run these in DuckDB or TablePlus:"
	@ls -1 sql/drills/*.sql
	@echo; echo "See sql/drills/README.md"

learn: ## Run a PV learning file: make learn FILE=sql/learning/week1_resource.sql
	@test -n "$(FILE)" || { echo "usage: make learn FILE=sql/learning/week1_resource.sql"; ls -1 sql/learning/*.sql; exit 1; }
	uv run python scripts/run_sql_file.py "$(FILE)"

drill: ## Run a SQL drill: make drill FILE=sql/drills/01_window_functions.sql
	@test -n "$(FILE)" || { echo "usage: make drill FILE=sql/drills/01_window_functions.sql"; ls -1 sql/drills/*.sql; exit 1; }
	uv run python scripts/run_sql_file.py "$(FILE)"

impute: ## Score gap imputation against withheld truth
	uv run northstar-sim --config config/northstar.toml impute \
		--dataset datasets/curriculum --run-id curriculum

score: ## Score blind analysis against injected truth (release gate)
	uv run northstar-sim --config config/northstar.toml score \
		--dataset datasets/year --run-id year

headline: ## Recompute the README figures from a real dataset
	uv run python scripts/headline_figures.py datasets/year year

check-schemas: ## Compare Parquet column types across date partitions
	uv run python scripts/check_partition_schemas.py datasets/observed poa_global

inspect-dataset: ## Inspect a dataset when acceptance reports "no data"
	./scripts/inspect_dataset.sh datasets/observed observed

real-dataset: ## Generate a dataset from fetched NSRDB and ERCOT data
	uv run northstar-sim --config config/northstar.toml generate --real \
		--out datasets/observed --run-id observed
	uv run northstar-sim --config config/northstar.toml accept \
		--dataset datasets/observed --run-id observed

generate: ## Generate a custom dataset (see: northstar-sim generate --help)
	uv run northstar-sim --config config/northstar.toml generate --out datasets/custom

demo: ## Show what the dataset contains - start here
	uv run python scripts/demo.py

dev-dataset: ## Build the dataset the notebooks and acceptance report consume
	uv run northstar-sim --config config/northstar.toml curriculum-gate --out datasets/curriculum --write-sql sql/exercises

curriculum: ## Run the SQL curriculum and write the .sql files
	uv run northstar-sim --config config/northstar.toml curriculum-gate --out datasets/curriculum --write-sql sql/exercises

notebooks: dev-dataset ## Execute the analysis notebooks against the dev dataset
	uv run --group notebooks jupytext --to notebook --update notebooks/*.py
	uv run --group notebooks jupyter execute notebooks/*.ipynb

lab: dev-dataset ## Open JupyterLab against the dev dataset
	uv run --group notebooks jupyter lab

dashboards: ## Generate Grafana dashboard JSON
	uv run northstar-sim --config config/northstar.toml dashboards --out dashboards

schema: ## Emit the full TimescaleDB schema from an exported dataset
	uv run northstar-sim --config config/northstar.toml ddl --dataset datasets/curriculum --out db/init/02_tables.sql

accept: dev-dataset ## Generate the dataset acceptance report
	uv run northstar-sim --config config/northstar.toml accept --dataset datasets/curriculum --report datasets/curriculum/acceptance.csv

db-up: ## Start TimescaleDB and Grafana (schema only - see db-load for data)
	docker compose -f db/docker-compose.yml up -d
	@echo
	@echo "Schema is created but EMPTY. Next: make db-load"

db-load: dev-dataset ## Load the dataset and point the dashboards at it
	uv run northstar-sim --config config/northstar.toml load \
		--dsn "$${NORTHSTAR_DSN:-postgresql://northstar:changeme@localhost:5432/northstar}" \
		--dataset datasets/curriculum --run-id curriculum
	uv run northstar-sim --config config/northstar.toml dashboards \
		--out dashboards --datasource-out db/grafana/datasources \
		--dataset datasets/curriculum --run-id curriculum
	docker compose -f db/docker-compose.yml restart grafana
	@echo
	@echo "Loaded. Grafana: http://localhost:3000 (admin/admin)"

db-test: ## Run the database test suite against a running TimescaleDB
	psql "$${NORTHSTAR_DSN:-postgresql://northstar:changeme@localhost:5432/northstar}" \
		-v ON_ERROR_STOP=1 -f db/tests/test_role_isolation.sql
	psql "$${NORTHSTAR_DSN:-postgresql://northstar:changeme@localhost:5432/northstar}" \
		-v ON_ERROR_STOP=1 -f db/tests/test_reconciliation.sql
	uv run python db/tests/test_exercises_postgres.py \
		--dsn "$${NORTHSTAR_DSN:-postgresql://northstar:changeme@localhost:5432/northstar}"
	uv run python db/tests/test_dashboard_panels.py \
		--dsn "$${NORTHSTAR_DSN:-postgresql://northstar:changeme@localhost:5432/northstar}"

db-schema: ## Apply the full schema to a running database, in order
	psql -d northstar -v ON_ERROR_STOP=1 -f db/init/01_schemas.sql
	psql -d northstar -v ON_ERROR_STOP=1 -f db/init/02_tables.sql
	psql -d northstar -v ON_ERROR_STOP=1 -f db/init/03_hypertables.sql

db-queries: ## Run the time-series query set against a loaded database
	@for f in sql/timeseries/*.sql; do \
		echo "=== $$f ==="; \
		psql "$${NORTHSTAR_DSN:-postgresql://northstar:changeme@localhost:5432/northstar}" \
			-f "$$f" || exit 1; \
	done

db-diagnose: ## Diagnose blank Grafana panels
	./db/diagnose.sh

db-down: ## Stop the database stack, preserving volumes
	docker compose -f db/docker-compose.yml down

clean: ## Remove caches and build artifacts (leaves resource_cache alone)
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache dist build
