# Repository Guidelines

## Project Structure & Module Organization

This repository is a small Python CLI for monitoring GitHub Actions runs in a Rich live table.

- `main.py` contains the full application: GitHub CLI invocation, time formatting, table rendering, and the live refresh loop.
- `pyproject.toml` defines project metadata, Python version, and runtime dependencies.
- `uv.lock` pins resolved dependencies for reproducible installs.
- `README.md` is currently empty; add user-facing usage notes there when features grow.

There is no package or test directory yet. If the project grows, move reusable logic into `gh_actions_monitor/` and tests into `tests/`.

## Build, Test, and Development Commands

Use `uv` for dependency management and execution:

- `uv sync` installs the locked environment.
- `uv run python main.py` runs the monitor locally.
- `uv add <package>` adds a runtime dependency and updates `uv.lock`.
- `uv lock` refreshes the lockfile after dependency changes.

The app shells out to `gh run list`, so install and authenticate GitHub CLI first:

```sh
gh auth status
```

## Coding Style & Naming Conventions

Target Python `>=3.14`. Use 4-space indentation, type hints for public helpers, and concise `snake_case` function names. Constants should be uppercase, as with `REFRESH_SECONDS`, `STATUS_STYLE`, and `CONCLUSION_STYLE`.

Keep rendering in `build_table`, GitHub data access in `fetch_runs`, and formatting helpers separate. Avoid global mutable state beyond simple configuration constants.

## Testing Guidelines

No test framework is configured yet. When adding tests, use `pytest` under `tests/` with names like `test_time_formatting.py`.

Prioritize unit tests for pure helpers such as `time_ago`, `duration`, and table style selection. Mock `subprocess.run` for `fetch_runs`; do not require live GitHub access in automated tests.

Expected future command:

```sh
uv run pytest
```

## Commit & Pull Request Guidelines

Existing history uses concise conventional-style commits, for example `feat: GitHub Actions monitor with rich live table`. Continue with imperative `<type>: <summary>` messages, such as `fix: handle empty GitHub run output`.

Pull requests should include a short description, local commands run, and behavior changes. For terminal table changes, include a screenshot or copied output when practical. Link related issues when available.

## Security & Configuration Tips

Do not store GitHub tokens or credentials in this repository. Rely on `gh auth login` and the GitHub CLI credential store. Treat subprocess JSON parsing defensively when adding fields from `gh`.
