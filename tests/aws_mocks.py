"""Hand-written AWS mocks validated against the real botocore input shapes.

Each mock method calls ``botocore.validate.validate_parameters`` on its kwargs, so a
request the real SDK would reject fails the test instead of silently passing.
"""

import copy
from types import SimpleNamespace

import boto3
from botocore.validate import validate_parameters

CONTROL_MODEL = boto3.Session()._session.get_service_model("bedrock-agentcore-control")
AB_TEST_MODEL = boto3.Session()._session.get_service_model("bedrock-agentcore")
ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-abcdefghij"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gateway-abcdefghij"
IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/agent@sha256:" + "a" * 64

DEFAULT_RESULTS = {
    "evaluatorMetrics": [
        {
            "evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/Builtin.Helpfulness",
            "controlStats": {"variantName": "C", "sampleSize": 10, "mean": 0.75},
            "variantResults": [
                {
                    "variantName": "T1",
                    "sampleSize": 8,
                    "mean": 0.8,
                    "absoluteChange": 0.05,
                    "percentChange": 6.7,
                    "pValue": 0.02,
                    "isSignificant": True,
                }
            ],
        },
        {
            "evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/Builtin.Correctness",
            "controlStats": {"variantName": "C", "sampleSize": 10, "mean": 0.85},
            "variantResults": [
                {
                    "variantName": "T1",
                    "sampleSize": 8,
                    "mean": 0.9,
                    "absoluteChange": 0.05,
                    "percentChange": 5.9,
                    "pValue": 0.01,
                    "isSignificant": True,
                }
            ],
        },
    ]
}


class MockAgentCoreControl:
    """In-memory bedrock-agentcore-control client: runtime, endpoints, gateway, eval configs."""

    def __init__(self):
        self.meta = SimpleNamespace(service_model=CONTROL_MODEL)
        self.exceptions = SimpleNamespace(ResourceNotFoundException=KeyError)
        self.endpoints = {"control": "1"}
        self.statuses = {}
        self.targets = {}
        self.events = []
        self.ephemeral_configs: dict = {}
        self.config = {
            "agentRuntimeId": "agent-abcdefghij",
            "agentRuntimeArn": ARN,
            "roleArn": "arn:aws:iam::123456789012:role/runtime",
            "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "environmentVariables": {"RELEASE_ID": "bootstrap", "KEEP": "value"},
            "status": "READY",
        }

    def validate(self, operation, kwargs):
        validate_parameters(kwargs, CONTROL_MODEL.operation_model(operation).input_shape)

    def get_agent_runtime_endpoint(self, **kwargs):
        self.validate("GetAgentRuntimeEndpoint", kwargs)
        return {
            "status": self.statuses.get(kwargs["endpointName"], "READY"),
            "liveVersion": self.endpoints[kwargs["endpointName"]],
        }

    def create_agent_runtime_endpoint(self, **kwargs):
        self.validate("CreateAgentRuntimeEndpoint", kwargs)
        self.endpoints[kwargs["name"]] = kwargs["agentRuntimeVersion"]

    def update_agent_runtime_endpoint(self, **kwargs):
        self.validate("UpdateAgentRuntimeEndpoint", kwargs)
        self.statuses[kwargs["endpointName"]] = "READY"
        self.endpoints[kwargs["endpointName"]] = kwargs["agentRuntimeVersion"]
        self.events.append((kwargs["endpointName"], kwargs["agentRuntimeVersion"]))

    def get_agent_runtime(self, **kwargs):
        self.validate("GetAgentRuntime", kwargs)
        return copy.deepcopy(self.config)

    def update_agent_runtime(self, **kwargs):
        self.validate("UpdateAgentRuntime", kwargs)
        self.update = kwargs
        self.events.append(("runtime", "2"))
        return {"agentRuntimeVersion": "2"}

    def get_gateway(self, **kwargs):
        self.validate("GetGateway", kwargs)
        return {
            "status": "READY",
            "gatewayArn": GATEWAY_ARN,
            "gatewayUrl": "https://gateway-abcdefghij.gateway.bedrock-agentcore.us-east-1.amazonaws.com",
        }

    def get_paginator(self, operation):
        page = {"items": [{"name": name, "targetId": name} for name in self.targets]}
        return SimpleNamespace(paginate=lambda **_kwargs: [page])

    def create_gateway_target(self, **kwargs):
        self.validate("CreateGatewayTarget", kwargs)
        name = kwargs["name"]
        self.targets[name] = {
            "targetId": name,
            "status": "READY",
            "targetConfiguration": kwargs["targetConfiguration"],
        }
        return copy.deepcopy(self.targets[name])

    def get_gateway_target(self, **kwargs):
        self.validate("GetGatewayTarget", kwargs)
        return copy.deepcopy(self.targets[kwargs["targetId"]])

    def get_online_evaluation_config(self, **kwargs):
        self.validate("GetOnlineEvaluationConfig", kwargs)
        config_id = kwargs["onlineEvaluationConfigId"]
        return {
            "onlineEvaluationConfigArn": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:online-evaluation-config/"
                + config_id
            ),
            "onlineEvaluationConfigId": config_id,
            "onlineEvaluationConfigName": "control-eval",
            "status": "ACTIVE",
            "evaluationExecutionRoleArn": "arn:aws:iam::123456789012:role/EvalRole",
            "rule": {"samplingConfig": {"samplingPercentage": 50.0}},
            "dataSourceConfig": {
                "cloudWatchLogs": {
                    "logGroupNames": ["/aws/bedrock-agentcore/runtimes/runtime123-control"],
                    "serviceNames": ["/aws/bedrock-agentcore/control"],
                }
            },
        }

    def create_online_evaluation_config(self, **kwargs):
        self.validate("CreateOnlineEvaluationConfig", kwargs)
        config_id = f"eval-ephemeral-{len(self.ephemeral_configs)}"
        arn = (
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:online-evaluation-config/" + config_id
        )
        self.ephemeral_configs[config_id] = kwargs
        return {
            "onlineEvaluationConfigId": config_id,
            "onlineEvaluationConfigArn": arn,
            "status": "ACTIVE",
            "executionStatus": "RUNNING",
        }

    def delete_online_evaluation_config(self, **kwargs):
        self.validate("DeleteOnlineEvaluationConfig", kwargs)
        config_id = kwargs["onlineEvaluationConfigId"]
        arn = (
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:online-evaluation-config/" + config_id
        )
        self.ephemeral_configs.pop(config_id, None)
        return {
            "onlineEvaluationConfigId": config_id,
            "onlineEvaluationConfigArn": arn,
            "status": "DELETING",
        }


class ConflictException(Exception):
    """Stand-in for the real bedrock-agentcore ConflictException."""


class MockAgentCore:
    """In-memory bedrock-agentcore (data-plane) client: A/B tests and their results."""

    def __init__(self):
        self.exceptions = SimpleNamespace(
            ResourceNotFoundException=KeyError, ConflictException=ConflictException
        )
        self.tests = {}
        self.events = []
        self.results = copy.deepcopy(DEFAULT_RESULTS)

    def validate(self, operation, kwargs):
        validate_parameters(kwargs, AB_TEST_MODEL.operation_model(operation).input_shape)

    def create_ab_test(self, **kwargs):
        self.validate("CreateABTest", kwargs)
        ab_test_id = "abtest-" + str(len(self.tests) + 1)
        arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:ab-test/" + ab_test_id
        self.tests[ab_test_id] = {
            "abTestId": ab_test_id,
            "abTestArn": arn,
            "gatewayArn": kwargs["gatewayArn"],
            "variants": kwargs["variants"],
            "status": "ACTIVE",
            "executionStatus": "RUNNING" if kwargs.get("enableOnCreate") else "NOT_STARTED",
        }
        self.events.append(("create", ab_test_id))
        return {
            "abTestId": ab_test_id,
            "abTestArn": arn,
            "name": kwargs["name"],
            "status": "ACTIVE",
            "executionStatus": self.tests[ab_test_id]["executionStatus"],
        }

    def get_ab_test(self, **kwargs):
        self.validate("GetABTest", kwargs)
        test = copy.deepcopy(self.tests[kwargs["abTestId"]])
        test["results"] = copy.deepcopy(self.results)
        return test

    def update_ab_test(self, **kwargs):
        self.validate("UpdateABTest", kwargs)
        test = self.tests[kwargs["abTestId"]]
        if "executionStatus" in kwargs:
            test["executionStatus"] = kwargs["executionStatus"]
        self.events.append(("update", kwargs["abTestId"], kwargs.get("executionStatus")))
        return {
            "abTestId": kwargs["abTestId"],
            "abTestArn": test["abTestArn"],
            "status": test["status"],
            "executionStatus": test["executionStatus"],
        }

    def delete_ab_test(self, **kwargs):
        self.validate("DeleteABTest", kwargs)
        test = self.tests.pop(kwargs["abTestId"])
        self.events.append(("delete", kwargs["abTestId"]))
        return {
            "abTestId": kwargs["abTestId"],
            "abTestArn": test["abTestArn"],
            "status": "DELETING",
        }

    def get_paginator(self, operation):
        assert operation == "list_ab_tests"
        page = {
            "abTests": [
                {
                    "abTestId": test["abTestId"],
                    "abTestArn": test["abTestArn"],
                    "status": test["status"],
                    "executionStatus": test["executionStatus"],
                    "gatewayArn": test["gatewayArn"],
                }
                for test in self.tests.values()
            ]
        }
        return SimpleNamespace(paginate=lambda **_kwargs: [page])
