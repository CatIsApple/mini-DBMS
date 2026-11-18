PYTHON ?= python3
PSQL ?= psql

all:
	$(PYTHON) -m compileall -q datom

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
	$(PYTHON) tests/integration.py

test-psql:
	$(PYTHON) tests/psql_smoke.py --psql "$(PSQL)"

.PHONY: all test test-psql
