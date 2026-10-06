"""Report: PR comment rendering and optional GitHub publication."""

import json

import pytest

from agentcore_release_gate.constants import GITHUB_COMMENTS_PAGE_SIZE
from agentcore_release_gate.evaluation import enforce_quality_gates
from agentcore_release_gate.exceptions import (
    ConfigurationError,
    QualityGateFailedError,
    UnexpectedGitHubResponseError,
)
from agentcore_release_gate.report import COMMENT_MARKER, build_report, publish_report
from agentcore_release_gate.state import DeploymentState


@pytest.fixture
def stub_urlopen(monkeypatch):
    requests = []

    def install(*payloads):
        responses = iter(payloads)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(next(responses)).encode()

        def open_request(request, timeout):
            requests.append((request, timeout))
            return Response()

        monkeypatch.setattr("agentcore_release_gate.report.urlopen", open_request)
        return requests

    return install


def test_build_report_includes_decision_scores_and_thresholds():
    state = {
        "finished": "promoted",
        "version": "2",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/agent@sha256:" + "a" * 64,
        "quality_gates": {"Builtin.Helpfulness": 0.7, "custom-tone-abcdefghij": 3.5},
        "variant_results": {
            "Builtin.Helpfulness": {
                "mean": 0.82,
                "isSignificant": True,
                "absoluteChange": 0.1,
                "pValue": 0.02,
                "controlSampleSize": 40,
                "treatmentSampleSize": 42,
            },
            "custom-tone-abcdefghij": {
                "mean": 4.0,
                "isSignificant": True,
                "absoluteChange": 0.5,
                "pValue": 0.01,
                "controlSampleSize": 40,
                "treatmentSampleSize": 42,
            },
        },
    }

    report = build_report(DeploymentState(**state), "success")

    assert COMMENT_MARKER in report
    assert "Promoted" in report
    assert "Builtin.Helpfulness" in report
    assert "0.82" in report
    assert "3.5" in report
    assert "`2`" in report
    assert "40/42" in report
    assert "Yes" in report


def test_build_report_explains_failure_without_deployment_state():
    report = build_report(DeploymentState(), "failure")

    assert "Failed" in report
    assert "No deployment state was recorded" in report


def test_build_report_shows_rollback_and_per_evaluator_failure_reasons():
    state = {
        "finished": "rolled_back",
        "quality_gates": {
            "Builtin.Below": 0.7,
            "Builtin.NotSignificant": 0.7,
            "Builtin.Regressed": 0.7,
            "Builtin.Missing": 0.7,
        },
        "variant_results": {
            "Builtin.Below": {
                "mean": 0.5,
                "isSignificant": True,
                "absoluteChange": 0.1,
                "pValue": 0.02,
                "controlSampleSize": 10,
                "treatmentSampleSize": 10,
            },
            "Builtin.NotSignificant": {
                "mean": 0.8,
                "isSignificant": False,
                "absoluteChange": 0.1,
                "pValue": 0.4,
                "controlSampleSize": 10,
                "treatmentSampleSize": 10,
            },
            "Builtin.Regressed": {
                "mean": 0.9,
                "isSignificant": True,
                "absoluteChange": -0.05,
                "pValue": 0.01,
                "controlSampleSize": 10,
                "treatmentSampleSize": 10,
            },
        },
    }

    report = build_report(DeploymentState(**state), "failure")

    assert "Rolled back" in report
    assert "❌ AgentCore A/B deployment rejected" in report
    assert "❌ Below minimum" in report
    assert "❌ Not significant" in report
    assert "❌ Regressed vs control" in report
    assert "❌ Missing" in report


def test_build_report_links_to_workflow_run():
    report = build_report(
        DeploymentState(finished="rolled_back"), "failure", "https://example.com/run/1"
    )

    assert "[View workflow run](https://example.com/run/1)" in report


def test_publish_report_updates_existing_bot_comment(stub_urlopen):
    requests = stub_urlopen(
        [{"id": 42, "body": COMMENT_MARKER, "user": {"type": "Bot"}}],
        {"id": 42},
    )

    publish_report("token", "owner/repository", 7, "report")

    assert [request.get_method() for request, _timeout in requests] == ["GET", "PATCH"]
    assert requests[1][0].full_url.endswith("/repos/owner/repository/issues/comments/42")
    assert json.loads(requests[1][0].data) == {"body": "report"}


def test_publish_report_creates_comment_when_no_bot_comment_exists(stub_urlopen):
    requests = stub_urlopen(
        [{"id": 9, "body": COMMENT_MARKER, "user": {"type": "User"}}],
        {"id": 10},
    )

    publish_report("token", "owner/repository", 7, "report")

    assert [request.get_method() for request, _timeout in requests] == ["GET", "POST"]
    assert requests[1][0].full_url.endswith("/repos/owner/repository/issues/7/comments")


@pytest.mark.parametrize("repository", ["invalid", "/repository", "owner/"])
def test_publish_report_rejects_invalid_repository(repository):
    with pytest.raises(ConfigurationError, match="repository"):
        publish_report("token", repository, 7, "report")


@pytest.mark.parametrize("pull_request", [0, -1])
def test_publish_report_rejects_non_positive_pull_request(pull_request):
    with pytest.raises(ConfigurationError, match="positive"):
        publish_report("token", "owner/repository", pull_request, "report")


def test_publish_report_rejects_non_list_comments_response(stub_urlopen):
    stub_urlopen({"error": "not a list"})

    with pytest.raises(UnexpectedGitHubResponseError, match="JSON array"):
        publish_report("token", "owner/repository", 7, "report")


def test_publish_report_pages_through_comments_when_first_page_is_full(stub_urlopen):
    unrelated = [
        {"id": i, "body": "unrelated", "user": {"type": "User"}}
        for i in range(GITHUB_COMMENTS_PAGE_SIZE)
    ]
    owned = [{"id": 999, "body": COMMENT_MARKER, "user": {"type": "Bot"}}]
    requests = stub_urlopen(unrelated, owned, {"id": 999})

    publish_report("token", "owner/repository", 7, "report")

    urls = [request.full_url for request, _timeout in requests]
    assert "page=1" in urls[0]
    assert "page=2" in urls[1]
    assert urls[2].endswith("/repos/owner/repository/issues/comments/999")


def _single_gate_state(require_significance=None, **variant_overrides):
    variant = {
        "mean": 1.0,
        "isSignificant": False,
        "absoluteChange": 0.0,
        "pValue": None,
        "controlSampleSize": 19,
        "treatmentSampleSize": 5,
        **variant_overrides,
    }
    state = {
        "finished": "promoted",
        "version": "19",
        "image": "img@sha256:x",
        "quality_gates": {"agentcore_ab_toolusage-235rfk6sBg": 0.5},
        "variant_results": {"agentcore_ab_toolusage-235rfk6sBg": variant},
    }
    if require_significance is not None:
        state["require_significance"] = require_significance
    return DeploymentState(**state)


def _result_cell(report):
    row = next(line for line in report.splitlines() if "agentcore_ab_toolusage" in line)
    return row.rstrip(" |").rsplit("| ", 1)[-1]


def test_build_report_passes_non_significant_result_when_significance_not_required():
    report = build_report(_single_gate_state(require_significance=False), "success")

    assert "✅ AgentCore A/B deployment promoted" in report
    assert _result_cell(report) == "✅ Pass"
    assert "No (not required)" in report
    assert "Not significant" not in report


def test_build_report_fails_non_significant_result_when_significance_required():
    report = build_report(_single_gate_state(require_significance=True), "success")

    assert _result_cell(report) == "❌ Not significant"
    assert "No (not required)" not in report


def test_build_report_requires_significance_for_journals_without_the_setting():
    report = build_report(_single_gate_state(), "success")

    assert _result_cell(report) == "❌ Not significant"


def test_build_report_flags_below_minimum_when_significance_not_required():
    report = build_report(_single_gate_state(require_significance=False, mean=0.4), "success")

    assert _result_cell(report) == "❌ Below minimum"


def test_build_report_flags_regression_when_significance_not_required():
    state = _single_gate_state(require_significance=False, absoluteChange=-0.1)

    assert _result_cell(build_report(state, "success")) == "❌ Regressed vs control"


@pytest.mark.parametrize("require_significance", [True, False])
@pytest.mark.parametrize("change", [-0.1, 0.0, 0.2, None])
@pytest.mark.parametrize("significant", [True, False])
@pytest.mark.parametrize(("mean", "minimum"), [(0.4, 0.5), (0.5, 0.5), (0.9, 0.5)])
def test_build_report_result_matches_enforce_quality_gates(
    mean, minimum, significant, change, require_significance
):
    variant = {
        "mean": mean,
        "isSignificant": significant,
        "absoluteChange": change,
        "pValue": None,
        "controlSampleSize": 10,
        "treatmentSampleSize": 10,
    }
    state = {
        "finished": "promoted",
        "quality_gates": {"agentcore_ab_toolusage-235rfk6sBg": minimum},
        "variant_results": {"agentcore_ab_toolusage-235rfk6sBg": variant},
        "require_significance": require_significance,
    }
    try:
        enforce_quality_gates(
            state["variant_results"],
            state["quality_gates"],
            require_significance=require_significance,
        )
        gate_passed = True
    except QualityGateFailedError:
        gate_passed = False

    report = build_report(DeploymentState(**state), "success")

    assert _result_cell(report).startswith("✅") is gate_passed
