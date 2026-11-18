PYTHON ?= python3

all:
	$(PYTHON) -m compileall -q datom

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

.PHONY: all test
