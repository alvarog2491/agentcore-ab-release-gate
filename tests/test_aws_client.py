"""AwsClient: ECR image parsing/resolution and online-evaluation config management."""

from types import SimpleNamespace

import pytest
from aws_mocks import IMAGE

from agentcore_release_gate.aws_client import _parse_image
from agentcore_release_gate.exceptions import ImageRegionMismatchError, InvalidImageUriError


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/org/agent:main",
        "docker.io/org/agent:v1",
        "docker.io/library/nginx:latest",
        "org/agent:latest",
    ],
    ids=["ghcr", "docker-hub", "docker-hub-library", "implicit-docker-hub"],
)
def test_parse_image_rejects_non_ecr_registries(image):
    with pytest.raises(InvalidImageUriError, match="ECR"):
        _parse_image(image)


def test_parse_image_extracts_tag_and_digest():
    base = "123456789012.dkr.ecr.us-east-1.amazonaws.com/team/agent"

    assert _parse_image(base + ":v1")["tag"] == "v1"
    assert _parse_image(base + "@sha256:" + "a" * 64)["digest"] == "sha256:" + "a" * 64


def test_resolve_image_pins_tag_to_its_digest(aws):
    seen = {}

    def describe_images(**kwargs):
        seen.update(kwargs)
        return {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]}

    aws._ecr = SimpleNamespace(describe_images=describe_images)
    assert aws.resolve_image(IMAGE.split("@")[0] + ":v1") == IMAGE
    assert seen["imageIds"] == [{"imageTag": "v1"}]


def test_resolve_image_returns_digest_uri_unchanged(aws):
    aws._ecr = SimpleNamespace(describe_images=lambda **_kwargs: pytest.fail("no lookup needed"))

    assert aws.resolve_image(IMAGE) == IMAGE


def test_resolve_image_rejects_region_mismatch(aws):
    wrong_region_image = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/agent:v1"

    with pytest.raises(ImageRegionMismatchError, match="same AWS Region"):
        aws.resolve_image(wrong_region_image)


def test_delete_evaluation_config_tolerates_already_deleted_config(aws):
    def delete_online_evaluation_config(**_kwargs):
        raise KeyError("not found")

    aws.agentcore_control.delete_online_evaluation_config = delete_online_evaluation_config

    aws.delete_evaluation_config("eval-ephemeral-0")


def test_create_evaluation_config_from_copies_template_and_forces_full_sampling(aws):
    template = aws.get_evaluation_config("template_eval-abcdefghij")

    config_id, config_arn = aws.create_evaluation_config_from(template, variant="t")

    created = aws.agentcore_control.ephemeral_configs[config_id]
    assert config_arn.endswith("/" + config_id)
    assert created["onlineEvaluationConfigName"].startswith("control-eval_t_")
    assert created["rule"]["samplingConfig"]["samplingPercentage"] == 100
    assert created["dataSourceConfig"] == template["dataSourceConfig"]
    assert created["evaluationExecutionRoleArn"] == template["evaluationExecutionRoleArn"]
