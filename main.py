"""Run deployment, recovery, or PR reporting for the standalone action."""

import argparse
import json
import os
import signal
from pathlib import Path

from pydantic import ValidationError

from agentcore_release_gate.deployment import Deployment
from agentcore_release_gate.report import build_report, publish_report
from agentcore_release_gate.schemas import ActionConfig
from agentcore_release_gate.workflow_logging import get_workflow_logger

logger = get_workflow_logger()


def _interrupted(_signal: int, _frame: object) -> None:
    """Turn SIGTERM/SIGINT into KeyboardInterrupt so rollback handlers still run."""
    raise KeyboardInterrupt("Workflow interrupted; attempting rollback")


def cmd_report() -> None:
    """Publish the optional pull-request report for the completed action run.

    Missing pull-request context is expected for non-PR workflows and skips
    reporting without affecting the deployment outcome.
    """
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pull_request = event.get("pull_request", {}).get("number")
    if not pull_request:
        return
    state_path = os.environ.get("STATE_FILE", "")
    state = {}
    if state_path and Path(state_path).is_file():
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    run_url = (
        f"{server}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    report = build_report(state, os.environ.get("DEPLOY_OUTCOME", "failure"), run_url)
    publish_report(
        os.environ["GITHUB_TOKEN"],
        os.environ["GITHUB_REPOSITORY"],
        int(pull_request),
        report,
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )


def cmd_rollback(state_file: str) -> None:
    """Recover an unfinished deployment when its state journal exists.

    Args:
        state_file: Path to the deployment recovery journal.
    """
    # Validation failures can reach cleanup without creating deployment state.
    if not Path(state_file).exists():
        return
    Deployment(state_file).rollback()


def cmd_promote(state_file: str) -> None:
    """Promote the candidate recorded as ready in a recovery journal.

    Args:
        state_file: Path to the deployment recovery journal.
    """
    Deployment(state_file).promote_candidate()


def _parse_int_env(name: str, default: str, label: str) -> int:
    """Read an integer action input, raising a labeled error on malformed input."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be an integer") from error


def _load_action_config() -> ActionConfig:
    """Parse and validate the action's configuration from environment variables.

    Raises:
        ValueError: If any input is missing, malformed, or out of range. Wraps
            pydantic.ValidationError so the CLI surfaces a single readable message
            instead of a full validation-error dump.
    """
    try:
        gates = json.loads(os.environ["QUALITY_GATES"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("quality-gates must map evaluator IDs to minimum scores") from error

    try:
        return ActionConfig(
            quality_gates=gates if isinstance(gates, dict) else {},
            require_significance=(
                os.environ.get("REQUIRE_SIGNIFICANCE", "true").strip().lower() != "false"
            ),
            control_endpoint_name=os.environ.get("CONTROL_ENDPOINT_NAME", "control"),
            evaluation_config_id=os.environ["EVALUATION_CONFIG_ID"],
            ab_test_role_arn=os.environ["AB_TEST_ROLE_ARN"],
            control_weight=_parse_int_env("CONTROL_WEIGHT", "80", "control-weight"),
            treatment_weight=_parse_int_env("TREATMENT_WEIGHT", "20", "treatment-weight"),
            evaluation_timeout=_parse_int_env(
                "EVALUATION_TIMEOUT_SECONDS", "900", "evaluation-timeout-seconds"
            ),
            duration_seconds=_parse_int_env("DURATION_SECONDS", "7200", "duration-seconds"),
            scoring_lag_seconds=_parse_int_env("SCORING_LAG_SECONDS", "120", "scoring-lag-seconds"),
        )
    except ValidationError as error:
        raise ValueError("; ".join(err["msg"] for err in error.errors())) from error


def _build_deployment(state_file: str) -> tuple[Deployment, int]:
    """Build a Deployment from validated inputs; also return the observation duration."""
    config = _load_action_config()
    deployment = Deployment(
        state_file,
        quality_gates=config.quality_gates,
        require_significance=config.require_significance,
        control_endpoint_name=config.control_endpoint_name,
        evaluation_config_template=config.evaluation_config_id,
        ab_test_role_arn=config.ab_test_role_arn,
        control_weight=config.control_weight,
        treatment_weight=config.treatment_weight,
        evaluation_timeout=config.evaluation_timeout,
        scoring_lag_seconds=config.scoring_lag_seconds,
    )
    return deployment, config.duration_seconds


def cmd_observe(state_file: str) -> None:
    """Deploy and evaluate a candidate without promoting it.

    Args:
        state_file: Path where deployment state is persisted for later promotion.
    """
    deployment, duration = _build_deployment(state_file)
    deployment.observe_candidate(os.environ["IMAGE_URI"], duration)


def cmd_run(state_file: str) -> None:
    """Run the action's complete observe-and-promote deployment workflow.

    Args:
        state_file: Path where deployment state is persisted for recovery.
    """
    deployment, duration = _build_deployment(state_file)
    deployment.run(os.environ["IMAGE_URI"], duration)


def main() -> None:
    """Dispatch the selected action subcommand and install cancellation handling."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "observe", "promote", "rollback", "report"])
    command = parser.parse_args().command

    if command == "report":
        try:
            cmd_report()
        except Exception as error:
            # Reporting is optional and must never change the deployment result.
            logger.warning("::warning::Unable to publish AgentCore A/B PR report: %s", error)
        return

    state_file = os.environ["STATE_FILE"]
    signal.signal(signal.SIGTERM, _interrupted)
    signal.signal(signal.SIGINT, _interrupted)

    try:
        if command == "rollback":
            cmd_rollback(state_file)
        elif command == "promote":
            cmd_promote(state_file)
        elif command == "observe":
            cmd_observe(state_file)
        else:
            cmd_run(state_file)
    except BaseException as error:
        logger.error("::error::%s", error)
        raise


if __name__ == "__main__":
    main()
