"""main.py: subcommand dispatch, environment parsing, and GitHub Actions step outputs."""

import json
import os
import sys
from types import SimpleNamespace

import pytest
from aws_mocks import IMAGE

import main as cli
from agentcore_release_gate import aws_client as ab_aws
from agentcore_release_gate.exceptions import ConfigurationError, WorkflowCancelledError
from agentcore_release_gate.report import COMMENT_MARKER


@pytest.fixture
def run_cli(deployment, monkeypatch):
    """Run one main.py subcommand against the deployment fixture's mock AWS clients."""
    clients = {
        "bedrock-agentcore-control": deployment.aws.agentcore_control,
        "bedrock-agentcore": deployment.aws.agentcore,
    }
    session = SimpleNamespace(
        client=lambda service, **_kwargs: clients.get(service, SimpleNamespace())
    )

    def run(command, **environment):
        environment = {
            "STATE_FILE": str(deployment.path),
            "AWS_REGION": "us-east-1",
            "RUNTIME_ID": deployment.aws.runtime_id,
            "GATEWAY_ID": deployment.aws.gateway_id,
            **environment,
        }
        monkeypatch.setattr(os, "environ", environment)
        monkeypatch.setattr(sys, "argv", ["main.py", command])
        monkeypatch.setattr(ab_aws.boto3, "Session", lambda *_args, **_kwargs: session)
        cli.main()

    return run


@pytest.fixture
def evaluation_env():
    """The inputs the run/observe subcommands need on top of the AWS identifiers."""
    return {
        "IMAGE_URI": IMAGE,
        "DURATION_SECONDS": "60",
        "QUALITY_GATES": json.dumps({"Builtin.Helpfulness": 0.7, "Builtin.Correctness": 0.7}),
        "EVALUATION_CONFIG_ID": "template_eval-abcdefghij",
        "AB_TEST_ROLE_ARN": "arn:aws:iam::123456789012:role/ABTestRole",
        "EVALUATION_TIMEOUT_SECONDS": "60",
    }


# --- Environment, signals, and input validation ---


def test_require_env_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv("IMAGE_URI", raising=False)
    with pytest.raises(ConfigurationError, match="IMAGE_URI is not set"):
        cli.require_env("IMAGE_URI")


def test_require_env_returns_the_value(monkeypatch):
    monkeypatch.setenv("IMAGE_URI", "image")
    assert cli.require_env("IMAGE_URI") == "image"


def test_interrupted_signal_handler_raises_workflow_cancelled():
    with pytest.raises(WorkflowCancelledError, match="attempting rollback"):
        cli._interrupted(None, None)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"EVALUATION_TIMEOUT_SECONDS": "0"}, "Evaluation timeout"),
        ({"DURATION_SECONDS": "30"}, "observation must last"),
        ({"SCORING_LAG_SECONDS": "-1"}, "Scoring lag"),
        ({"EVALUATION_CONFIG_ID": " "}, "evaluation-config-id"),
        ({"AB_TEST_ROLE_ARN": " "}, "ab-test-role-arn"),
        ({"QUALITY_GATES": "not-json"}, "quality-gates"),
        ({"QUALITY_GATES": "[]"}, "quality-gates"),
        ({"QUALITY_GATES": "{}"}, "quality-gates"),
        ({"CONTROL_WEIGHT": "abc"}, "control-weight"),
        ({"TREATMENT_WEIGHT": "abc"}, "treatment-weight"),
        ({"CONTROL_WEIGHT": "0", "TREATMENT_WEIGHT": "100"}, "weight"),
        ({"CONTROL_WEIGHT": "50", "TREATMENT_WEIGHT": "51"}, "weight"),
    ],
    ids=[
        "non-positive-timeout",
        "duration-too-short",
        "negative-scoring-lag",
        "blank-evaluation-config-id",
        "blank-ab-test-role-arn",
        "malformed-quality-gates-json",
        "quality-gates-not-an-object",
        "quality-gates-empty-object",
        "non-numeric-control-weight",
        "non-numeric-treatment-weight",
        "zero-control-weight",
        "weights-sum-over-100",
    ],
)
def test_main_rejects_invalid_action_inputs(
    tmp_path, evaluation_env, monkeypatch, overrides, match
):
    environment = {"STATE_FILE": str(tmp_path / "state.json"), **evaluation_env, **overrides}
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(sys, "argv", ["main.py", "observe"])

    with pytest.raises(ConfigurationError, match=match):
        cli.main()


def test_main_logs_failures_as_workflow_errors(tmp_path, evaluation_env, monkeypatch, capsys):
    environment = {
        "STATE_FILE": str(tmp_path / "state.json"),
        **evaluation_env,
        "DURATION_SECONDS": "30",
    }
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(sys, "argv", ["main.py", "observe"])

    with pytest.raises(ConfigurationError):
        cli.main()

    assert "::error::" in capsys.readouterr().out


# --- run ---


def test_run_subcommand_promotes_candidate_and_writes_all_outputs(
    deployment, run_cli, evaluation_env, tmp_path
):
    output_path = tmp_path / "github_output.txt"

    run_cli("run", GITHUB_OUTPUT=str(output_path), **evaluation_env)

    assert json.loads(deployment.path.read_text())["finished"] == "promoted"
    assert deployment.aws.agentcore_control.endpoints == {"control": "2", "treatment": "2"}
    outputs = dict(line.split("=", 1) for line in output_path.read_text().splitlines())
    assert outputs.keys() == {"variant-results", "runtime-version", "image-uri"}
    assert outputs["runtime-version"] == "2"


# --- observe ---


def test_observe_subcommand_leaves_candidate_ready_to_promote(deployment, run_cli, evaluation_env):
    run_cli("observe", **evaluation_env)

    state = json.loads(deployment.path.read_text())
    assert state["ready_to_promote"] is True
    assert state["finished"] is None
    assert deployment.aws.agentcore.tests[state["ab_test_id"]]["executionStatus"] == "RUNNING"


def test_observe_subcommand_writes_variant_results_to_github_output(
    run_cli, evaluation_env, tmp_path
):
    output_path = tmp_path / "github_output.txt"

    run_cli("observe", GITHUB_OUTPUT=str(output_path), SCORING_LAG_SECONDS="0", **evaluation_env)

    line = output_path.read_text().strip()
    assert line.startswith("variant-results=")
    payload = json.loads(line.removeprefix("variant-results="))
    assert payload["Builtin.Helpfulness"]["mean"] == 0.8


# --- promote ---


def test_promote_subcommand_promotes_observed_candidate(deployment, run_cli):
    deployment.observe_candidate(IMAGE, 60)

    run_cli("promote")

    assert json.loads(deployment.path.read_text())["finished"] == "promoted"


def test_promote_subcommand_writes_runtime_outputs(deployment, run_cli, tmp_path):
    output_path = tmp_path / "github_output.txt"
    deployment.observe_candidate(IMAGE, 60)

    run_cli("promote", GITHUB_OUTPUT=str(output_path))

    content = output_path.read_text()
    assert "runtime-version=2" in content
    assert f"image-uri={IMAGE}" in content


# --- rollback ---


def test_rollback_subcommand_needs_no_evaluation_inputs(deployment, run_cli):
    deployment._prepare()

    run_cli("rollback")

    assert json.loads(deployment.path.read_text())["finished"] == "rolled_back"


def test_rollback_subcommand_without_state_needs_no_aws_configuration(deployment, monkeypatch):
    def unexpected_session(*_args, **_kwargs):
        pytest.fail("rollback without state must not create an AWS session")

    monkeypatch.setattr(os, "environ", {"STATE_FILE": str(deployment.path)})
    monkeypatch.setattr(sys, "argv", ["main.py", "rollback"])
    monkeypatch.setattr(ab_aws.boto3, "Session", unexpected_session)

    cli.main()


# --- report ---


class _GitHubResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_report_subcommand_publishes_pr_comment(monkeypatch, tmp_path):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 7}}))
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"finished": "promoted"}))
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return _GitHubResponse([])

    monkeypatch.setattr("agentcore_release_gate.report.urlopen", open_request)
    environment = {
        "GITHUB_EVENT_PATH": str(event_path),
        "STATE_FILE": str(state_path),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123",
        "GITHUB_TOKEN": "token",
        "DEPLOY_OUTCOME": "success",
    }
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(sys, "argv", ["main.py", "report"])

    cli.main()

    assert [request.get_method() for request in requests] == ["GET", "POST"]
    body = json.loads(requests[1].data)["body"]
    assert body.startswith(COMMENT_MARKER)
    assert "github.com/owner/repo/actions/runs/123" in body


def test_report_subcommand_skips_when_not_a_pull_request(monkeypatch, tmp_path):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({}))

    def open_request(*_args, **_kwargs):
        pytest.fail("report without a pull request must not call the GitHub API")

    monkeypatch.setattr("agentcore_release_gate.report.urlopen", open_request)
    monkeypatch.setattr(
        os,
        "environ",
        {"GITHUB_EVENT_PATH": str(event_path), "STATE_FILE": str(tmp_path / "missing.json")},
    )
    monkeypatch.setattr(sys, "argv", ["main.py", "report"])

    cli.main()


def test_report_subcommand_only_warns_when_reporting_fails(monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "argv", ["main.py", "report"])

    cli.main()

    assert "::warning::" in capsys.readouterr().out
