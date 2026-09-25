"""AWS client for AgentCore Runtime, Gateway, Evaluations, A/B tests, and ECR."""

import uuid
from typing import Any, cast

import boto3
from botocore.config import Config

from agentcore_release_gate.constants import (
    AB_TEST_NAME_RANDOM_LENGTH,
    AWS_CONNECT_TIMEOUT_SECONDS,
    AWS_MAX_ATTEMPTS,
    AWS_READ_TIMEOUT_SECONDS,
)
from agentcore_release_gate.types import JsonObject
from agentcore_release_gate.utils import _parse_image

_EVAL_CONFIG_COPY_FIELDS = frozenset(
    {
        "rule",
        "dataSourceConfig",
        "evaluators",
        "insights",
        "clusteringConfig",
        "evaluationExecutionRoleArn",
        "description",
    }
)


class AwsClient:
    """Thin wrapper around the two AgentCore boto3 clients and ECR.

    Owns client creation and exposes one method per AWS operation. Contains no
    orchestration logic, no waiting, and no state — those belong in Deployment.

    Attributes:
        agentcore_control: bedrock-agentcore-control boto3 client.
        agentcore: bedrock-agentcore boto3 client.
        runtime_id: AgentCore Runtime identifier.
        gateway_id: AgentCore Gateway identifier.
        region: AWS Region used by every client in this instance.
    """

    def __init__(self, region: str, runtime_id: str, gateway_id: str) -> None:
        """Create service clients scoped to one runtime and gateway.

        Args:
            region: AWS Region containing the AgentCore resources and ECR image.
            runtime_id: Identifier of the AgentCore Runtime being deployed.
            gateway_id: Identifier of the Gateway that routes experiment traffic.
        """
        config = Config(
            connect_timeout=AWS_CONNECT_TIMEOUT_SECONDS,
            read_timeout=AWS_READ_TIMEOUT_SECONDS,
            retries={"mode": "standard", "total_max_attempts": AWS_MAX_ATTEMPTS},
        )
        session = boto3.Session(region_name=region)
        # Typed as Any: boto3-stubs' precise per-operation TypedDicts would force every
        # call site in this module onto a rigid, request/response-specific shape, when
        # the rest of the codebase deliberately treats AWS payloads as JsonObject (see
        # schemas.py's docstring for why). boto3-stubs is still installed for editor
        # and mypy completion on `session.client(...)` itself, and for the strongly
        # typed AB-test result parsing in evaluation.py.
        self.agentcore_control: Any = session.client("bedrock-agentcore-control", config=config)
        self.agentcore: Any = session.client("bedrock-agentcore", config=config)
        self._ecr: Any = session.client("ecr")
        self.region = region
        self.runtime_id = runtime_id
        self.gateway_id = gateway_id

    # ── Runtime endpoints ────────────────────────────────────────────────────

    def get_endpoint(self, name: str) -> JsonObject:
        """Return the current configuration and status for a named runtime endpoint.

        Args:
            name: Runtime endpoint name, such as ``control`` or ``treatment``.

        Returns:
            AgentCore endpoint response payload.
        """
        return cast(
            JsonObject,
            self.agentcore_control.get_agent_runtime_endpoint(
                agentRuntimeId=self.runtime_id, endpointName=name
            ),
        )

    def update_endpoint(self, name: str, version: str) -> None:
        """Point a runtime endpoint at an existing runtime version.

        Args:
            name: Runtime endpoint to update.
            version: Runtime version that should serve through the endpoint.
        """
        self.agentcore_control.update_agent_runtime_endpoint(
            agentRuntimeId=self.runtime_id, endpointName=name, agentRuntimeVersion=version
        )

    def create_endpoint(self, name: str, version: str) -> None:
        """Create a runtime endpoint initially pointing at a runtime version.

        Args:
            name: Name for the new endpoint.
            version: Runtime version the endpoint should serve.
        """
        self.agentcore_control.create_agent_runtime_endpoint(
            agentRuntimeId=self.runtime_id, name=name, agentRuntimeVersion=version
        )

    # ── Runtime versions ─────────────────────────────────────────────────────

    def get_runtime(self, version: str) -> JsonObject:
        """Return the configuration and status for one runtime version.

        Args:
            version: AgentCore runtime version identifier.

        Returns:
            AgentCore runtime response payload.
        """
        return cast(
            JsonObject,
            self.agentcore_control.get_agent_runtime(
                agentRuntimeId=self.runtime_id, agentRuntimeVersion=version
            ),
        )

    def update_runtime(self, baseline_config: JsonObject, image: str) -> str:
        """Publish a new runtime version from a baseline config with a new container image.

        Args:
            baseline_config: Existing runtime configuration to preserve where supported.
            image: Immutable ECR image URI for the candidate container.

        Returns:
            The newly created runtime version string.
        """
        allowed = self.agentcore_control.meta.service_model.operation_model(
            "UpdateAgentRuntime"
        ).input_shape.members
        update = {key: value for key, value in baseline_config.items() if key in allowed}
        update.update(
            agentRuntimeId=self.runtime_id,
            clientToken=str(uuid.uuid4()),
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
        )
        return cast(
            str, self.agentcore_control.update_agent_runtime(**update)["agentRuntimeVersion"]
        )

    # ── Gateway ───────────────────────────────────────────────────────────────

    def get_gateway(self) -> JsonObject:
        """Return the configured Gateway's current status and metadata.

        Returns:
            AgentCore Gateway response payload.
        """
        return cast(
            JsonObject, self.agentcore_control.get_gateway(gatewayIdentifier=self.gateway_id)
        )

    def list_gateway_targets(self) -> dict[str, JsonObject]:
        """Return all Gateway targets keyed by their stable names.

        Returns:
            Gateway target summary payloads, indexed by target name.
        """
        return {
            target["name"]: target
            for page in self.agentcore_control.get_paginator("list_gateway_targets").paginate(
                gatewayIdentifier=self.gateway_id
            )
            for target in page["items"]
        }

    def get_gateway_target(self, target_id: str) -> JsonObject:
        """Return one Gateway target by its service-assigned identifier.

        Args:
            target_id: Identifier returned when the Gateway target was created.

        Returns:
            AgentCore Gateway target response payload.
        """
        return cast(
            JsonObject,
            self.agentcore_control.get_gateway_target(
                gatewayIdentifier=self.gateway_id, targetId=target_id
            ),
        )

    def create_gateway_target(self, name: str, target_config: JsonObject) -> JsonObject:
        """Create an IAM-authenticated Gateway target for a runtime endpoint.

        Args:
            name: Stable target name used by the A/B test configuration.
            target_config: AgentCore Gateway target configuration payload.

        Returns:
            Created Gateway target response payload.
        """
        return cast(
            JsonObject,
            self.agentcore_control.create_gateway_target(
                gatewayIdentifier=self.gateway_id,
                name=name,
                targetConfiguration=target_config,
                credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            ),
        )

    # ── Online evaluations ────────────────────────────────────────────────────

    def get_evaluation_config(self, config_id: str) -> JsonObject:
        """Return the reusable online-evaluation configuration template.

        Args:
            config_id: Online evaluation configuration identifier.

        Returns:
            AgentCore online-evaluation configuration response payload.
        """
        return cast(
            JsonObject,
            self.agentcore_control.get_online_evaluation_config(onlineEvaluationConfigId=config_id),
        )

    def create_evaluation_config_from(
        self, source: JsonObject, variant: str = "t"
    ) -> tuple[str, str]:
        """Create a fresh evaluation config by copying settings from a source config.

        Sampling is always forced to 100%: ephemeral configs start with no history, so
        every session in the short test window should be scored.

        Args:
            source: Reusable evaluation config whose compatible fields are copied.
            variant: Short variant marker used to keep the generated name distinct.

        Returns:
            Tuple of (onlineEvaluationConfigId, onlineEvaluationConfigArn).
        """
        kwargs: JsonObject = {k: v for k, v in source.items() if k in _EVAL_CONFIG_COPY_FIELDS}
        rule: JsonObject = {**kwargs.get("rule", {})}
        rule["samplingConfig"] = {**rule.get("samplingConfig", {}), "samplingPercentage": 100}
        kwargs["rule"] = rule
        kwargs["onlineEvaluationConfigName"] = (
            source.get("onlineEvaluationConfigName", "eval")
            + f"_{variant}_"
            + uuid.uuid4().hex[:AB_TEST_NAME_RANDOM_LENGTH]
        )
        kwargs["clientToken"] = str(uuid.uuid4())
        kwargs["enableOnCreate"] = True
        response = self.agentcore_control.create_online_evaluation_config(**kwargs)
        return response["onlineEvaluationConfigId"], response["onlineEvaluationConfigArn"]

    def delete_evaluation_config(self, config_id: str) -> None:
        """Delete an ephemeral online-evaluation configuration if it still exists.

        Args:
            config_id: Identifier of the configuration created for this experiment.
        """
        try:
            self.agentcore_control.delete_online_evaluation_config(
                onlineEvaluationConfigId=config_id
            )
        except self.agentcore_control.exceptions.ResourceNotFoundException:
            pass

    # ── A/B tests ─────────────────────────────────────────────────────────────

    def list_ab_tests(self) -> list[JsonObject]:
        """Return every A/B test visible to the configured AgentCore account.

        Returns:
            A flattened list of paginated A/B test summary payloads.
        """
        return [
            test
            for page in self.agentcore.get_paginator("list_ab_tests").paginate()
            for test in page["abTests"]
        ]

    def create_ab_test(
        self,
        gateway_arn: str,
        role_arn: str,
        variants: list[JsonObject],
        gateway_filter: JsonObject,
        evaluation_config: JsonObject,
    ) -> str:
        """Create and enable an A/B test, returning its ID.

        Args:
            gateway_arn: ARN of the Gateway that receives experiment traffic.
            role_arn: IAM role AgentCore assumes while executing the experiment.
            variants: Control and treatment target definitions and traffic weights.
            gateway_filter: Gateway routes included in the experiment.
            evaluation_config: Per-variant online-evaluation configuration.

        Returns:
            Identifier of the enabled A/B test.
        """
        response = self.agentcore.create_ab_test(
            name=f"agentcore_release_gate_{uuid.uuid4().hex[:AB_TEST_NAME_RANDOM_LENGTH]}",
            gatewayArn=gateway_arn,
            roleArn=role_arn,
            variants=variants,
            gatewayFilter=gateway_filter,
            evaluationConfig=evaluation_config,
            enableOnCreate=True,
            clientToken=str(uuid.uuid4()),
        )
        return cast(str, response["abTestId"])

    def get_ab_test(self, ab_test_id: str) -> JsonObject:
        """Return the current state and results for an A/B test.

        Args:
            ab_test_id: AgentCore A/B test identifier.

        Returns:
            AgentCore A/B test response payload.
        """
        return cast(JsonObject, self.agentcore.get_ab_test(abTestId=ab_test_id))

    def stop_ab_test(self, ab_test_id: str) -> None:
        """Stop an A/B test, returning Gateway traffic to its control target.

        Args:
            ab_test_id: Identifier of the active A/B test to stop.
        """
        self.agentcore.update_ab_test(abTestId=ab_test_id, executionStatus="STOPPED")

    # ── ECR ───────────────────────────────────────────────────────────────────

    def resolve_image(self, image: str) -> str:
        """Resolve a tagged ECR image URI to an immutable digest URI.

        Args:
            image: ECR URI using either a tag or SHA-256 digest.

        Returns:
            The input URI when already digest-pinned, otherwise its resolved digest URI.

        Raises:
            ValueError: If the image belongs to a different AWS Region.
        """
        parsed = _parse_image(image)
        if parsed["region"] != self.region:
            raise ValueError("ECR image and AgentCore must use the same AWS Region")
        if parsed["digest"]:
            return image
        result = self._ecr.describe_images(
            registryId=parsed["account"],
            repositoryName=parsed["repository"],
            imageIds=[{"imageTag": parsed["tag"]}],
        )
        digest = cast(str, result["imageDetails"][0]["imageDigest"])
        return image.rsplit(":", 1)[0] + "@" + digest
