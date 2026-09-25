"""Orchestrate candidate deployment, observation, promotion, and recovery."""

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from agentcore_release_gate.aws_client import AwsClient
from agentcore_release_gate.constants import (
    DEFAULT_AWS_WAIT_TIMEOUT_SECONDS,
    DEFAULT_CONTROL_WEIGHT,
    DEFAULT_TREATMENT_WEIGHT,
    MINIMUM_OBSERVATION_SECONDS,
    OBSERVATION_LOG_INTERVAL_SECONDS,
    SCORING_LAG_SECONDS,
)
from agentcore_release_gate.evaluation import enforce_quality_gates, wait_for_ab_test_results
from agentcore_release_gate.types import JsonObject, QualityGates
from agentcore_release_gate.utils import wait_for


class Deployment:
    """Orchestrate recovery-safe AgentCore deployment operations.

    Attributes:
        state: Persisted recovery journal for the current deployment.
        aws: AWS client for all AgentCore and ECR API calls.
    """

    def __init__(
        self,
        state_file: str,
        *,
        quality_gates: QualityGates | None = None,
        require_significance: bool = True,
        control_endpoint_name: str = "control",
        evaluation_config_template: str = "",
        ab_test_role_arn: str = "",
        control_weight: int = DEFAULT_CONTROL_WEIGHT,
        treatment_weight: int = DEFAULT_TREATMENT_WEIGHT,
        evaluation_timeout: int = DEFAULT_AWS_WAIT_TIMEOUT_SECONDS,
        scoring_lag_seconds: int = SCORING_LAG_SECONDS,
    ) -> None:
        """Initialize a deployment coordinator and resume any saved recovery state.

        Args:
            state_file: JSON journal used to recover from failure or cancellation.
            quality_gates: Minimum score required for each configured evaluator.
            require_significance: When True (the default) an evaluator must be
                statistically significant to pass. When False only the minimum
                score and regression checks apply.
            control_endpoint_name: Stable endpoint that serves the approved runtime.
            evaluation_config_template: Evaluation config copied for each experiment.
            ab_test_role_arn: IAM role AgentCore assumes to run the A/B test.
            control_weight: Percentage of experiment traffic sent to control.
            treatment_weight: Percentage of experiment traffic sent to treatment.
            evaluation_timeout: Maximum time to wait for evaluator results.
            scoring_lag_seconds: Required quiet period after scores stop arriving.
        """
        self.quality_gates: QualityGates = quality_gates or {}
        self.require_significance = require_significance
        self.control_endpoint_name = control_endpoint_name
        self.evaluation_config_template = evaluation_config_template
        self.ab_test_role_arn = ab_test_role_arn
        self.control_weight = control_weight
        self.treatment_weight = treatment_weight
        self.evaluation_timeout = evaluation_timeout
        self.scoring_lag_seconds = scoring_lag_seconds
        self.path = Path(state_file)
        self.state: JsonObject = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.aws = AwsClient(
            region=os.environ["AWS_REGION"],
            runtime_id=os.environ["RUNTIME_ID"],
            gateway_id=os.environ["GATEWAY_ID"],
        )

    def _log(self, event: str, **details: Any) -> None:
        """Print one structured JSON log line for a deployment event."""
        print(json.dumps({"event": event, **details}), flush=True)

    def _checkpoint(self, **changes: Any) -> None:
        """Merge changes into the in-memory state and flush to the JSON recovery journal.

        Writes to a sibling .tmp file first, then renames it over the journal so a
        mid-write crash never leaves a partial file. The always() cleanup step reads
        this file to restore the baseline when the deployment fails or is cancelled.

        Args:
            **changes: Key-value pairs to merge into the current state.
        """
        updated_state = {**self.state, **changes}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated_state), encoding="utf-8")
        temporary.replace(self.path)
        self.state = updated_state

    def _point(self, name: str, version: str) -> None:
        """Point an endpoint at a runtime version and wait for readiness."""
        current = wait_for(lambda: self.aws.get_endpoint(name), ("READY", "UPDATE_FAILED"))
        if current["status"] != "READY" or current["liveVersion"] != version:
            self.aws.update_endpoint(name, version)
            wait_for(lambda: self.aws.get_endpoint(name), "READY", version)

    def _active_ab_test(self) -> JsonObject | None:
        """Return the running or paused A/B test on the configured gateway, or None."""
        for test in self.aws.list_ab_tests():
            if test["gatewayArn"] == self.state["gateway_arn"] and test["executionStatus"] in (
                "RUNNING",
                "PAUSED",
            ):
                return test
        return None

    def _prepare(self) -> JsonObject:
        """Capture the baseline and ensure compatible treatment targets exist.

        Returns:
            The baseline runtime configuration used to create the candidate.

        Raises:
            RuntimeError: If another A/B test is active on the gateway.
        """
        self._log("deployment-preparing", controlEndpoint=self.control_endpoint_name)
        baseline = wait_for(lambda: self.aws.get_endpoint(self.control_endpoint_name), "READY")[
            "liveVersion"
        ]
        config = self.aws.get_runtime(baseline)
        gateway = wait_for(lambda: self.aws.get_gateway(), "READY")
        # Record the baseline before any serving-state changes, for the always() cleanup step.
        self._checkpoint(
            baseline=baseline,
            runtime_arn=config["agentRuntimeArn"],
            gateway_arn=gateway["gatewayArn"],
        )
        if self._active_ab_test() is not None:
            raise RuntimeError(
                "An AgentCore A/B test is still active on this Gateway; recover it first"
            )
        self._ensure_targets()
        self._log(
            "deployment-prepared",
            baseline=baseline,
            runtime=self.aws.runtime_id,
            gateway=self.aws.gateway_id,
        )
        return config

    def _ensure_targets(self) -> None:
        """Create missing treatment resources and validate existing targets.

        Raises:
            ValueError: If an existing target points at an incompatible runtime endpoint.
        """
        try:
            self.aws.get_endpoint("treatment")
            self._log("treatment-endpoint-existing", endpoint="treatment")
        except self.aws.agentcore_control.exceptions.ResourceNotFoundException:
            self._log(
                "treatment-endpoint-creating",
                endpoint="treatment",
                version=self.state["baseline"],
            )
            self.aws.create_endpoint("treatment", self.state["baseline"])
        wait_for(lambda: self.aws.get_endpoint("treatment"), "READY")
        self._log("treatment-endpoint-ready", endpoint="treatment")

        targets = self.aws.list_gateway_targets()
        for name in (self.control_endpoint_name, "treatment"):
            desired = {
                "http": {
                    "agentcoreRuntime": {
                        "arn": self.state["runtime_arn"],
                        "qualifier": name,
                    }
                }
            }
            if name in targets:
                self._log("gateway-target-existing", target=name)
                target = self.aws.get_gateway_target(targets[name]["targetId"])
                if target["targetConfiguration"] != desired:
                    raise ValueError(
                        "Existing gateway target does not match runtime/endpoint: " + name
                    )
            else:
                self._log("gateway-target-creating", target=name)
                target = self.aws.create_gateway_target(name, desired)
            wait_for(
                lambda: self.aws.get_gateway_target(target["targetId"]),
                "READY",
            )
            self._log("gateway-target-ready", target=name, targetId=target["targetId"])

    def _eval_config_for_variant(self, template: JsonObject, variant_name: str) -> JsonObject:
        """Return template with data source names pointing at the given variant endpoint.

        Replaces occurrences of the control endpoint name with `variant_name` in both
        serviceNames and logGroupNames so the evaluator reads the correct log group and
        stream for each variant (e.g. …-control → …-treatment).
        """
        cw = (template.get("dataSourceConfig") or {}).get("cloudWatchLogs") or {}
        service_names = cw.get("serviceNames") or []
        log_group_names = cw.get("logGroupNames") or []
        patched_services = [
            n.replace(self.control_endpoint_name, variant_name) for n in service_names
        ]
        patched_groups = [
            n.replace(self.control_endpoint_name, variant_name) for n in log_group_names
        ]
        if patched_services == service_names and patched_groups == log_group_names:
            return template
        return {
            **template,
            "dataSourceConfig": {
                **template["dataSourceConfig"],
                "cloudWatchLogs": {
                    **cw,
                    "serviceNames": patched_services,
                    "logGroupNames": patched_groups,
                },
            },
        }

    def _create_and_activate_eval_config(
        self, template: JsonObject, endpoint_name: str, variant_label: str, variant_key: str
    ) -> tuple[str, str]:
        """Create an ephemeral evaluation config for one variant and wait until active.

        Checkpoints the new config immediately after creation so rollback can delete
        it even if the subsequent wait is interrupted.

        Args:
            template: Evaluation config template to copy variant-specific settings from.
            endpoint_name: Runtime endpoint the variant's data source should read from.
            variant_label: "control" or "treatment"; used for log events and state keys.
            variant_key: Short marker AWS uses to keep the generated config name distinct.

        Returns:
            Tuple of (onlineEvaluationConfigId, onlineEvaluationConfigArn).
        """
        self._log("evaluation-config-creating", variant=variant_label)
        config_id, config_arn = self.aws.create_evaluation_config_from(
            self._eval_config_for_variant(template, endpoint_name), variant=variant_key
        )
        self._log("evaluation-config-created", variant=variant_label, evaluationConfigId=config_id)
        self._checkpoint(
            **{
                f"{variant_label}_evaluation_config_arn": config_arn,
                f"ephemeral_{variant_label}_config_id": config_id,
            }
        )
        wait_for(
            lambda: self.aws.get_evaluation_config(config_id),
            "ACTIVE",
            on_poll=lambda r: self._log(
                "evaluation-config-waiting",
                variant=variant_label,
                evaluationConfigId=config_id,
                status=r.get("status", "UNKNOWN"),
            ),
        )
        self._log("evaluation-config-ready", variant=variant_label, evaluationConfigId=config_id)
        return config_id, config_arn

    def _start_ab_test(self) -> None:
        """Create and start the native A/B test that splits and scores traffic."""
        self._log(
            "evaluation-config-template-loading",
            evaluationConfigId=self.evaluation_config_template,
        )
        template = self.aws.get_evaluation_config(self.evaluation_config_template)
        control_id, control_arn = self._create_and_activate_eval_config(
            template, self.control_endpoint_name, "control", "c"
        )
        treatment_id, treatment_arn = self._create_and_activate_eval_config(
            template, "treatment", "treatment", "t"
        )
        self._log(
            "ab-test-creating",
            controlWeight=self.control_weight,
            treatmentWeight=self.treatment_weight,
            gatewayArn=self.state["gateway_arn"],
        )
        ab_test_id = self.aws.create_ab_test(
            gateway_arn=self.state["gateway_arn"],
            role_arn=self.ab_test_role_arn,
            # Variant names are fixed by the service to "C" (control) and "T1" (treatment);
            # the Gateway target each variant serves is named separately, in `target.name`.
            variants=[
                {
                    "name": "C",
                    "weight": self.control_weight,
                    "variantConfiguration": {"target": {"name": self.control_endpoint_name}},
                },
                {
                    "name": "T1",
                    "weight": self.treatment_weight,
                    "variantConfiguration": {"target": {"name": "treatment"}},
                },
            ],
            gateway_filter={"targetPaths": [f"/{self.control_endpoint_name}/*"]},
            evaluation_config={
                "perVariantOnlineEvaluationConfig": [
                    {
                        "name": "C",
                        "onlineEvaluationConfigArn": self.state["control_evaluation_config_arn"],
                    },
                    {
                        "name": "T1",
                        "onlineEvaluationConfigArn": self.state["treatment_evaluation_config_arn"],
                    },
                ]
            },
        )
        # Checkpoint before waiting so a crash mid-wait still leaves the AB test recoverable.
        self._checkpoint(ab_test_id=ab_test_id)
        self._log("ab-test-created", abTestId=ab_test_id)
        wait_for(
            lambda: self.aws.get_ab_test(ab_test_id),
            "ACTIVE",
            execution_status="RUNNING",
        )
        self._log("ab-test-running", abTestId=ab_test_id)

    def _delete_ephemeral_configs(self) -> None:
        """Delete both ephemeral evaluation configs (control and treatment) created for this run."""
        for key in ("ephemeral_control_config_id", "ephemeral_treatment_config_id"):
            config_id = self.state.get(key)
            if not config_id:
                continue
            try:
                self.aws.delete_evaluation_config(config_id)
            except (ClientError, BotoCoreError) as exc:
                # Best-effort cleanup: an AWS-side failure here must not block promotion
                # or rollback. A bug in our own code should still surface, so only AWS's
                # own exception hierarchy is swallowed.
                print(
                    f"Warning: could not delete ephemeral config {config_id}: {exc}",
                    flush=True,
                )
            self._checkpoint(**{key: None})

    def _stop_ab_test(self) -> None:
        """Stop the A/B test while tolerating a test that is already gone.

        Left stopped rather than deleted so its results (evaluator means,
        significance, sample counts) stay queryable via GetABTest for later
        review. active_ab_test() only guards against RUNNING/PAUSED tests, so
        a stopped-but-undeleted test never blocks the next run.
        """
        ab_test_id = self.state.get("ab_test_id")
        if not ab_test_id:
            return
        try:
            self.aws.stop_ab_test(ab_test_id)
            wait_for(
                lambda: self.aws.get_ab_test(ab_test_id),
                "ACTIVE",
                execution_status="STOPPED",
            )
        except (
            self.aws.agentcore.exceptions.ResourceNotFoundException,
            self.aws.agentcore.exceptions.ConflictException,
        ):
            pass

    def _deploy_candidate(self, config: JsonObject, image: str) -> str:
        """Publish a candidate while preserving baseline runtime settings.

        Args:
            config: Baseline runtime configuration.
            image: Immutable candidate container image URI.

        Returns:
            The newly published AgentCore runtime version.
        """
        self._log("candidate-runtime-creating", image=image)
        version = self.aws.update_runtime(config, image)
        self._checkpoint(version=version, image=image)
        self._log("candidate-runtime-created", version=version)
        wait_for(lambda: self.aws.get_runtime(version), "READY")
        self._log("candidate-runtime-ready", version=version)
        self._log("treatment-endpoint-updating", endpoint="treatment", version=version)
        self._point("treatment", version)
        self._log("treatment-endpoint-serving", endpoint="treatment", version=version)
        return version

    def _observe(self, seconds: int) -> None:
        """Wait while real or manually generated traffic accrues.

        Raises:
            RuntimeError: If the AB test leaves RUNNING state before the window elapses.
        """
        ab_test_id = self.state["ab_test_id"]
        self._log(
            "listening-for-connections",
            abTestId=ab_test_id,
            observationSeconds=seconds,
            message="A/B test is running; waiting for Gateway connections and evaluator results.",
        )
        start = time.monotonic()
        while True:
            remaining = seconds - (time.monotonic() - start)
            if remaining <= 0:
                return
            time.sleep(min(OBSERVATION_LOG_INTERVAL_SECONDS, remaining))
            elapsed = int(time.monotonic() - start)
            ab_test = self.aws.get_ab_test(ab_test_id)
            execution_status = ab_test.get("executionStatus")
            if execution_status != "RUNNING":
                raise RuntimeError(
                    f"AB test left RUNNING state during observation window "
                    f"(executionStatus={execution_status!r}); failing fast"
                )
            self._log(
                "observing",
                abTestId=ab_test_id,
                elapsedSeconds=elapsed,
                remainingSeconds=max(0, seconds - elapsed),
                abTestStatus=execution_status,
            )

    def rollback(self) -> None:
        """Restore both endpoints and clean up an unfinished experiment.

        Safe to call repeatedly: a completed recovery journal is left unchanged.
        """
        if not self.state or self.state.get("finished"):
            return
        if self.state.get("promoting"):
            self._point(self.control_endpoint_name, self.state["baseline"])
        if self.state.get("version"):
            # Leaves no endpoint pointing at a candidate that failed or was cancelled.
            self._point("treatment", self.state["baseline"])
        self._stop_ab_test()
        self._delete_ephemeral_configs()
        self._checkpoint(finished="rolled_back")
        print("Rolled back: control retains version " + self.state["baseline"], flush=True)

    @contextmanager
    def _rollback_on_failure(self) -> Iterator[None]:
        """Roll back if the wrapped block raises, then re-raise the original failure."""
        try:
            yield
        except BaseException:
            try:
                self.rollback()
            except BaseException as recovery_error:
                # Preserve the original failure and leave state for the cleanup step.
                print(
                    "Rollback failed; cleanup must retry: " + str(recovery_error),
                    flush=True,
                )
            raise

    def observe_candidate(self, image: str, seconds: int) -> None:
        """Deploy and evaluate a candidate using observed traffic.

        Raises:
            BaseException: Re-raises deployment or evaluation failures after rollback.

        Args:
            image: Candidate ECR image URI; tags are resolved to immutable digests.
            seconds: Duration to collect A/B-test traffic before evaluating results.
        """
        image = self.aws.resolve_image(image)
        self._log("candidate-image-resolved", image=image, observationSeconds=seconds)
        with self._rollback_on_failure():
            config = self._prepare()
            self._checkpoint(
                quality_gates=self.quality_gates,
                require_significance=self.require_significance,
            )
            self._deploy_candidate(config, image)
            self._start_ab_test()
            self._observe(seconds)
            self._log("evaluation-results-waiting", abTestId=self.state["ab_test_id"])
            variant_results = wait_for_ab_test_results(
                self.aws.agentcore,
                self.state["ab_test_id"],
                self.quality_gates,
                self.evaluation_timeout,
                scoring_lag_seconds=self.scoring_lag_seconds,
                require_significance=self.require_significance,
            )
            self._checkpoint(quality_gates=self.quality_gates, variant_results=variant_results)
            enforce_quality_gates(
                variant_results,
                self.quality_gates,
                require_significance=self.require_significance,
            )
            self._checkpoint(ready_to_promote=True)
            self._log("quality-gates-passed", evaluators=sorted(variant_results))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"variant-results={json.dumps(variant_results)}\n")

    def promote_candidate(self) -> None:
        """Promote a candidate that passed observation and quality gates.

        Raises:
            RuntimeError: If no candidate is ready for promotion.
            BaseException: Re-raises promotion failures after rollback.
        """
        if not self.state.get("ready_to_promote"):
            raise RuntimeError("No candidate is ready to promote; run observation first")
        version = self.state["version"]
        image = self.state["image"]
        with self._rollback_on_failure():
            self._log("candidate-promotion-starting", version=version)
            self._checkpoint(promoting=True)
            # Stopping the AB test reverts all Gateway traffic to the control target
            # before the control alias is repointed, so only the approved candidate serves.
            self._stop_ab_test()
            self._point(self.control_endpoint_name, version)
            self._point("treatment", version)
            self._delete_ephemeral_configs()
            self._checkpoint(finished="promoted")
            self._log("candidate-promoted", version=version)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"runtime-version={version}\nimage-uri={image}\n")

    def run(self, image: str, seconds: int) -> None:
        """Observe and promote a candidate in one automatic operation.

        Raises:
            ValueError: If the observation window is shorter than the minimum.

        Args:
            image: Candidate ECR image URI to deploy and evaluate.
            seconds: Duration to observe experiment traffic before promotion.
        """
        if seconds < MINIMUM_OBSERVATION_SECONDS:
            raise ValueError("Observation must last at least 60 seconds")
        self.observe_candidate(image, seconds)
        self.promote_candidate()
