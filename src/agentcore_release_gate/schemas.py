"""Pydantic models that validate data at the two boundaries where it enters this action
untyped: action inputs (environment variables) and AgentCore's `GetABTest` JSON response.

AWS resource payloads read elsewhere (endpoints, gateways, targets, runtimes) stay as
plain dicts validated by `wait_for`'s status/version checks and by the tests' schema-
validated fakes; forcing every heterogeneous AWS response shape through a model here
would add a large discriminated-union surface for no corresponding safety gain, since
those payloads are already service-validated and consumed narrowly.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from agentcore_release_gate.constants import (
    EVALUATOR_ID_SUFFIX_LENGTH,
    MAX_EVALUATOR_NAME_PREFIX_LENGTH,
    MAXIMUM_VARIANT_WEIGHT,
    MINIMUM_OBSERVATION_SECONDS,
    MINIMUM_VARIANT_WEIGHT,
    TOTAL_TRAFFIC_WEIGHT,
)

EVALUATOR_ID_PATTERN = re.compile(
    r"(?:Builtin\.[a-zA-Z0-9._-]+|ThirdParty\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"
    rf"|[a-zA-Z][a-zA-Z0-9-_]{{0,{MAX_EVALUATOR_NAME_PREFIX_LENGTH - 1}}}"
    rf"-[a-zA-Z0-9]{{{EVALUATOR_ID_SUFFIX_LENGTH}}})"
)


class ActionConfig(BaseModel):
    """Validated deployment configuration assembled from the action's environment variables.

    Callers are expected to do only string-to-native-type conversion (int(), strip(),
    JSON decoding) before constructing this model; every semantic rule -- ranges,
    non-blank requirements, evaluator ID shape, and cross-field consistency -- lives
    here as a single, declarative, independently testable contract.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    quality_gates: dict[str, float]
    require_significance: bool = True
    control_endpoint_name: str = "control"
    evaluation_config_id: str
    ab_test_role_arn: str
    control_weight: int
    treatment_weight: int
    evaluation_timeout: int
    duration_seconds: int
    scoring_lag_seconds: int

    @field_validator("quality_gates", mode="before")
    @classmethod
    def _validate_quality_gates(cls, value: object) -> object:
        """Require a non-empty map of known evaluator IDs to finite numeric scores."""
        # mode="before": inspect raw values ahead of Pydantic's own dict[str, float]
        # coercion, which would otherwise silently turn a bool score into 0.0/1.0.
        if (
            not isinstance(value, dict)
            or not value
            or any(
                not isinstance(name, str)
                or not EVALUATOR_ID_PATTERN.fullmatch(name)
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                for name, score in value.items()
            )
        ):
            raise ValueError(
                "quality-gates must be a non-empty JSON object of evaluator IDs and "
                "numeric minimum scores"
            )
        return value

    @field_validator("evaluation_config_id", "ab_test_role_arn")
    @classmethod
    def _require_non_blank(cls, value: str, info: ValidationInfo) -> str:
        """Reject empty (or whitespace-only, after stripping) required string inputs."""
        if not value:
            field = (info.field_name or "value").replace("_", "-")
            raise ValueError(f"{field} must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_weights(self) -> ActionConfig:
        """Require each traffic weight in 1-99 and both to add up to 100."""
        if not (
            MINIMUM_VARIANT_WEIGHT <= self.control_weight <= MAXIMUM_VARIANT_WEIGHT
            and MINIMUM_VARIANT_WEIGHT <= self.treatment_weight <= MAXIMUM_VARIANT_WEIGHT
        ):
            raise ValueError("control-weight and treatment-weight must each be between 1 and 99")
        if self.control_weight + self.treatment_weight != TOTAL_TRAFFIC_WEIGHT:
            raise ValueError("control-weight and treatment-weight must add up to 100")
        return self

    @model_validator(mode="after")
    def _validate_timing(self) -> ActionConfig:
        """Require a positive timeout, a minimum observation window, and a non-negative lag."""
        if self.evaluation_timeout <= 0 or self.duration_seconds < MINIMUM_OBSERVATION_SECONDS:
            raise ValueError(
                "Evaluation timeout must be positive and observation must last at least 60 seconds"
            )
        if self.scoring_lag_seconds < 0:
            raise ValueError("Scoring lag seconds must be non-negative")
        return self


class ControlStats(BaseModel):
    """Control-variant sample statistics for one evaluator, as returned by GetABTest."""

    model_config = ConfigDict(extra="ignore")

    variantName: str
    sampleSize: int = 0
    mean: float | None = None


class VariantMetric(BaseModel):
    """One variant's scored metrics for a single evaluator, as returned by GetABTest.

    `mean` is validated eagerly: AgentCore should never report a non-finite, boolean,
    or non-numeric mean for a variant that already has a sample size, so a malformed
    value here indicates corrupt upstream data worth failing loudly on rather than
    silently coercing (a bare `float` field would let Pydantic's lax mode coerce
    `True` to `1.0`, defeating that fail-closed contract).
    """

    model_config = ConfigDict(extra="ignore")

    variantName: str
    sampleSize: int = 0
    mean: float | None = None
    isSignificant: bool = False
    absoluteChange: float | None = None
    percentChange: float | None = None
    pValue: float | None = None

    @field_validator("mean", mode="before")
    @classmethod
    def _reject_invalid_mean(cls, value: object) -> object:
        """Reject boolean, non-numeric, or non-finite mean scores instead of coercing them."""
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("invalid mean score")
        return value


class EvaluatorMetric(BaseModel):
    """One evaluator's control and variant metrics for a single GetABTest poll."""

    model_config = ConfigDict(extra="ignore")

    evaluatorArn: str
    controlStats: ControlStats
    variantResults: list[VariantMetric] = Field(default_factory=list)
