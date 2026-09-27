"""Poll AWS resources until they reach a requested state."""

import time
from collections.abc import Callable, Collection
from typing import TypeVar

from agentcore_release_gate.constants import (
    AWS_POLL_INTERVAL_SECONDS,
    DEFAULT_AWS_WAIT_TIMEOUT_SECONDS,
)
from agentcore_release_gate.exceptions import AwsResourceFailedError, AwsWaitTimeoutError

ResultT = TypeVar("ResultT", bound=dict[str, object])


def wait_for(
    read: Callable[[], ResultT],
    status: str | Collection[str],
    version: str | None = None,
    timeout: float = DEFAULT_AWS_WAIT_TIMEOUT_SECONDS,
    execution_status: str | None = None,
    on_poll: Callable[[ResultT], None] | None = None,
) -> ResultT:
    """Wait for an AWS resource to reach the requested state.

    Args:
        read: Callable that fetches the latest resource representation.
        status: One acceptable status or a collection of acceptable statuses.
        version: Optional live version that must also match.
        timeout: Maximum number of seconds to wait.
        execution_status: Optional A/B test execution status that must also match.
        on_poll: Optional callable invoked with the latest result on each tick where
            the target state has not been reached and no failure was detected.

    Returns:
        The first resource representation matching all requested conditions.

    Raises:
        AwsResourceFailedError: If AWS reports a failed resource status.
        AwsWaitTimeoutError: If the requested state is not reached before ``timeout``.
    """
    statuses = (status,) if isinstance(status, str) else status
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read()
        if (
            result["status"] in statuses
            and (version is None or result.get("liveVersion") == version)
            and (execution_status is None or result.get("executionStatus") == execution_status)
        ):
            return result
        resource_status = result["status"]
        if isinstance(resource_status, str) and (
            "FAILED" in resource_status or "ERROR" in resource_status.upper()
        ):
            raise AwsResourceFailedError("AWS resource failed to become ready")
        if on_poll is not None:
            on_poll(result)
        time.sleep(AWS_POLL_INTERVAL_SECONDS)
    raise AwsWaitTimeoutError("Timed out waiting for AWS readiness")
