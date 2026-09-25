"""Verify deployment decisions and recovery using SDK-validated AWS fakes."""

import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
from pydantic import ValidationError

ACTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACTION))
import main as cli
from agentcore_release_gate import aws_client as ab_aws
from agentcore_release_gate import deployment as ab
from agentcore_release_gate.evaluation import (
    _collect_variant_results,
    enforce_quality_gates,
    wait_for_ab_test_results,
)
from agentcore_release_gate.report import COMMENT_MARKER
from agentcore_release_gate.schemas import ActionConfig
from agentcore_release_gate.utils import _parse_image, wait_for


@pytest.mark.parametrize(
    "score",
    [float("nan"), float("inf"), True, "0.9", None],
    ids=["nan", "infinity", "boolean", "string", "none"],
)
def test_invalid_managed_scores_fail_closed(score):
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

    with pytest.raises(ValueError, match="invalid mean score"):
        _collect_variant_results(results, {"custom-eval-abcdefghij": 3})


def test_collect_variant_results_excludes_zero_sample_evaluator():
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


def test_collect_variant_results_ignores_invalid_mean_on_unrelated_evaluator():
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


@pytest.mark.parametrize(
    "variant",
    [
        {"mean": 0.9, "isSignificant": False, "absoluteChange": 0.1},
        {"mean": 0.9, "isSignificant": True, "absoluteChange": -0.1},
    ],
    ids=["not-significant", "regression"],
)
def test_gate_rejects_invalid_results(variant):
    with pytest.raises(ValueError, match="quality gates failed"):
        enforce_quality_gates({"Builtin.Helpfulness": variant}, {"Builtin.Helpfulness": 0.7})


def test_gate_accepts_significant_non_negative_change():
    variant = {"mean": 0.9, "isSignificant": True, "absoluteChange": 0.1}

    enforce_quality_gates({"Builtin.Helpfulness": variant}, {"Builtin.Helpfulness": 0.7})


def test_gate_accepts_non_significant_when_significance_not_required():
    variant = {"mean": 0.9, "isSignificant": False, "absoluteChange": 0.1}

    enforce_quality_gates(
        {"Builtin.Helpfulness": variant}, {"Builtin.Helpfulness": 0.7}, require_significance=False
    )


def test_gate_still_rejects_regression_when_significance_not_required():
    variant = {"mean": 0.9, "isSignificant": False, "absoluteChange": -0.1}

    with pytest.raises(ValueError, match="quality gates failed"):
        enforce_quality_gates(
            {"Builtin.Helpfulness": variant},
            {"Builtin.Helpfulness": 0.7},
            require_significance=False,
        )


@pytest.mark.parametrize(
    "image",
    ["ghcr.io/org/agent:main", "docker.io/org/agent:v1", "org/agent:latest"],
    ids=["ghcr", "docker-hub", "implicit-docker-hub"],
)
def test_non_ecr_registries_fail_with_actionable_message(image):
    with pytest.raises(ValueError, match="ECR"):
        _parse_image(image)


def test_ecr_tag_and_digest():
    base = "123456789012.dkr.ecr.us-east-1.amazonaws.com/team/agent"

    assert _parse_image(base + ":v1")["tag"] == "v1"
    assert _parse_image(base + "@sha256:" + "a" * 64)["digest"] == "sha256:" + "a" * 64


MODEL = boto3.Session()._session.get_service_model("bedrock-agentcore-control")
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


class Clock:
    value = 100000

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Api:
    def __init__(self):
        self.meta = SimpleNamespace(service_model=MODEL)
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
        validate_parameters(kwargs, MODEL.operation_model(operation).input_shape)

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


class _ConflictException(Exception):
    """Stand-in for the real bedrock-agentcore ConflictException."""


class AbTestApi:
    def __init__(self):
        self.exceptions = SimpleNamespace(
            ResourceNotFoundException=KeyError, ConflictException=_ConflictException
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


def test_interrupted_signal_handler_raises_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt, match="attempting rollback"):
        cli._interrupted(None, None)


def test_wait_for_raises_on_failed_status():
    with pytest.raises(RuntimeError, match="failed to become ready"):
        wait_for(lambda: {"status": "UPDATE_FAILED"}, "READY")


def test_wait_for_raises_on_error_status_case_insensitively():
    with pytest.raises(RuntimeError, match="failed to become ready"):
        wait_for(lambda: {"status": "SomeError"}, "READY")


def test_wait_for_invokes_on_poll_callback_while_waiting(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ab.time, "monotonic", clock.now)
    monkeypatch.setattr(ab.time, "sleep", clock.sleep)
    statuses = iter(["PENDING", "PENDING", "READY"])
    polled = []

    wait_for(
        lambda: {"status": next(statuses)},
        "READY",
        on_poll=lambda result: polled.append(result["status"]),
    )

    assert polled == ["PENDING", "PENDING"]


def test_wait_for_times_out_when_status_never_reached(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ab.time, "monotonic", clock.now)
    monkeypatch.setattr(ab.time, "sleep", clock.sleep)

    with pytest.raises(TimeoutError, match="Timed out waiting for AWS readiness"):
        wait_for(lambda: {"status": "PENDING"}, "READY", timeout=25)


def test_wait_for_ab_test_results_fails_fast_when_no_sessions_score(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ab.time, "monotonic", clock.now)
    monkeypatch.setattr(ab.time, "sleep", clock.sleep)
    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: None)
    client = SimpleNamespace(get_ab_test=lambda **_kwargs: {"results": {"evaluatorMetrics": []}})

    with pytest.raises(TimeoutError, match="No sessions scored"):
        wait_for_ab_test_results(
            client,
            "abtest-1",
            {"Builtin.Helpfulness": 0.7},
            timeout=1000,
            no_sessions_timeout=100,
            scoring_lag_seconds=0,
        )


def test_wait_for_ab_test_results_scoring_lag_resets_on_new_samples(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ab.time, "monotonic", clock.now)
    monkeypatch.setattr(ab.time, "sleep", clock.sleep)
    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: None)
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


class TestDeployment:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        self.deployment = ab.Deployment.__new__(ab.Deployment)
        self.deployment.path = tmp_path / "state.json"
        self.deployment.state = {}
        self.deployment.quality_gates = {"Builtin.Helpfulness": 0.7, "Builtin.Correctness": 0.7}
        self.deployment.require_significance = True
        self.deployment.control_endpoint_name = "control"
        self.deployment.evaluation_config_template = "template_eval-abcdefghij"
        self.deployment.ab_test_role_arn = "arn:aws:iam::123456789012:role/ABTestRole"
        self.deployment.control_weight = 80
        self.deployment.treatment_weight = 20
        self.deployment.evaluation_timeout = 60
        self.deployment.scoring_lag_seconds = 0
        aws = ab_aws.AwsClient.__new__(ab_aws.AwsClient)
        aws.agentcore_control = Api()
        aws.agentcore = AbTestApi()
        aws._ecr = SimpleNamespace()
        aws.runtime_id = "agent-abcdefghij"
        aws.gateway_id = "gateway-abcdefghij"
        aws.region = "us-east-1"
        self.deployment.aws = aws
        self.clock = Clock()
        monkeypatch.setattr(ab.time, "monotonic", self.clock.now)
        monkeypatch.setattr(ab.time, "time", self.clock.now)
        monkeypatch.setattr(ab.time, "sleep", self.clock.sleep)
        monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: None)

    def test_full_two_hour_gate_promotes_the_same_version(self):
        self.deployment.run(IMAGE, 7200)
        assert self.clock.value - 100000 >= 7200
        assert self.deployment.aws.agentcore_control.endpoints == {"control": "2", "treatment": "2"}
        ab_test_id = self.deployment.state["ab_test_id"]
        assert self.deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"
        assert self.deployment.state["finished"] == "promoted"
        assert ("control", "2") in self.deployment.aws.agentcore_control.events

    def test_cleanup_requires_no_evaluation_inputs(self, monkeypatch):
        self.deployment._prepare()
        session = SimpleNamespace(
            client=lambda *_args, **_kwargs: self.deployment.aws.agentcore_control
        )
        environment = {
            "STATE_FILE": str(self.deployment.path),
            "AWS_REGION": "us-east-1",
            "RUNTIME_ID": self.deployment.aws.runtime_id,
            "GATEWAY_ID": self.deployment.aws.gateway_id,
        }
        monkeypatch.setattr(os, "environ", environment)
        monkeypatch.setattr(sys, "argv", ["main.py", "rollback"])
        monkeypatch.setattr(ab_aws.boto3, "Session", lambda *_args, **_kwargs: session)

        cli.main()

        assert json.loads(self.deployment.path.read_text())["finished"] == "rolled_back"

    def test_cleanup_without_state_needs_no_aws_configuration(self, monkeypatch):
        def unexpected_session(*_args, **_kwargs):
            pytest.fail("rollback without state must not create an AWS session")

        monkeypatch.setattr(os, "environ", {"STATE_FILE": str(self.deployment.path)})
        monkeypatch.setattr(sys, "argv", ["main.py", "rollback"])
        monkeypatch.setattr(ab_aws.boto3, "Session", unexpected_session)

        cli.main()

    def test_observe_subcommand_defers_promotion(self, monkeypatch):
        clients = {
            "bedrock-agentcore-control": self.deployment.aws.agentcore_control,
            "bedrock-agentcore": self.deployment.aws.agentcore,
        }
        session = SimpleNamespace(
            client=lambda service, **_kwargs: clients.get(service, SimpleNamespace()),
            region_name="us-east-1",
        )
        environment = {
            "STATE_FILE": str(self.deployment.path),
            "AWS_REGION": "us-east-1",
            "RUNTIME_ID": self.deployment.aws.runtime_id,
            "GATEWAY_ID": self.deployment.aws.gateway_id,
            "IMAGE_URI": IMAGE,
            "DURATION_SECONDS": "60",
            "QUALITY_GATES": json.dumps(self.deployment.quality_gates),
            "EVALUATION_CONFIG_ID": self.deployment.evaluation_config_template,
            "AB_TEST_ROLE_ARN": self.deployment.ab_test_role_arn,
            "EVALUATION_TIMEOUT_SECONDS": "60",
        }
        monkeypatch.setattr(os, "environ", environment)
        monkeypatch.setattr(sys, "argv", ["main.py", "observe"])
        monkeypatch.setattr(ab_aws.boto3, "Session", lambda *_args, **_kwargs: session)

        cli.main()

        state = json.loads(self.deployment.path.read_text())
        assert state["ready_to_promote"] is True
        assert "finished" not in state
        assert (
            self.deployment.aws.agentcore.tests[state["ab_test_id"]]["executionStatus"] == "RUNNING"
        )

    def test_run_subcommand_deploys_observes_and_promotes(self, monkeypatch):
        clients = {
            "bedrock-agentcore-control": self.deployment.aws.agentcore_control,
            "bedrock-agentcore": self.deployment.aws.agentcore,
        }
        session = SimpleNamespace(
            client=lambda service, **_kwargs: clients.get(service, SimpleNamespace()),
            region_name="us-east-1",
        )
        environment = {
            "STATE_FILE": str(self.deployment.path),
            "AWS_REGION": "us-east-1",
            "RUNTIME_ID": self.deployment.aws.runtime_id,
            "GATEWAY_ID": self.deployment.aws.gateway_id,
            "IMAGE_URI": IMAGE,
            "DURATION_SECONDS": "60",
            "QUALITY_GATES": json.dumps(self.deployment.quality_gates),
            "EVALUATION_CONFIG_ID": self.deployment.evaluation_config_template,
            "AB_TEST_ROLE_ARN": self.deployment.ab_test_role_arn,
            "EVALUATION_TIMEOUT_SECONDS": "60",
        }
        monkeypatch.setattr(os, "environ", environment)
        monkeypatch.setattr(sys, "argv", ["main.py", "run"])
        monkeypatch.setattr(ab_aws.boto3, "Session", lambda *_args, **_kwargs: session)

        cli.main()

        state = json.loads(self.deployment.path.read_text())
        assert state["finished"] == "promoted"
        assert self.deployment.aws.agentcore_control.endpoints == {"control": "2", "treatment": "2"}

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"EVALUATION_TIMEOUT_SECONDS": "0"}, "Evaluation timeout"),
            ({"DURATION_SECONDS": "30"}, "observation must last"),
            ({"SCORING_LAG_SECONDS": "-1"}, "Scoring lag"),
            ({"EVALUATION_CONFIG_ID": " "}, "evaluation-config-id"),
            ({"AB_TEST_ROLE_ARN": " "}, "ab-test-role-arn"),
            ({"QUALITY_GATES": "not-json"}, "quality-gates"),
            ({"QUALITY_GATES": "[]"}, "quality-gates"),
            ({"QUALITY_GATES": "{}"}, "quality-gates"),
            ({"CONTROL_WEIGHT": "abc"}, "control-weight"),
            ({"TREATMENT_WEIGHT": "abc"}, "treatment-weight"),
            ({"CONTROL_WEIGHT": "0", "TREATMENT_WEIGHT": "100"}, "weight"),
            ({"CONTROL_WEIGHT": "50", "TREATMENT_WEIGHT": "51"}, "weight"),
        ],
        ids=[
            "non-positive-timeout",
            "duration-too-short",
            "negative-scoring-lag",
            "blank-evaluation-config-id",
            "blank-ab-test-role-arn",
            "malformed-quality-gates-json",
            "quality-gates-not-an-object",
            "quality-gates-empty-object",
            "non-numeric-control-weight",
            "non-numeric-treatment-weight",
            "zero-control-weight",
            "weights-sum-over-100",
        ],
    )
    def test_build_deployment_rejects_invalid_configuration(self, monkeypatch, overrides, match):
        environment = {
            "STATE_FILE": str(self.deployment.path),
            "IMAGE_URI": IMAGE,
            "QUALITY_GATES": json.dumps(self.deployment.quality_gates),
            "EVALUATION_CONFIG_ID": self.deployment.evaluation_config_template,
            "AB_TEST_ROLE_ARN": self.deployment.ab_test_role_arn,
        }
        environment.update(overrides)
        monkeypatch.setattr(os, "environ", environment)
        monkeypatch.setattr(sys, "argv", ["main.py", "observe"])

        with pytest.raises(ValueError, match=match):
            cli.main()

    def test_promote_subcommand_promotes_saved_candidate(self, monkeypatch):
        self.deployment.observe_candidate(IMAGE, 60)
        session = SimpleNamespace(
            client=lambda service, **_kwargs: (
                self.deployment.aws.agentcore_control
                if service == "bedrock-agentcore-control"
                else self.deployment.aws.agentcore
            )
        )
        environment = {
            "STATE_FILE": str(self.deployment.path),
            "AWS_REGION": "us-east-1",
            "RUNTIME_ID": self.deployment.aws.runtime_id,
            "GATEWAY_ID": self.deployment.aws.gateway_id,
        }
        monkeypatch.setattr(os, "environ", environment)
        monkeypatch.setattr(sys, "argv", ["main.py", "promote"])
        monkeypatch.setattr(ab_aws.boto3, "Session", lambda *_args, **_kwargs: session)

        cli.main()

        assert json.loads(self.deployment.path.read_text())["finished"] == "promoted"

    def test_rollback_failure_preserves_original_error_and_pending_state(self):
        self.deployment._observe = lambda _seconds: (_ for _ in ()).throw(
            ValueError("Probe failed")
        )
        self.deployment.rollback = lambda: (_ for _ in ()).throw(RuntimeError("AWS unavailable"))
        with pytest.raises(ValueError, match="Probe failed"):
            self.deployment.run(IMAGE, 60)
        assert "finished" not in self.deployment.state

    def test_custom_evaluator_uses_its_own_scale(self):
        self.deployment.quality_gates = {"custom-eval-abcdefghij": 3.5}
        self.deployment.aws.agentcore.results = {
            "evaluatorMetrics": [
                {
                    "evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/custom-eval-abcdefghij",
                    "controlStats": {"variantName": "C", "sampleSize": 5, "mean": 3.0},
                    "variantResults": [
                        {
                            "variantName": "T1",
                            "sampleSize": 5,
                            "mean": 4.0,
                            "absoluteChange": 1.0,
                            "percentChange": 33.3,
                            "pValue": 0.01,
                            "isSignificant": True,
                        }
                    ],
                }
            ]
        }
        self.deployment.run(IMAGE, 60)
        assert self.deployment.state["finished"] == "promoted"
        assert self.deployment.state["quality_gates"] == {"custom-eval-abcdefghij": 3.5}
        assert self.deployment.state["variant_results"]["custom-eval-abcdefghij"]["mean"] == 4.0

    def test_results_appearing_after_polling_starts(self):
        calls = 0
        empty_results = {"evaluatorMetrics": []}
        happy_results = self.deployment.aws.agentcore.results

        def get_ab_test(**kwargs):
            nonlocal calls
            calls += 1
            test = copy.deepcopy(self.deployment.aws.agentcore.tests[kwargs["abTestId"]])
            test["results"] = empty_results if calls == 1 else happy_results
            return test

        self.deployment.aws.agentcore.get_ab_test = get_ab_test
        self.deployment.run(IMAGE, 60)
        assert calls >= 2
        assert self.deployment.state["finished"] == "promoted"

    def test_failed_state_write_preserves_recoverable_journal(self, monkeypatch):
        self.deployment._checkpoint(baseline="1")

        def fail_replace(_source, _target):
            raise OSError("Disk unavailable")

        monkeypatch.setattr(Path, "replace", fail_replace)

        with pytest.raises(OSError, match="Disk unavailable"):
            self.deployment._checkpoint(finished="promoted")

        assert self.deployment.state == {"baseline": "1"}
        assert json.loads(self.deployment.path.read_text()) == {"baseline": "1"}

    def test_managed_score_below_threshold_rolls_back(self):
        self.deployment.quality_gates = {"Builtin.Helpfulness": 0.9}
        with pytest.raises(ValueError, match="quality gates failed"):
            self.deployment.run(IMAGE, 60)
        assert self.deployment.state["finished"] == "rolled_back"
        assert self.deployment.state["variant_results"]["Builtin.Helpfulness"]["mean"] == 0.8
        assert self.deployment.aws.agentcore_control.endpoints["treatment"] == "1"

    def test_result_not_yet_significant_rolls_back_not_times_out(self):
        results = copy.deepcopy(DEFAULT_RESULTS)
        results["evaluatorMetrics"][0]["variantResults"][0]["isSignificant"] = False
        self.deployment.aws.agentcore.results = results
        with pytest.raises(ValueError, match="quality gates failed"):
            self.deployment.run(IMAGE, 60)
        assert self.deployment.state["finished"] == "rolled_back"

    def test_non_significant_result_promotes_when_significance_not_required(self):
        results = copy.deepcopy(DEFAULT_RESULTS)
        results["evaluatorMetrics"][0]["variantResults"][0]["isSignificant"] = False
        self.deployment.aws.agentcore.results = results
        self.deployment.require_significance = False
        self.deployment.run(IMAGE, 60)
        assert self.deployment.state["finished"] == "promoted"

    def test_missing_managed_results_times_out_and_rolls_back(self):
        self.deployment.aws.agentcore.results = {"evaluatorMetrics": []}
        with pytest.raises(TimeoutError, match="A/B test results"):
            self.deployment.run(IMAGE, 60)
        assert self.deployment.state["finished"] == "rolled_back"

    def test_interruption_attempts_rollback(self):
        self.deployment._observe = lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            self.deployment.run(IMAGE, 60)
        ab_test_id = self.deployment.state["ab_test_id"]
        assert self.deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"
        assert self.deployment.aws.agentcore_control.endpoints["control"] == "1"
        assert self.deployment.aws.agentcore_control.endpoints["treatment"] == "1"

    def test_promotion_failure_restores_baseline(self):
        original = self.deployment._point

        def point(name, version):
            original(name, version)
            if name == "control" and version == "2":
                raise RuntimeError("Lost response after update")

        self.deployment._point = point
        with pytest.raises(RuntimeError, match="Lost response"):
            self.deployment.run(IMAGE, 60)
        assert self.deployment.aws.agentcore_control.endpoints["control"] == "1"
        assert self.deployment.aws.agentcore_control.endpoints["treatment"] == "1"
        ab_test_id = self.deployment.state["ab_test_id"]
        assert self.deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"

    def test_cleanup_is_idempotent_after_success(self):
        self.deployment.run(IMAGE, 60)
        self.deployment.rollback()
        assert self.deployment.aws.agentcore_control.endpoints["control"] == "2"

    def test_promote_requires_prior_observation(self):
        with pytest.raises(RuntimeError, match="run observation first"):
            self.deployment.promote_candidate()

    def test_observe_then_promote_matches_run(self):
        """The two-phase CLI (observe, wait for approval, promote) reaches the
        same end state as the single-call automatic path."""
        self.deployment.observe_candidate(IMAGE, 60)
        assert self.deployment.state["ready_to_promote"] is True
        assert "finished" not in self.deployment.state
        ab_test_id = self.deployment.state["ab_test_id"]
        assert self.deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "RUNNING"

        self.deployment.promote_candidate()
        assert self.deployment.state["finished"] == "promoted"
        assert self.deployment.aws.agentcore_control.endpoints == {"control": "2", "treatment": "2"}
        assert self.deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"

    def test_observe_failure_rolls_back_without_promoting(self):
        self.deployment.quality_gates = {"Builtin.Helpfulness": 0.99}
        with pytest.raises(ValueError, match="quality gates failed"):
            self.deployment.observe_candidate(IMAGE, 60)
        assert self.deployment.state["finished"] == "rolled_back"
        assert "ready_to_promote" not in self.deployment.state
        assert self.deployment.aws.agentcore_control.endpoints["treatment"] == "1"

    def test_ab_test_failure_during_observation_fails_fast_and_rolls_back(self):
        original_get = self.deployment.aws.agentcore.get_ab_test
        calls = 0

        def get_ab_test(**kwargs):
            nonlocal calls
            calls += 1
            result = original_get(**kwargs)
            # Simulate the AB test failing on the second poll (first happens inside _start_ab_test),
            # but only when it hasn't been stopped yet (rollback must be able to stop it).
            stored = self.deployment.aws.agentcore.tests[kwargs["abTestId"]]
            if calls >= 2 and stored["executionStatus"] == "RUNNING":
                stored["executionStatus"] = "FAILED"
                result["executionStatus"] = "FAILED"
            return result

        self.deployment.aws.agentcore.get_ab_test = get_ab_test
        with pytest.raises(RuntimeError, match="left RUNNING state"):
            self.deployment.observe_candidate(IMAGE, 300)
        assert self.deployment.state["finished"] == "rolled_back"
        assert "ready_to_promote" not in self.deployment.state

    def test_does_not_take_over_an_active_experiment(self):
        self.deployment.aws.agentcore.tests["abtest-existing"] = {
            "abTestId": "abtest-existing",
            "abTestArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:ab-test/abtest-existing",
            "gatewayArn": GATEWAY_ARN,
            "variants": [],
            "status": "ACTIVE",
            "executionStatus": "RUNNING",
        }
        with pytest.raises(RuntimeError, match="still active"):
            self.deployment.run(IMAGE, 60)
        assert self.deployment.aws.agentcore_control.events == []

    def test_ecr_tag_is_resolved_without_publishing(self):
        seen = {}

        def describe_images(**kwargs):
            seen.update(kwargs)
            return {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]}

        self.deployment.aws._ecr = SimpleNamespace(describe_images=describe_images)
        assert self.deployment.aws.resolve_image(IMAGE.split("@")[0] + ":v1") == IMAGE
        assert seen["imageIds"] == [{"imageTag": "v1"}]

    def test_failed_control_update_can_be_restored(self):
        original = self.deployment._point

        def point(name, version):
            if name == "control" and version == "2":
                self.deployment.aws.agentcore_control.statuses["control"] = "UPDATE_FAILED"
                raise RuntimeError("Control update failed")
            original(name, version)

        self.deployment._point = point
        with pytest.raises(RuntimeError, match="Control update failed"):
            self.deployment.run(IMAGE, 60)
        assert self.deployment.aws.agentcore_control.endpoints["control"] == "1"
        assert self.deployment.aws.agentcore_control.endpoints["treatment"] == "1"
        assert self.deployment.aws.agentcore_control.statuses["control"] == "READY"
        ab_test_id = self.deployment.state["ab_test_id"]
        assert self.deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"

    def test_ab_test_uses_configured_weights_and_targets(self):
        self.deployment.control_weight = 90
        self.deployment.treatment_weight = 10
        self.deployment.observe_candidate(IMAGE, 60)
        ab_test_id = self.deployment.state["ab_test_id"]
        variants = {
            v["name"]: v for v in self.deployment.aws.agentcore.tests[ab_test_id]["variants"]
        }
        assert variants["C"]["weight"] == 90
        assert variants["T1"]["weight"] == 10
        assert variants["C"]["variantConfiguration"]["target"]["name"] == "control"
        assert variants["T1"]["variantConfiguration"]["target"]["name"] == "treatment"

    def test_ephemeral_configs_use_correct_data_source_per_variant(self):
        self.deployment.observe_candidate(IMAGE, 60)
        created = self.deployment.aws.agentcore_control.ephemeral_configs
        assert len(created) == 2
        by_name = {cfg["onlineEvaluationConfigName"]: cfg for cfg in created.values()}
        control_names = [k for k in by_name if "_c_" in k]
        treatment_names = [k for k in by_name if "_t_" in k]
        assert len(control_names) == 1
        assert len(treatment_names) == 1
        control_cw = by_name[control_names[0]]["dataSourceConfig"]["cloudWatchLogs"]
        treatment_cw = by_name[treatment_names[0]]["dataSourceConfig"]["cloudWatchLogs"]
        # serviceNames must point at the correct variant
        assert all("control" in n for n in control_cw["serviceNames"])
        assert all("treatment" in n for n in treatment_cw["serviceNames"])
        assert all("control" not in n for n in treatment_cw["serviceNames"])
        # logGroupNames must also point at the correct per-agent log group
        assert all("control" in g for g in control_cw["logGroupNames"])
        assert all("treatment" in g for g in treatment_cw["logGroupNames"])
        assert all("control" not in g for g in treatment_cw["logGroupNames"])

    def test_observation_logs_experiment_setup_and_connection_listening(self, monkeypatch):
        logs: list[str] = []
        monkeypatch.setattr("builtins.print", lambda message, **_kwargs: logs.append(message))
        self.deployment.observe_candidate(IMAGE, 60)

        events = [json.loads(line)["event"] for line in logs if line.startswith("{")]

        assert "evaluation-config-creating" in events
        assert "ab-test-creating" in events
        assert "ab-test-running" in events
        assert "listening-for-connections" in events

    def test_ephemeral_configs_are_forced_to_100_percent_sampling(self):
        self.deployment.observe_candidate(IMAGE, 60)
        for cfg in self.deployment.aws.agentcore_control.ephemeral_configs.values():
            assert cfg["rule"]["samplingConfig"]["samplingPercentage"] == 100

    def test_ephemeral_configs_are_deleted_after_promotion(self):
        self.deployment.run(IMAGE, 60)
        assert len(self.deployment.aws.agentcore_control.ephemeral_configs) == 0
        assert self.deployment.state.get("ephemeral_control_config_id") is None
        assert self.deployment.state.get("ephemeral_treatment_config_id") is None

    def test_ephemeral_configs_are_deleted_after_rollback(self):
        self.deployment.quality_gates = {"Builtin.Helpfulness": 0.99}
        with pytest.raises(ValueError):
            self.deployment.run(IMAGE, 60)
        assert len(self.deployment.aws.agentcore_control.ephemeral_configs) == 0
        assert self.deployment.state.get("ephemeral_control_config_id") is None
        assert self.deployment.state.get("ephemeral_treatment_config_id") is None

    def test_promotion_tolerates_ab_test_already_stopped_conflict(self):
        self.deployment.observe_candidate(IMAGE, 60)
        original_update = self.deployment.aws.agentcore.update_ab_test

        def update_ab_test(**kwargs):
            if kwargs.get("executionStatus") == "STOPPED":
                raise _ConflictException("AB test is already stopped")
            return original_update(**kwargs)

        self.deployment.aws.agentcore.update_ab_test = update_ab_test
        self.deployment.promote_candidate()

        assert self.deployment.state["finished"] == "promoted"

    def test_rollback_tolerates_ab_test_already_stopped_conflict(self):
        self.deployment.observe_candidate(IMAGE, 60)
        original_update = self.deployment.aws.agentcore.update_ab_test

        def update_ab_test(**kwargs):
            if kwargs.get("executionStatus") == "STOPPED":
                raise _ConflictException("AB test is already stopped")
            return original_update(**kwargs)

        self.deployment.aws.agentcore.update_ab_test = update_ab_test
        self.deployment.rollback()

        assert self.deployment.state["finished"] == "rolled_back"

    def test_promotion_tolerates_ab_test_already_deleted(self):
        self.deployment.observe_candidate(IMAGE, 60)
        ab_test_id = self.deployment.state["ab_test_id"]
        del self.deployment.aws.agentcore.tests[ab_test_id]

        self.deployment.promote_candidate()

        assert self.deployment.state["finished"] == "promoted"

    def test_delete_evaluation_config_tolerates_already_deleted_config(self):
        def delete_online_evaluation_config(**_kwargs):
            raise KeyError("not found")

        self.deployment.aws.agentcore_control.delete_online_evaluation_config = (
            delete_online_evaluation_config
        )

        self.deployment.aws.delete_evaluation_config("eval-ephemeral-0")

    def test_delete_ephemeral_configs_tolerates_unexpected_aws_delete_failure(self):
        self.deployment.observe_candidate(IMAGE, 60)
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "DeleteOnlineEvaluationConfig",
        )
        self.deployment.aws.delete_evaluation_config = lambda _config_id: (_ for _ in ()).throw(
            error
        )

        self.deployment.promote_candidate()

        assert self.deployment.state["finished"] == "promoted"
        assert self.deployment.state.get("ephemeral_control_config_id") is None
        assert self.deployment.state.get("ephemeral_treatment_config_id") is None

    def test_delete_ephemeral_configs_does_not_swallow_non_aws_bugs(self):
        self.deployment.observe_candidate(IMAGE, 60)
        self.deployment.aws.delete_evaluation_config = lambda _config_id: (_ for _ in ()).throw(
            RuntimeError("programming error, not an AWS failure")
        )

        with pytest.raises(RuntimeError, match="programming error"):
            self.deployment.promote_candidate()

    def test_reuses_existing_treatment_endpoint_and_matching_gateway_targets(self, monkeypatch):
        logs: list[str] = []
        monkeypatch.setattr("builtins.print", lambda message, **_kwargs: logs.append(message))
        self.deployment.aws.agentcore_control.endpoints["treatment"] = "1"
        self.deployment.aws.agentcore_control.targets = {
            "control": {
                "targetId": "control",
                "status": "READY",
                "targetConfiguration": {
                    "http": {"agentcoreRuntime": {"arn": ARN, "qualifier": "control"}}
                },
            },
            "treatment": {
                "targetId": "treatment",
                "status": "READY",
                "targetConfiguration": {
                    "http": {"agentcoreRuntime": {"arn": ARN, "qualifier": "treatment"}}
                },
            },
        }

        self.deployment.observe_candidate(IMAGE, 60)

        events = [json.loads(line)["event"] for line in logs if line.startswith("{")]
        assert "treatment-endpoint-existing" in events
        assert "treatment-endpoint-creating" not in events
        assert events.count("gateway-target-existing") == 2
        assert "gateway-target-creating" not in events

    def test_rejects_gateway_target_with_mismatched_configuration(self):
        self.deployment.aws.agentcore_control.targets = {
            "control": {
                "targetId": "control",
                "status": "READY",
                "targetConfiguration": {
                    "http": {"agentcoreRuntime": {"arn": ARN, "qualifier": "wrong-endpoint"}}
                },
            },
        }

        with pytest.raises(ValueError, match="does not match runtime/endpoint"):
            self.deployment.observe_candidate(IMAGE, 60)
        assert self.deployment.state["finished"] == "rolled_back"

    def test_resolve_image_rejects_region_mismatch(self):
        wrong_region_image = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/agent:v1"

        with pytest.raises(ValueError, match="same AWS Region"):
            self.deployment.aws.resolve_image(wrong_region_image)

    def test_run_rejects_observation_shorter_than_minimum(self):
        with pytest.raises(ValueError, match="at least 60 seconds"):
            self.deployment.run(IMAGE, 59)

    def test_observe_candidate_writes_variant_results_to_github_output(self, monkeypatch, tmp_path):
        output_path = tmp_path / "github_output.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

        self.deployment.observe_candidate(IMAGE, 60)

        line = output_path.read_text().strip()
        assert line.startswith("variant-results=")
        payload = json.loads(line.removeprefix("variant-results="))
        assert payload["Builtin.Helpfulness"]["mean"] == 0.8

    def test_promote_candidate_writes_runtime_outputs(self, monkeypatch, tmp_path):
        output_path = tmp_path / "github_output.txt"
        self.deployment.observe_candidate(IMAGE, 60)
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

        self.deployment.promote_candidate()

        content = output_path.read_text()
        assert "runtime-version=2" in content
        assert f"image-uri={IMAGE}" in content


class _GitHubResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_report_subcommand_publishes_pr_comment(monkeypatch, tmp_path):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 7}}))
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"finished": "promoted"}))
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return _GitHubResponse([])

    monkeypatch.setattr("agentcore_release_gate.report.urlopen", open_request)
    environment = {
        "GITHUB_EVENT_PATH": str(event_path),
        "STATE_FILE": str(state_path),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123",
        "GITHUB_TOKEN": "token",
        "DEPLOY_OUTCOME": "success",
    }
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(sys, "argv", ["main.py", "report"])

    cli.main()

    assert [request.get_method() for request in requests] == ["GET", "POST"]
    body = json.loads(requests[1].data)["body"]
    assert body.startswith(COMMENT_MARKER)
    assert "github.com/owner/repo/actions/runs/123" in body


def test_report_subcommand_skips_when_no_pull_request(monkeypatch, tmp_path):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({}))

    def open_request(*_args, **_kwargs):
        pytest.fail("report without a pull request must not call the GitHub API")

    monkeypatch.setattr("agentcore_release_gate.report.urlopen", open_request)
    monkeypatch.setattr(
        os,
        "environ",
        {"GITHUB_EVENT_PATH": str(event_path), "STATE_FILE": str(tmp_path / "missing.json")},
    )
    monkeypatch.setattr(sys, "argv", ["main.py", "report"])

    cli.main()


def test_report_subcommand_never_fails_the_job(monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "argv", ["main.py", "report"])

    cli.main()

    assert "::warning::" in capsys.readouterr().out
