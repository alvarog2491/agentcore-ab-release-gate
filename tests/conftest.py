"""Shared fixtures: a mock clock and a Deployment wired to the SDK-validated AWS mocks."""

from types import SimpleNamespace

import pytest
from aws_mocks import MockAgentCore, MockAgentCoreControl

from agentcore_release_gate import aws_client as ab_aws
from agentcore_release_gate import deployment as ab
from agentcore_release_gate.state import DeploymentState


class MockClock:
    """Deterministic replacement for ``time.monotonic``/``time.sleep``."""

    value = 100000

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


@pytest.fixture
def clock(monkeypatch):
    """Make every poll loop advance instantly on a mock monotonic clock."""
    mock = MockClock()
    monkeypatch.setattr(ab.time, "monotonic", mock.now)
    monkeypatch.setattr(ab.time, "sleep", mock.sleep)
    return mock


@pytest.fixture
def silence_print(monkeypatch):
    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: None)


@pytest.fixture
def aws():
    """An AwsClient whose boto3 clients are replaced by the in-memory mocks."""
    client = ab_aws.AwsClient.__new__(ab_aws.AwsClient)
    client.agentcore_control = MockAgentCoreControl()
    client.agentcore = MockAgentCore()
    client._ecr = SimpleNamespace()
    client.runtime_id = "agent-abcdefghij"
    client.gateway_id = "gateway-abcdefghij"
    client.region = "us-east-1"
    return client


@pytest.fixture
def deployment(tmp_path, aws, clock, silence_print):
    """A Deployment with a fresh state journal, backed by the mock AWS client."""
    deployment = ab.Deployment.__new__(ab.Deployment)
    deployment.path = tmp_path / "state.json"
    deployment.state = DeploymentState()
    deployment.quality_gates = {"Builtin.Helpfulness": 0.7, "Builtin.Correctness": 0.7}
    deployment.require_significance = True
    deployment.control_endpoint_name = "control"
    deployment.evaluation_config_template = "template_eval-abcdefghij"
    deployment.ab_test_role_arn = "arn:aws:iam::123456789012:role/ABTestRole"
    deployment.control_weight = 80
    deployment.treatment_weight = 20
    deployment.evaluation_timeout = 60
    deployment.scoring_lag_seconds = 0
    deployment.aws = aws
    return deployment
