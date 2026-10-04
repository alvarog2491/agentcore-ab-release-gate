"""Exceptions: every deliberate failure shares one base and keeps its built-in type."""

import pytest

from agentcore_release_gate import exceptions as errors


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
