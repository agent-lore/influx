.PHONY: install fmt lint typecheck test check diagrams metrics-history metrics-diff docker-build \
	docker-up-dev docker-up-prod docker-down-dev docker-down-prod

install:
	uv sync

fmt:
	uv run ruff format src/ tests/ scripts/

lint:
	uv run ruff check src/ tests/ scripts/
	uv run ruff format --check src/ tests/ scripts/

typecheck:
	uv run pyright

test:
	uv run pytest

check: lint typecheck test

# Regenerate the architecture docs, metrics, and per-component pages under
# docs/generated/. Run after changing code/models and commit the result; CI
# fails if the committed artifacts drift from the code (.github/workflows/ci.yml).
diagrams:
	uv run pytest tests/guardrail/ -q

# Print the architecture-metrics trend mined from the git history of
# docs/generated/metrics.json. FORMAT=csv|mermaid (default csv).
metrics-history:
	uv run python scripts/metrics_history.py --format $(or $(FORMAT),csv)

# Show the metrics delta between BASE (default origin/main) and the working tree.
metrics-diff:
	@tmp=$$(mktemp); \
	git show $(or $(BASE),origin/main):docs/generated/metrics.json > $$tmp 2>/dev/null || echo '{}' > $$tmp; \
	uv run python scripts/metrics_diff.py $$tmp docs/generated/metrics.json; \
	rm -f $$tmp

docker-build:
	docker build -t influx:dev -f docker/Dockerfile .

docker-up-dev:
	./docker/run.sh dev up

docker-up-prod:
	./docker/run.sh prod up

docker-down-dev:
	./docker/run.sh dev down

docker-down-prod:
	./docker/run.sh prod down
