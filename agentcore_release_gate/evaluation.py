"""Read managed AgentCore A/B test results and enforce caller-defined quality gates."""

import json
import math
import time
from typing import Protocol, cast

from agentcore_release_gate.constants import (
    EVALUATION_POLL_INTERVAL_SECONDS,
    MINIMUM_RESULT_SAMPLE_SIZE,
    NO_SESSIONS_TIMEOUT_SECONDS,
    SCORING_LAG_SECONDS,
)
from agentcore_release_gate.types import JsonObject, QualityGates, VariantResult, VariantResults


class _ABTestClient(Protocol):
    """Minimal client interface required while polling A/B test results."""

    def get_ab_test(self, **kwargs: str) -> JsonObject:
        """Fetch one A/B test response."""
        ...


def _resolve_evaluator_id(evaluator_arn: str) -> str:
    """Extract an evaluator ID from an AgentCore evaluator ARN.

    Args:
        evaluator_arn: Full AgentCore evaluator ARN.

    Returns:
        The final path component containing the evaluator ID.
    """
    return evaluator_arn.rsplit("/", 1)[-1]


def _collect_variant_results(
    results: JsonObject | None, quality_gates: QualityGates
) -> VariantResults:
    """Extract each requested evaluator's treatment-variant metrics from GetABTest results.

    Args:
        results: Optional result payload returned by AgentCore.
        quality_gates: Evaluator IDs whose treatment results are required.

    Returns:
        Treatment metrics keyed by requested evaluator ID. Evaluators without a
        scored treatment session are omitted.

    Raises:
        ValueError: If AgentCore returns a non-finite or non-numeric mean score.
    """
    collected: VariantResults = {}
    for metric in (results or {}).get("evaluatorMetrics", []):
        evaluator_id = _resolve_evaluator_id(metric["evaluatorArn"])
        if evaluator_id not in quality_gates:
            continue
        # AgentCore fixes variant names to "C" (control) and "T1" (treatment).
        treatment = next(
            (variant for variant in metric["variantResults"] if variant["variantName"] == "T1"),
            None,
        )
        if treatment is None or treatment.get("sampleSize", 0) < MINIMUM_RESULT_SAMPLE_SIZE:
            continue
        mean = treatment["mean"]
        if isinstance(mean, bool) or not isinstance(mean, (int, float)) or not math.isfinite(mean):
            raise ValueError("AgentCore returned an invalid mean score for " + evaluator_id)
        collected[evaluator_id] = VariantResult(
            mean=mean,
            isSignificant=bool(treatment.get("isSignificant")),
            absoluteChange=cast(float | None, treatment.get("absoluteChange")),
            percentChange=cast(float | None, treatment.get("percentChange")),
            pValue=cast(float | None, treatment.get("pValue")),
            treatmentSampleSize=cast(int, treatment["sampleSize"]),
            controlSampleSize=cast(int, metric["controlStats"]["sampleSize"]),
        )
    return collected


def _total_sample_size(results: JsonObject | None) -> int:
    """Sum sampleSize across all variants and evaluators in a GetABTest result payload."""
    total = 0
    for metric in (results or {}).get("evaluatorMetrics", []):
        for variant in metric.get("variantResults") or []:
            total += variant.get("sampleSize", 0)
    return total


def _partial_results(collected: VariantResults) -> JsonObject:
    """Render each collected variant's key metrics for progress logging."""
    return {
        k: {
            "mean": v["mean"],
            "treatmentSamples": v["treatmentSampleSize"],
            "controlSamples": v["controlSampleSize"],
            "pValue": v["pValue"],
        }
        for k, v in collected.items()
    }


def _emit_final_results(ab_test_id: str, results: VariantResults) -> VariantResults:
    """Log the terminal A/B-test results event and return the results unchanged."""
    print(
        json.dumps({"event": "ab-test-results", "abTestId": ab_test_id, "results": results}),
        flush=True,
    )
    return results


def wait_for_ab_test_results(
    client: _ABTestClient,
    ab_test_id: str,
    quality_gates: QualityGates,
    timeout: float,
    no_sessions_timeout: float = NO_SESSIONS_TIMEOUT_SECONDS,
    scoring_lag_seconds: float = SCORING_LAG_SECONDS,
    require_significance: bool = True,
) -> VariantResults:
    """Wait until every quality gate has scored results and those results have stabilized.

    Phase 1 waits until every requested evaluator has at least one scored treatment
    session. Phase 2 then keeps polling until the total scored-sample count has not
    grown for ``scoring_lag_seconds``, allowing late-arriving evaluation scores to
    accumulate before quality gates are enforced.

    When ``require_significance`` is True, the function does not return early on
    stabilization alone — it continues polling until all evaluators reach statistical
    significance or ``timeout`` elapses.

    Args:
        client: AgentCore client used to retrieve the A/B test.
        ab_test_id: Identifier of the A/B test to poll.
        quality_gates: Evaluator IDs that must have treatment results.
        timeout: Maximum number of seconds to wait across both phases.
        no_sessions_timeout: Fail fast if no sessions are scored within this many seconds.
        scoring_lag_seconds: After all evaluators have results, continue polling until
            the total sample count is unchanged for this many seconds.
        require_significance: When True (the default) an early exit is only allowed once
            all evaluators report statistical significance; otherwise stabilization alone
            is sufficient.

    Returns:
        Available treatment metrics for every requested evaluator.

    Raises:
        TimeoutError: If any requested evaluator lacks results before ``timeout``, or if
            no sessions are scored within ``no_sessions_timeout``.
    """
    start = time.monotonic()
    deadline = start + timeout
    no_sessions_deadline = start + no_sessions_timeout
    last_total_samples: int = 0
    last_change_time: float | None = None
    latest_collected: VariantResults = {}
    while time.monotonic() < deadline:
        response = client.get_ab_test(abTestId=ab_test_id)
        results_payload = response.get("results")
        collected = _collect_variant_results(results_payload, quality_gates)
        total_samples = _total_sample_size(results_payload)
        now = time.monotonic()
        elapsed = int(now - start)
        remaining = max(0, int(deadline - now))

        if collected.keys() >= quality_gates.keys():
            latest_collected = collected
            if last_change_time is None or total_samples > last_total_samples:
                last_total_samples = total_samples
                last_change_time = now
            time_since_change = now - last_change_time
            all_significant = all(v["isSignificant"] for v in collected.values())
            stable = time_since_change >= scoring_lag_seconds
            can_exit_early = stable and (not require_significance or all_significant)
            if can_exit_early:
                return _emit_final_results(ab_test_id, latest_collected)
            print(
                json.dumps(
                    {
                        "event": "stabilizing-results",
                        "abTestId": ab_test_id,
                        "elapsedSeconds": elapsed,
                        "remainingSeconds": remaining,
                        "totalSamplesScored": total_samples,
                        "stableForSeconds": int(time_since_change),
                        "remainingScoringLagSeconds": max(
                            0, int(scoring_lag_seconds - time_since_change)
                        ),
                        "awaitingSignificance": require_significance and not all_significant,
                        "partialResults": _partial_results(collected),
                    }
                ),
                flush=True,
            )
        else:
            ready = list(collected.keys())
            waiting = [k for k in quality_gates if k not in collected]
            print(
                json.dumps(
                    {
                        "event": "waiting-for-results",
                        "abTestId": ab_test_id,
                        "elapsedSeconds": elapsed,
                        "remainingSeconds": remaining,
                        "totalSamplesScored": total_samples,
                        "evaluatorsReady": ready,
                        "evaluatorsWaiting": waiting,
                        "partialResults": _partial_results(collected),
                    }
                ),
                flush=True,
            )
            if total_samples == 0 and now > no_sessions_deadline:
                raise TimeoutError(
                    f"No sessions scored after {int(no_sessions_timeout)}s — "
                    "check that traffic is flowing through the gateway and that "
                    "the online evaluation configs are correctly linked to the A/B test"
                )
        time.sleep(min(EVALUATION_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))

    if latest_collected.keys() >= quality_gates.keys():
        return _emit_final_results(ab_test_id, latest_collected)
    raise TimeoutError("Timed out waiting for AgentCore A/B test results")


def enforce_quality_gates(
    collected: VariantResults,
    quality_gates: QualityGates,
    *,
    require_significance: bool = True,
) -> None:
    """Enforce minimum score, non-regression, and (optionally) significance requirements.

    Args:
        collected: Treatment metrics keyed by evaluator ID.
        quality_gates: Minimum acceptable score keyed by evaluator ID.
        require_significance: When True (the default) an evaluator whose treatment
            result is not statistically significant fails the gate. When False only
            the minimum score and regression checks apply.

    Raises:
        ValueError: If an evaluator fails one or more quality requirements.
    """
    failed: dict[str, VariantResult] = {}
    for name, minimum in quality_gates.items():
        variant = collected[name]
        change = variant["absoluteChange"]
        if (
            variant["mean"] < minimum
            or (require_significance and not variant["isSignificant"])
            or (change is not None and change < 0)
        ):
            failed[name] = variant
    if failed:
        raise ValueError("AgentCore A/B test quality gates failed: " + json.dumps(failed))
