.PHONY: test lint postgres-up postgres-down postgres-test

test:
	python -m unittest discover -s tests/unit -v

lint:
	ruff check .

postgres-up:
	docker compose up -d postgres

postgres-down:
	docker compose down

postgres-test:
	pytest tests/integration -m postgres

