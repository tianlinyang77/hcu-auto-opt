.PHONY: test lint up down demo postgres-up postgres-down postgres-test

test:
	python -m unittest discover -s tests/unit -v

lint:
	ruff check .

up:
	docker compose up -d --build

down:
	docker compose down

demo:
	docker compose run --rm api dcuopt walking-demo --api-url http://api:8000 --external-workers

postgres-up:
	docker compose up -d postgres

postgres-down:
	docker compose down

postgres-test:
	pytest tests/integration -m postgres
