"""Verify the release gate's exception hierarchy and the failures that raise it."""

import sys
from pathlib import Path

import pytest

ACTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACTION))
import main as cli
from agentcore_release_gate import exceptions as errors
from agentcore_release_gate.aws_client import _parse_image
from agentcore_release_gate.deployment import load_state
from agentcore_release_gate.evaluation import enforce_quality_gates
from agentcore_release_gate.utils import wait_for


@pytest.mark.parametrize(
    ("error", "builtin"),
    [
        (errors.ConfigurationError, ValueError),
        (errors.InvalidImageUriError, ValueError),
        (errors.ImageRegionMismatchError, ValueError),
        (errors.StateJournalError, ValueError),
        (errors.ABTestAlreadyActiveError, RuntimeError),
        (errors.GatewayTargetMismatchError, ValueError),
        (errors.CandidateNotReadyError, RuntimeError),
        (errors.AwsResourceFailedError, RuntimeError),
        (errors.AwsWaitTimeoutError, TimeoutError),
        (errors.ABTestInterruptedError, RuntimeError),
        (errors.InvalidEvaluatorResultError, ValueError),
        (errors.EvaluationTimeoutError, TimeoutError),
        (errors.NoSessionsScoredError, TimeoutError),
        (errors.QualityGateFailedError, ValueError),
        (errors.UnexpectedGitHubResponseError, ValueError),
    ],
)
def test_errors_share_a_base_and_keep_their_builtin_type(error, builtin):
    assert issubclass(error, errors.ReleaseGateError)
    assert issubclass(error, builtin)


def test_workflow_cancellation_bypasses_ordinary_exception_handlers():
    assert issubclass(errors.WorkflowCancelledError, KeyboardInterrupt)
    assert not issubclass(errors.WorkflowCancelledError, Exception)
    with pytest.raises(errors.WorkflowCancelledError):
        cli._interrupted(None, None)


def test_require_env_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv("IMAGE_URI", raising=False)
    with pytest.raises(errors.ConfigurationError, match="IMAGE_URI is not set"):
        cli.require_env("IMAGE_URI")


def test_require_env_returns_the_value(monkeypatch):
    monkeypatch.setenv("IMAGE_URI", "image")
    assert cli.require_env("IMAGE_URI") == "image"


def test_missing_state_journal_starts_empty(tmp_path):
    assert load_state(tmp_path / "state.json") == {}


@pytest.mark.parametrize(
    ("content", "message"),
    [("{truncated", "not valid JSON"), ("[]", "must contain a JSON object")],
    ids=["corrupt", "not-an-object"],
)
def test_unreadable_state_journal_is_reported(tmp_path, content, message):
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(errors.StateJournalError, match=message):
        load_state(path)


def test_invalid_image_uri_is_a_configuration_error():
    with pytest.raises(errors.InvalidImageUriError):
        _parse_image("docker.io/library/nginx:latest")


def test_failed_aws_resource_raises_resource_failed():
    with pytest.raises(errors.AwsResourceFailedError):
        wait_for(lambda: {"status": "CREATE_FAILED"}, "READY")


def test_aws_wait_timeout_raises_wait_timeout():
    with pytest.raises(errors.AwsWaitTimeoutError):
        wait_for(lambda: {"status": "CREATING"}, "READY", timeout=0)


def test_failed_quality_gate_raises_quality_gate_failed():
    result = {
        "mean": 0.1,
        "isSignificant": True,
        "absoluteChange": 0.0,
        "percentChange": 0.0,
        "pValue": 0.01,
        "treatmentSampleSize": 5,
        "controlSampleSize": 5,
    }
    with pytest.raises(errors.QualityGateFailedError, match="Builtin.Helpfulness"):
        enforce_quality_gates({"Builtin.Helpfulness": result}, {"Builtin.Helpfulness": 0.5})
