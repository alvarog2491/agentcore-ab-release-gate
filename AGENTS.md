# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (dependency-groups sync by default; --no-dev for a production-only install)
uv sync

# Run the full test suite
uv run pytest

# Run a single test file
uv run pytest tests/test_action.py

# Run a single test by name
uv run pytest tests/test_action.py::test_name

# Run with coverage (CI enforces >=85%)
uv run pytest --cov=src/agentcore_release_gate --cov=main --cov-report=term-missing

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type-check
uv run mypy src main.py
```

Tests stub AWS API calls with hand-written fakes validated against the real botocore
input shapes (`botocore.validate.validate_parameters`) — no real AWS credentials or
`moto` needed; the fakes exercise the actual request/response contract instead of an
independently-maintained mock service.

## Architecture

This is a composite GitHub Action that evaluates a new Amazon Bedrock AgentCore Runtime image against the currently live version using a native A/B test, then promotes or rolls back.

**Entry point**: `main.py` dispatches to one of five subcommands (`run`, `observe`, `promote`, `rollback`, `report`) based on the `step` action input. The action.yml composite steps set environment variables and call `main.py` via `uv run`. Action inputs are parsed and validated once, through `ActionConfig` (see `schemas.py`), before a `Deployment` is constructed.

**`src/agentcore_release_gate/` package**:
- `deployment.py` — `Deployment` class orchestrates the full lifecycle: baseline capture, treatment endpoint setup, evaluation config cloning, A/B test creation, observation loop, result collection, and promotion/rollback. Every mutable state change is checkpointed to a JSON recovery journal (`state.json`) before the next AWS API call so failures are recoverable.
- `evaluation.py` — polls `GetABTest` until all configured evaluators have scored results, enforces quality gates (minimum score + no regression + optional statistical significance). Parses each evaluator's metrics through the `schemas.EvaluatorMetric`/`VariantMetric` Pydantic models.
- `aws_client.py` — thin wrapper over `boto3` for AgentCore Control, AgentCore (data-plane), and ECR API calls. Client attributes are typed `Any` deliberately (see the comment in `__init__`) so this module's `JsonObject`-based contract stays uniform; `boto3-stubs` is still installed for editor/mypy completion.
- `report.py` — builds and publishes the optional pull-request comment.
- `schemas.py` — Pydantic v2 models: `ActionConfig` validates the action's environment-variable inputs (weights, quality gates, timeouts); `EvaluatorMetric`/`VariantMetric`/`ControlStats` validate one evaluator's slice of a `GetABTest` response.
- `utils.py` — `wait_for` poller, ECR image URI parsing.
- `workflow_logging.py` — `get_workflow_logger()`: the `agentcore_release_gate` logger that `main.py` uses to emit GitHub Actions `::error::`/`::warning::` workflow commands to stdout.
- `types.py` — `JsonObject`, `QualityGates`, `VariantResult` type aliases for the AWS payloads deliberately left untyped (see `schemas.py`'s module docstring for why).
- `constants.py` — timeouts, poll intervals, weight defaults.

**Deployment flow**:
1. Resolve ECR image tag → immutable digest.
2. Capture baseline runtime version (`control` endpoint `liveVersion`).
3. Checkpoint baseline to `state.json` (the always() cleanup step reads this).
4. Ensure `treatment` endpoint and Gateway targets exist.
5. Clone the template online-evaluation config into two ephemeral configs (one per variant), patching CloudWatch log/service names.
6. Create and start the A/B test (variants `C` and `T1`).
7. Observe for `duration-seconds`, polling A/B test status.
8. Wait for all quality-gate evaluators to return results.
9. Enforce gates — promote on pass, rollback on failure.
10. Cleanup: stop A/B test, delete ephemeral evaluation configs.

**Recovery journal** (`state.json`): Records `baseline`, `version`, `ab_test_id`, `ephemeral_*_config_id`, `promoting`, `finished`. The `rollback` subcommand reads this file and is safe to call repeatedly (idempotent via `finished` flag).

**Step modes**: `auto` runs observe+promote in one job; `observe`/`promote`/`rollback` split across jobs using a GitHub Actions artifact to pass `state.json` between jobs.

## Versioning

This project follows [Semantic Versioning](https://semver.org/). Every PR title must be prefixed with a Conventional Commits type that determines the version bump:

| Prefix | Bump | When to use |
|---|---|---|
| `feat:` | **minor** | New feature or capability |
| `fix:` | **patch** | Bug fix |
| `feat!:` / `fix!:` / `refactor!:` (or any type with `!`) | **major** | Breaking change |
| `chore:`, `docs:`, `ci:`, `test:`, `refactor:`, `perf:`, `style:` | none | No user-facing change |

Always choose the prefix that matches the actual change. When a PR contains multiple changes, use the highest-impact prefix (major > minor > patch).

**This is enforced automatically**, not just a convention: [python-semantic-release](https://python-semantic-release.readthedocs.io/) (`[tool.semantic_release]` in `pyproject.toml`) reads squashed-merge commit messages on `main` and computes the bump from this exact table (`commit_parser_options.minor_tags`/`patch_tags` are overridden so `perf:` stays a no-op, matching this table rather than PSR's own default of treating it as a patch). The `release` job in `.github/workflows/ci.yml` runs on every push to `main`, and — only if `quality`, `test`, `actionlint`, and `action-smoke-test` all pass first — writes the new version into `project.version` (and, via `build_command`, `uv.lock`), updates `CHANGELOG.md`, and creates the git tag and GitHub Release. `add_partial_tags = true` also moves the floating `v{major}` tag (e.g. `v1`) to the new release, which is what the README's `@v1` usage example resolves against.

## Changing action inputs/outputs

Any change to inputs or outputs in `action.yml` requires updating the README table and bumping the pinned version in the README via `scripts/bump_readme_pin.py`.

## uv version

`uv` is pinned once, in `pyproject.toml` `[tool.uv].required-version`. CI and the composite action's `setup-uv` step (via `version-file`) both read it, and `uv` refuses to run on a mismatch. The only other copy is the `pip install uv==…` in `[tool.semantic_release].build_command`; bump both together.
