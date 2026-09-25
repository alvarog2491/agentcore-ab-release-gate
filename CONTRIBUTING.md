# Contributing

## Development setup

```bash
uv sync
```

Dependency-groups (`pytest`, `ruff`, `mypy`, `boto3-stubs`) sync by default. Pass
`--no-dev` to reproduce the production-only install `action.yml` uses at runtime.

## Useful commands

| Command | What it does |
|---|---|
| `uv run pytest` | Run the full test suite |
| `uv run pytest --cov=src/agentcore_release_gate --cov=main --cov-report=term-missing` | Run with coverage (CI enforces >=85%) |
| `uv run ruff check .` | Lint `src/`, `main.py`, `scripts/`, and `tests/` |
| `uv run ruff format .` | Auto-format all Python files |
| `uv run mypy src main.py` | Type-check the action's own source (strict mode) |

## Project layout

```
action.yml                       # action manifest — inputs, outputs, composite steps
main.py                          # entry point called by action.yml via `uv run`
src/agentcore_release_gate/
  deployment.py                  # Deployment lifecycle: baseline, endpoints, A/B test, promote/rollback
  evaluation.py                  # GetABTest polling and quality-gate enforcement
  aws_client.py                  # thin boto3 wrapper for AgentCore Control, AgentCore, and ECR
  schemas.py                     # Pydantic models: action-input validation and AB-test result parsing
  report.py                      # pull-request result comment
  utils.py                       # wait_for poller, ECR image URI parsing
  types.py                       # shared type aliases for the AWS payloads left untyped
  constants.py                   # timeouts, poll intervals, weight defaults
tests/
  test_action.py                 # integration-level tests for the full action flow
  test_bump_readme_pin.py
  test_report.py
scripts/
  bump_readme_pin.py             # utility for pinning the README version badge
.github/workflows/
  ci.yml                          # lint, type-check, test, actionlint, a composite-action smoke
                                   # test, and (on push to main) the semantic-release job
```

## Running tests

```bash
uv run pytest
```

Tests stub AWS API calls with hand-written fakes validated against the real botocore
input shapes (`botocore.validate.validate_parameters`), not `moto` — `moto` does not
model AgentCore's A/B test or online-evaluation-config operations at all, which are
this action's core logic. No real AWS credentials or resources are needed.

## Making changes

1. Edit source under `src/agentcore_release_gate/` or `main.py`.
2. Add or update tests in `tests/`.
3. Run `uv run pytest`, `uv run ruff check .`, and `uv run mypy src main.py` before opening a PR.
4. Prefix your PR title (and, since merges are squashed, the merge commit message) with a
   [Conventional Commits](https://www.conventionalcommits.org/) type — see [Releases](#releases)
   below for exactly how each type maps to a version bump.
5. If you changed any action inputs or outputs in `action.yml`, also update the README's
   input/output tables and run `python3 scripts/bump_readme_pin.py <sha> <tag>` to keep the
   README's usage example pinned to a real, resolvable reference. This is independent of the
   version number itself, which is computed automatically — see below.

## Releases

Releases are fully automated by [python-semantic-release](https://python-semantic-release.readthedocs.io/)
(configured in `pyproject.toml`'s `[tool.semantic_release]`) — there is no manual version bump
or tag to create. On every push to `main`, once `quality`, `test`, `actionlint`, and
`action-smoke-test` all pass, the `release` job in `.github/workflows/ci.yml`:

1. Parses commit messages since the last release using the table in
   [`AGENTS.md`](AGENTS.md#versioning) (`feat:` → minor, `fix:` → patch, a `!` or
   `BREAKING CHANGE:` footer on any type → major, everything else → no release).
2. Writes the new version into `project.version`, updates `CHANGELOG.md`, commits, and tags.
3. Moves the floating `v{major}` tag (e.g. `v1`) to the release, and publishes a GitHub Release.

If no commit since the last release warrants a bump, the job runs and simply does nothing.

## Contract changes

The public contract is:

- Inputs and outputs declared in `action.yml`
- The promotion/rollback decision logic documented in the README

Any change to these warrants a major version bump (a `!` on your commit type) and a clear
description in the PR.

## Reporting bugs and feature requests

Open an issue in this repository. Please include:

- The action version and the inputs you passed to it
- The error output or relevant excerpt from your GitHub Actions job log
- What you expected to happen and what actually happened
