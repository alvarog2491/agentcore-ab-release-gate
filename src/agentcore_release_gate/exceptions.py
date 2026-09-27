"""Exceptions raised by the release gate.

Every deliberate failure derives from ``ReleaseGateError``, so callers can tell an
expected, explained failure apart from a bug or a raw AWS SDK error. Each class also
inherits the built-in exception it replaced (``ValueError``, ``RuntimeError``,
``TimeoutError``), so existing ``except ValueError`` style handlers keep working.

Pydantic validators in ``schemas.py`` still raise plain ``ValueError``: Pydantic only
collects ``ValueError``/``AssertionError`` into a ``ValidationError``, which ``main.py``
then re-raises as ``ConfigurationError``.
"""


class ReleaseGateError(Exception):
    """Base class for every failure this action raises on purpose."""


class WorkflowCancelledError(KeyboardInterrupt):
    """The workflow was cancelled (SIGTERM/SIGINT) while the action was running.

    Deliberately a ``KeyboardInterrupt``, not a ``ReleaseGateError``: it must bypass
    ordinary ``except Exception`` handlers so only the rollback path catches it.
    """


# ── Configuration ─────────────────────────────────────────────────────────────


class ConfigurationError(ReleaseGateError, ValueError):
    """An action input or required environment variable is missing or invalid."""


class InvalidImageUriError(ConfigurationError):
    """The candidate image is not an ECR URI with a tag or digest."""


class ImageRegionMismatchError(ConfigurationError):
    """The candidate ECR image lives in a different AWS Region than AgentCore."""


class StateJournalError(ReleaseGateError, ValueError):
    """The recovery journal (state.json) exists but cannot be read as a JSON object."""


# ── Deployment preconditions ──────────────────────────────────────────────────


class ABTestAlreadyActiveError(ReleaseGateError, RuntimeError):
    """Another A/B test is still running or paused on the Gateway."""


class GatewayTargetMismatchError(ReleaseGateError, ValueError):
    """An existing Gateway target points at a different runtime or endpoint."""


class CandidateNotReadyError(ReleaseGateError, RuntimeError):
    """Promotion was requested but no candidate has passed observation."""


# ── AWS resource waits ────────────────────────────────────────────────────────


class AwsResourceFailedError(ReleaseGateError, RuntimeError):
    """An AWS resource reported a FAILED or ERROR status while being waited on."""


class AwsWaitTimeoutError(ReleaseGateError, TimeoutError):
    """An AWS resource did not reach the requested state before the timeout."""


# ── A/B test and evaluation ───────────────────────────────────────────────────


class ABTestInterruptedError(ReleaseGateError, RuntimeError):
    """The A/B test left the RUNNING state during the observation window."""


class InvalidEvaluatorResultError(ReleaseGateError, ValueError):
    """AgentCore returned a malformed score for a gated evaluator."""


class EvaluationTimeoutError(ReleaseGateError, TimeoutError):
    """Not every gated evaluator produced results before the evaluation timeout."""


class NoSessionsScoredError(EvaluationTimeoutError):
    """No sessions were scored at all, which usually means a misconfigured setup."""


class QualityGateFailedError(ReleaseGateError, ValueError):
    """The candidate failed at least one quality gate and must not be promoted."""


# ── Pull-request report ───────────────────────────────────────────────────────


class UnexpectedGitHubResponseError(ReleaseGateError, ValueError):
    """The GitHub API returned a payload with an unexpected shape."""
