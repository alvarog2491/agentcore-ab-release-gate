"""Schemas: ActionConfig validation of the action's inputs."""

import pytest
from pydantic import ValidationError

from agentcore_release_gate.schemas import ActionConfig


def _action_config(**overrides):
    defaults = {
        "quality_gates": {"Builtin.Helpfulness": 0.7},
        "evaluation_config_id": "template_eval-abcdefghij",
        "ab_test_role_arn": "arn:aws:iam::123456789012:role/ABTestRole",
        "control_weight": 80,
        "treatment_weight": 20,
        "evaluation_timeout": 900,
        "duration_seconds": 7200,
        "scoring_lag_seconds": 120,
    }
    defaults.update(overrides)
    return ActionConfig(**defaults)


def test_action_config_accepts_a_valid_configuration():
    config = _action_config(quality_gates={"Builtin.Helpfulness": 0.7, "custom-eval-abcdefghij": 3})

    assert config.quality_gates == {"Builtin.Helpfulness": 0.7, "custom-eval-abcdefghij": 3}
    assert (config.control_weight, config.treatment_weight) == (80, 20)


@pytest.mark.parametrize(
    "gates",
    [{}, {"unknown": 1}, {"Builtin.Helpfulness": True}, {"Builtin.Helpfulness": float("nan")}],
    ids=["empty", "unknown-evaluator", "boolean", "nan"],
)
def test_action_config_rejects_invalid_quality_gates(gates):
    with pytest.raises(ValidationError, match="quality-gates"):
        _action_config(quality_gates=gates)


@pytest.mark.parametrize(
    ("control_weight", "treatment_weight"),
    [(0, 100), (100, 0), (50, 51)],
    ids=["zero-control", "zero-treatment", "sum-over-100"],
)
def test_action_config_rejects_invalid_weights(control_weight, treatment_weight):
    with pytest.raises(ValidationError, match="weight"):
        _action_config(control_weight=control_weight, treatment_weight=treatment_weight)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("evaluation_config_id", "  ", "evaluation-config-id"),
        ("ab_test_role_arn", "  ", "ab-test-role-arn"),
        ("evaluation_timeout", 0, "Evaluation timeout"),
        ("duration_seconds", 30, "observation must last"),
        ("scoring_lag_seconds", -1, "Scoring lag"),
    ],
    ids=[
        "blank-eval-config-id",
        "blank-role-arn",
        "non-positive-timeout",
        "short-duration",
        "negative-lag",
    ],
)
def test_action_config_rejects_invalid_fields(field, value, match):
    with pytest.raises(ValidationError, match=match):
        _action_config(**{field: value})
