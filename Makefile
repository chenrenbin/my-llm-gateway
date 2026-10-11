.PHONY: install run test lint docker

install:
	python3 -m pip install -e '.[dev]'

run:
	GATEWAY_CONFIG=config/gateway.yaml uvicorn gateway.main:build_app --factory --reload --port 8000

test:
	pytest -q

lint:
	ruff check src tests

docker:
	docker compose up --build
