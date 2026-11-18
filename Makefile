PYTHON ?= python3
PSQL ?= psql

all:
	$(PYTHON) -m compileall -q datom bench

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
	$(PYTHON) tests/integration.py

test-psql:
	$(PYTHON) tests/psql_smoke.py --psql "$(PSQL)"

bench:
	$(PYTHON) -m bench.benchmark

.PHONY: all test test-psql bench
