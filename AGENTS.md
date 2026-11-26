# Repository Guidelines

## Project structure

The executable `session-ref` loads the project virtual environment and dispatches to `session_ref/cli.py`. The `shell/session-ref.zsh` functions connect the existing CLI names to this entry point. Keep provider protocols and metadata normalization in `catalog.py`, terminal byte decoding in `keys.py`, and PTY lifecycle and routing in `terminal.py`. The terminal selector handles input in `picker.py`, lays out rows in `view.py`, and restores full-screen CLI output with `screen.py`. Each picker belongs to the terminal that launched it.

## Build and development

Use macOS with zsh and Python 3.11 or newer. Run these commands from the repository root.

- `make setup` installs the pinned Claude SDK in `.venv/`. The terminal interface uses the Python standard library.
- `./session-ref list --summary` reports counts and provider errors without printing titles.
- `./session-ref run codex` exercises an interactive launch. Source `shell/session-ref.zsh` in a disposable zsh to check the original command names.

## Coding conventions

Use four-space indentation. Choose snake_case for Python functions and variables and PascalCase for types. Keep terminal payloads as bytes. Validate an ID before writing it to a PTY. Resolve vendor executables through the current PATH so upgrades and existing isolation launchers continue to work.

## Testing

Run `make test` for standard-library unittest discovery under `tests/test_*.py`. Add regression cases for affected protocol fields, split escape sequences, or argument forwarding. Use synthetic session metadata in fixtures. For input or screen restoration changes, check unsent drafts locally and over an interactive SSH connection, including cancellation and bracketed paste.

## Commits and pull requests

This repository has no commit history yet. Use short, imperative subjects that name the affected behavior. In pull requests, describe the user-visible change and list the checks performed. Identify the terminal and CLI used for interactive checks. Redact personal session titles, IDs, and paths from screenshots or logs.
