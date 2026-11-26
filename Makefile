PYTHON ?= python3

.PHONY: setup test
setup: .venv/.dependencies

.venv/.dependencies: requirements.txt
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "session-ref requires Python 3.11+")'
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install --no-user --disable-pip-version-check -r requirements.txt
	@touch $@

test:
	$(PYTHON) -m unittest discover -s tests -v
