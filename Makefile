PYTHON ?= python

install:
	$(PYTHON) -m pip install -r requirements.txt

migrations:
	$(PYTHON) manage.py makemigrations

migrate:
	$(PYTHON) manage.py migrate

run:
	$(PYTHON) manage.py runserver

test:
	$(PYTHON) -m pytest

coverage:
	$(PYTHON) -m pytest --cov=. --cov-report=html --cov-report=term

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m black .
	$(PYTHON) -m isort .

seed:
	$(PYTHON) manage.py seed_demo

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down
