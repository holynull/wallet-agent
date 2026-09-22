# Task 1 implementation report

## Files changed

- `tests/helpers/__init__.py`
- `tests/helpers/contracts.py`
- `tests/contracts/test_helpers.py`
- `tests/__init__.py` (package marker needed to avoid an installed third-party `tests` package shadowing local helpers)

## Verification

- Initial focused test run failed during collection with `ModuleNotFoundError`, before helper implementation.
- `PYTHONPATH=.:src .venv/bin/pytest tests/contracts/test_helpers.py -q` — 8 passed.
- `.venv/bin/ruff check tests/helpers tests/contracts/test_helpers.py` — all checks passed.

## Commit

`906e777` (`test: add response and amount contract helpers`)

## Concerns

The repository environment contains a site-packages package named `tests`; the local `tests/__init__.py` marker is required for the requested `tests.helpers.contracts` import to resolve reliably.
