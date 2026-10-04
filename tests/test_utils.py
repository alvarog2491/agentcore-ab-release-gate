"""Utils: wait_for, the AWS resource readiness poller."""

import pytest

from agentcore_release_gate.exceptions import AwsResourceFailedError, AwsWaitTimeoutError
from agentcore_release_gate.utils import wait_for


def test_wait_for_returns_once_status_version_and_execution_status_all_match(clock):
    responses = iter(
        [
            {"status": "READY", "liveVersion": "1", "executionStatus": "RUNNING"},
            {"status": "READY", "liveVersion": "2", "executionStatus": "NOT_STARTED"},
            {"status": "READY", "liveVersion": "2", "executionStatus": "RUNNING"},
        ]
    )

    result = wait_for(lambda: next(responses), "READY", version="2", execution_status="RUNNING")

    assert result == {"status": "READY", "liveVersion": "2", "executionStatus": "RUNNING"}


@pytest.mark.parametrize("status", ["UPDATE_FAILED", "CREATE_FAILED", "SomeError"])
def test_wait_for_raises_on_failed_or_error_status(status):
    with pytest.raises(AwsResourceFailedError, match="failed to become ready"):
        wait_for(lambda: {"status": status}, "READY")


def test_wait_for_invokes_on_poll_callback_while_waiting(clock):
    statuses = iter(["PENDING", "PENDING", "READY"])
    polled = []

    wait_for(
        lambda: {"status": next(statuses)},
        "READY",
        on_poll=lambda result: polled.append(result["status"]),
    )

    assert polled == ["PENDING", "PENDING"]


def test_wait_for_times_out_when_status_never_reached(clock):
    with pytest.raises(AwsWaitTimeoutError, match="Timed out waiting for AWS readiness"):
        wait_for(lambda: {"status": "PENDING"}, "READY", timeout=25)
