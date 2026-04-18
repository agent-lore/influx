.PHONY: install fmt lint typecheck test check docker-build \
	docker-up-dev docker-up-prod docker-down-dev docker-down-prod

install:
	uv sync

fmt:
	uv run ruff format src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

typecheck:
	uv run pyright

test:
	uv run pytest

check: lint typecheck test

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
