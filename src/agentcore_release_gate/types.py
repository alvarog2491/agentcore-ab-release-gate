"""Shared type aliases for dynamic AWS and GitHub payloads."""

from typing import Any, TypeAlias, TypedDict


class EcrImageParts(TypedDict):
    """Parsed components of a validated ECR image URI."""

    account: str
    region: str
    repository: str
    tag: str | None
    digest: str | None


JsonObject: TypeAlias = dict[str, Any]
QualityGates: TypeAlias = dict[str, float]


class VariantResult(TypedDict):
    """Validated treatment metrics persisted for one evaluator."""

    mean: float
    isSignificant: bool
    absoluteChange: float | None
    percentChange: float | None
    pValue: float | None
    treatmentSampleSize: int
    controlSampleSize: int


VariantResults: TypeAlias = dict[str, VariantResult]
GitHubResponse: TypeAlias = JsonObject | list[JsonObject]
