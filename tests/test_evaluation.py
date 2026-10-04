"""Evaluation: parsing A/B test results, waiting for scores, and enforcing quality gates."""

import copy
from types import SimpleNamespace

import pytest
from aws_mocks import DEFAULT_RESULTS

from agentcore_release_gate.evaluation import (
    _collect_variant_results,
    enforce_quality_gates,
    wait_for_ab_test_results,
)
from agentcore_release_gate.exceptions import (
    InvalidEvaluatorResultError,
    NoSessionsScoredError,
    QualityGateFailedError,
)

# --- _collect_variant_results ---


@pytest.mark.parametrize(
    "score",
    [float("nan"), float("inf"), True, "0.9", None],
    ids=["nan", "infinity", "boolean", "string", "none"],
)
def test_collect_variant_results_rejects_invalid_mean_score(score):
    results = {
        "evaluatorMetrics": [
            {
                "evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/custom-eval-abcdefghij",
                "controlStats": {"variantName": "C", "sampleSize": 1, "mean": 0.5},
                "variantResults": [
                    {
                        "variantName": "T1",
                        "sampleSize": 1,
                        "mean": score,
                        "isSignificant": True,
                        "absoluteChange": 0,
                    }
                ],
            }
        ]
    }

    with pytest.raises(InvalidEvaluatorResultError, match="invalid mean score"):
        _collect_variant_results(results, {"custom-eval-abcdefghij": 3})


def test_collect_variant_results_skips_evaluator_without_scored_sessions():
    results = {
        "evaluatorMetrics": [
            {
                "evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/Builtin.Helpfulness",
                "controlStats": {"variantName": "C", "sampleSize": 0, "mean": 0.0},
                "variantResults": [
                    {
                        "variantName": "T1",
                        "sampleSize": 0,
                        "mean": 0.8,
                        "isSignificant": False,
                        "absoluteChange": None,
                    }
                ],
            }
        ]
    }

    assert _collect_variant_results(results, {"Builtin.Helpfulness": 0.7}) == {}


def test_collect_variant_results_ignores_invalid_mean_on_ungated_evaluator():
    results = {
        "evaluatorMetrics": [
            {
                "evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/Builtin.Helpfulness",
                "controlStats": {"variantName": "C", "sampleSize": 10, "mean": 0.75},
                "variantResults": [
                    {
                        "variantName": "T1",
                        "sampleSize": 8,
                        "mean": 0.8,
                        "isSignificant": True,
                        "absoluteChange": 0.05,
                    }
                ],
            },
            {
                "evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/Builtin.Unrelated",
                "controlStats": {"variantName": "C", "sampleSize": 5, "mean": 0.5},
                "variantResults": [
                    {"variantName": "T1", "sampleSize": 5, "mean": True, "isSignificant": True}
                ],
            },
        ]
    }

    collected = _collect_variant_results(results, {"Builtin.Helpfulness": 0.7})

    assert collected["Builtin.Helpfulness"]["mean"] == 0.8
    assert "Builtin.Unrelated" not in collected


# --- enforce_quality_gates ---


@pytest.mark.parametrize(
    "variant",
    [
        {"mean": 0.5, "isSignificant": True, "absoluteChange": 0.1},
        {"mean": 0.9, "isSignificant": False, "absoluteChange": 0.1},
        {"mean": 0.9, "isSignificant": True, "absoluteChange": -0.1},
    ],
    ids=["below-minimum", "not-significant", "regression"],
)
def test_enforce_quality_gates_rejects_failing_result(variant):
    with pytest.raises(QualityGateFailedError, match="Builtin.Helpfulness"):
        enforce_quality_gates({"Builtin.Helpfulness": variant}, {"Builtin.Helpfulness": 0.7})


@pytest.mark.parametrize("change", [0.1, 0.0, None], ids=["improved", "unchanged", "unknown"])
def test_enforce_quality_gates_accepts_significant_non_negative_change(change):
    variant = {"mean": 0.7, "isSignificant": True, "absoluteChange": change}

    enforce_quality_gates({"Builtin.Helpfulness": variant}, {"Builtin.Helpfulness": 0.7})


def test_enforce_quality_gates_accepts_non_significant_result_when_significance_not_required():
    variant = {"mean": 0.9, "isSignificant": False, "absoluteChange": 0.1}

    enforce_quality_gates(
        {"Builtin.Helpfulness": variant}, {"Builtin.Helpfulness": 0.7}, require_significance=False
    )


def test_enforce_quality_gates_rejects_regression_even_when_significance_not_required():
    variant = {"mean": 0.9, "isSignificant": False, "absoluteChange": -0.1}

    with pytest.raises(QualityGateFailedError):
        enforce_quality_gates(
            {"Builtin.Helpfulness": variant},
            {"Builtin.Helpfulness": 0.7},
            require_significance=False,
        )


# --- wait_for_ab_test_results ---


def test_wait_for_ab_test_results_fails_fast_when_no_sessions_score(clock, silence_print):
    client = SimpleNamespace(get_ab_test=lambda **_kwargs: {"results": {"evaluatorMetrics": []}})

    with pytest.raises(NoSessionsScoredError, match="No sessions scored"):
        wait_for_ab_test_results(
            client,
            "abtest-1",
            {"Builtin.Helpfulness": 0.7},
            timeout=1000,
            no_sessions_timeout=100,
            scoring_lag_seconds=0,
        )


def test_wait_for_ab_test_results_restarts_scoring_lag_when_new_samples_arrive(
    clock, silence_print
):
    calls = 0

    def get_ab_test(**_kwargs):
        nonlocal calls
        calls += 1
        results = copy.deepcopy(DEFAULT_RESULTS)
        # Samples are still arriving on the first two polls; the count then plateaus,
        # so the 90s scoring lag (3 polls @ 30s) must elapse from the LAST growth
        # (call 2), not from the first fully-collected poll (call 1).
        if calls <= 2:
            for metric in results["evaluatorMetrics"]:
                metric["variantResults"][0]["sampleSize"] += calls
        return {"results": results}

    client = SimpleNamespace(get_ab_test=get_ab_test)
    quality_gates = {"Builtin.Helpfulness": 0.7, "Builtin.Correctness": 0.7}

    collected = wait_for_ab_test_results(
        client, "abtest-1", quality_gates, timeout=1000, scoring_lag_seconds=90
    )

    assert calls == 5
    assert collected["Builtin.Helpfulness"]["treatmentSampleSize"] == 8


@pytest.mark.parametrize(
    ("require_significance", "waits_until_timeout"),
    [(True, True), (False, False)],
    ids=["significance-required", "significance-not-required"],
)
def test_wait_for_ab_test_results_waits_for_significance_only_when_required(
    clock, silence_print, require_significance, waits_until_timeout
):
    results = copy.deepcopy(DEFAULT_RESULTS)
    results["evaluatorMetrics"][0]["variantResults"][0]["isSignificant"] = False
    client = SimpleNamespace(get_ab_test=lambda **_kwargs: {"results": results})
    start = clock.value

    collected = wait_for_ab_test_results(
        client,
        "abtest-1",
        {"Builtin.Helpfulness": 0.7},
        timeout=300,
        scoring_lag_seconds=0,
        require_significance=require_significance,
    )

    assert collected["Builtin.Helpfulness"]["isSignificant"] is False
    assert (clock.value - start >= 300) is waits_until_timeout
