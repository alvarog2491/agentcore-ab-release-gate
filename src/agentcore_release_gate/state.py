"""The recovery journal (`state.json`) that lets a deployment be resumed or rolled back."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from agentcore_release_gate.exceptions import StateJournalError
from agentcore_release_gate.types import QualityGates, VariantResults


@dataclass(frozen=True)
class DeploymentState:
    """Everything a later job needs to promote, roll back, or report on a deployment.

    Fields are filled in as the deployment progresses, so every field is optional and
    an all-default instance means no deployment has started yet.
    """

    baseline: str | None = None
    runtime_arn: str | None = None
    gateway_arn: str | None = None
    control_endpoint_name: str | None = None
    quality_gates: QualityGates = field(default_factory=dict)
    require_significance: bool = True
    version: str | None = None
    image: str | None = None
    control_evaluation_config_arn: str | None = None
    ephemeral_control_config_id: str | None = None
    treatment_evaluation_config_arn: str | None = None
    ephemeral_treatment_config_id: str | None = None
    ab_test_id: str | None = None
    variant_results: VariantResults = field(default_factory=dict)
    ready_to_promote: bool = False
    promoting: bool = False
    finished: Literal["promoted", "rolled_back"] | None = None


def load_state(path: Path) -> DeploymentState:
    """Read the recovery journal, or return an empty state when none exists yet.

    Raises:
        StateJournalError: If the journal exists but is not a JSON object of known fields.
    """
    if not path.exists():
        return DeploymentState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateJournalError(f"Recovery journal {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise StateJournalError(f"Recovery journal {path} must contain a JSON object")
    try:
        return DeploymentState(**data)
    except TypeError as error:
        raise StateJournalError(f"Recovery journal {path} has unknown fields") from error


def save_state(path: Path, state: DeploymentState) -> None:
    """Atomically write the recovery journal.

    Writes to a sibling .tmp file first, then renames it over the journal so a
    mid-write crash never leaves a partial file.
    """
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(state)), encoding="utf-8")
    temporary.replace(path)
