.PHONY: up migrate test check deploy-check docker-build demo-check

up:
	docker compose up -d --build

migrate:
	docker compose exec web python manage.py migrate

test:
	pytest -q

check:
	python manage.py check

deploy-check:
	python manage.py check --deploy --fail-level WARNING

docker-build:
	docker build --pull=false -t competitors_spy:local .

demo-check:
	docker compose exec web python manage.py check
