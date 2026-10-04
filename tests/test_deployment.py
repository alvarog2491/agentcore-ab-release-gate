"""Deployment: the release lifecycle orchestrated against the SDK-validated AWS mocks."""

import copy
import json
from pathlib import Path

import pytest
from aws_mocks import ARN, DEFAULT_RESULTS, GATEWAY_ARN, IMAGE, ConflictException
from botocore.exceptions import ClientError

from agentcore_release_gate.deployment import load_state
from agentcore_release_gate.exceptions import (
    ABTestAlreadyActiveError,
    ABTestInterruptedError,
    CandidateNotReadyError,
    ConfigurationError,
    EvaluationTimeoutError,
    GatewayTargetMismatchError,
    QualityGateFailedError,
    StateJournalError,
    WorkflowCancelledError,
)


def _raise(error):
    """Build a stand-in callable that raises ``error`` whatever it is called with."""

    def fail(*_args, **_kwargs):
        raise error

    return fail


def _logged_events(logs):
    return [json.loads(line)["event"] for line in logs if line.startswith("{")]


# --- State journal ---


def test_load_state_returns_empty_state_when_journal_is_missing(tmp_path):
    assert load_state(tmp_path / "state.json") == {}


@pytest.mark.parametrize(
    ("content", "message"),
    [("{truncated", "not valid JSON"), ("[]", "must contain a JSON object")],
    ids=["corrupt", "not-an-object"],
)
def test_load_state_rejects_unreadable_journal(tmp_path, content, message):
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(StateJournalError, match=message):
        load_state(path)


def test_checkpoint_keeps_previous_journal_when_write_fails(deployment, monkeypatch):
    deployment._checkpoint(baseline="1")
    monkeypatch.setattr(Path, "replace", _raise(OSError("Disk unavailable")))

    with pytest.raises(OSError, match="Disk unavailable"):
        deployment._checkpoint(finished="promoted")

    assert deployment.state == {"baseline": "1"}
    assert json.loads(deployment.path.read_text()) == {"baseline": "1"}


# --- run: deploy, observe, promote ---


def test_run_promotes_candidate_on_both_endpoints_after_full_observation(deployment, clock):
    start = clock.value

    deployment.run(IMAGE, 7200)

    assert clock.value - start >= 7200
    assert deployment.state["finished"] == "promoted"
    assert deployment.aws.agentcore_control.endpoints == {"control": "2", "treatment": "2"}
    ab_test_id = deployment.state["ab_test_id"]
    assert deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"


def test_run_gates_custom_evaluator_on_its_own_score_scale(deployment):
    deployment.quality_gates = {"custom-eval-abcdefghij": 3.5}
    deployment.aws.agentcore.results = {
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

    deployment.run(IMAGE, 60)

    assert deployment.state["finished"] == "promoted"
    assert deployment.state["quality_gates"] == {"custom-eval-abcdefghij": 3.5}
    assert deployment.state["variant_results"]["custom-eval-abcdefghij"]["mean"] == 4.0


def test_run_keeps_polling_until_evaluator_results_appear(deployment):
    observe = deployment._observe
    get_ab_test = deployment.aws.agentcore.get_ab_test
    observed = False
    result_polls = 0

    def observe_and_mark_done(seconds):
        nonlocal observed
        observe(seconds)
        observed = True

    def get_ab_test_without_results_on_first_result_poll(**kwargs):
        nonlocal result_polls
        test = get_ab_test(**kwargs)
        if observed:
            result_polls += 1
            if result_polls == 1:
                test["results"] = {"evaluatorMetrics": []}
        return test

    deployment._observe = observe_and_mark_done
    deployment.aws.agentcore.get_ab_test = get_ab_test_without_results_on_first_result_poll

    deployment.run(IMAGE, 60)

    assert deployment.state["finished"] == "promoted"
    assert deployment.state["variant_results"].keys() == deployment.quality_gates.keys()


def test_run_promotes_non_significant_result_when_significance_not_required(deployment):
    results = copy.deepcopy(DEFAULT_RESULTS)
    results["evaluatorMetrics"][0]["variantResults"][0]["isSignificant"] = False
    deployment.aws.agentcore.results = results
    deployment.require_significance = False

    deployment.run(IMAGE, 60)

    assert deployment.state["finished"] == "promoted"


def test_run_rejects_observation_shorter_than_minimum(deployment):
    with pytest.raises(ConfigurationError, match="at least 60 seconds"):
        deployment.run(IMAGE, 59)


def test_run_refuses_to_start_while_another_ab_test_is_active(deployment):
    deployment.aws.agentcore.tests["abtest-existing"] = {
        "abTestId": "abtest-existing",
        "abTestArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:ab-test/abtest-existing",
        "gatewayArn": GATEWAY_ARN,
        "variants": [],
        "status": "ACTIVE",
        "executionStatus": "RUNNING",
    }

    with pytest.raises(ABTestAlreadyActiveError, match="still active"):
        deployment.run(IMAGE, 60)

    assert deployment.aws.agentcore_control.events == []


# --- run: failed quality gates roll back ---


def test_run_rolls_back_when_score_is_below_minimum(deployment):
    deployment.quality_gates = {"Builtin.Helpfulness": 0.9}

    with pytest.raises(QualityGateFailedError):
        deployment.run(IMAGE, 60)

    assert deployment.state["finished"] == "rolled_back"
    assert deployment.state["variant_results"]["Builtin.Helpfulness"]["mean"] == 0.8
    assert deployment.aws.agentcore_control.endpoints == {"control": "1", "treatment": "1"}


def test_run_rolls_back_non_significant_result_instead_of_timing_out(deployment):
    results = copy.deepcopy(DEFAULT_RESULTS)
    results["evaluatorMetrics"][0]["variantResults"][0]["isSignificant"] = False
    deployment.aws.agentcore.results = results

    with pytest.raises(QualityGateFailedError):
        deployment.run(IMAGE, 60)

    assert deployment.state["finished"] == "rolled_back"


def test_run_rolls_back_when_evaluator_results_never_arrive(deployment):
    deployment.aws.agentcore.results = {"evaluatorMetrics": []}

    with pytest.raises(EvaluationTimeoutError, match="A/B test results"):
        deployment.run(IMAGE, 60)

    assert deployment.state["finished"] == "rolled_back"


# --- observe_candidate / promote_candidate: the split, approval-gated path ---


def test_observe_then_promote_reaches_same_end_state_as_run(deployment):
    deployment.observe_candidate(IMAGE, 60)

    assert deployment.state["ready_to_promote"] is True
    assert "finished" not in deployment.state
    ab_test_id = deployment.state["ab_test_id"]
    assert deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "RUNNING"

    deployment.promote_candidate()

    assert deployment.state["finished"] == "promoted"
    assert deployment.aws.agentcore_control.endpoints == {"control": "2", "treatment": "2"}
    assert deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"


def test_promote_rejects_candidate_that_was_not_observed(deployment):
    with pytest.raises(CandidateNotReadyError, match="run observation first"):
        deployment.promote_candidate()


def test_observe_rolls_back_without_promoting_when_gates_fail(deployment):
    deployment.quality_gates = {"Builtin.Helpfulness": 0.99}

    with pytest.raises(QualityGateFailedError):
        deployment.observe_candidate(IMAGE, 60)

    assert deployment.state["finished"] == "rolled_back"
    assert "ready_to_promote" not in deployment.state
    assert deployment.aws.agentcore_control.endpoints["treatment"] == "1"


@pytest.mark.parametrize("require_significance", [True, False])
@pytest.mark.parametrize(("minimum", "gates_pass"), [(0.7, True), (0.99, False)])
def test_observe_records_require_significance_in_journal(
    deployment, require_significance, minimum, gates_pass
):
    deployment.require_significance = require_significance
    deployment.quality_gates = {"Builtin.Helpfulness": minimum}

    if gates_pass:
        deployment.observe_candidate(IMAGE, 60)
    else:
        with pytest.raises(QualityGateFailedError):
            deployment.observe_candidate(IMAGE, 60)

    state = json.loads(deployment.path.read_text())
    assert state["require_significance"] is require_significance


def test_observe_rolls_back_when_ab_test_stops_during_observation(deployment):
    get_ab_test = deployment.aws.agentcore.get_ab_test
    calls = 0

    def get_ab_test_that_fails_after_starting(**kwargs):
        nonlocal calls
        calls += 1
        result = get_ab_test(**kwargs)
        # The first poll is the readiness wait inside _start_ab_test; fail on the next one,
        # but only while the test is still running so rollback can still stop it.
        stored = deployment.aws.agentcore.tests[kwargs["abTestId"]]
        if calls >= 2 and stored["executionStatus"] == "RUNNING":
            stored["executionStatus"] = "FAILED"
            result["executionStatus"] = "FAILED"
        return result

    deployment.aws.agentcore.get_ab_test = get_ab_test_that_fails_after_starting

    with pytest.raises(ABTestInterruptedError, match="left RUNNING state"):
        deployment.observe_candidate(IMAGE, 300)

    assert deployment.state["finished"] == "rolled_back"
    assert "ready_to_promote" not in deployment.state


# --- observe_candidate: experiment setup ---


def test_observe_creates_ab_test_with_configured_weights_and_targets(deployment):
    deployment.control_weight = 90
    deployment.treatment_weight = 10

    deployment.observe_candidate(IMAGE, 60)

    ab_test_id = deployment.state["ab_test_id"]
    variants = {v["name"]: v for v in deployment.aws.agentcore.tests[ab_test_id]["variants"]}
    assert variants["C"]["weight"] == 90
    assert variants["T1"]["weight"] == 10
    assert variants["C"]["variantConfiguration"]["target"]["name"] == "control"
    assert variants["T1"]["variantConfiguration"]["target"]["name"] == "treatment"


def test_observe_points_each_evaluation_config_at_its_own_variant_logs(deployment):
    deployment.observe_candidate(IMAGE, 60)

    created = deployment.aws.agentcore_control.ephemeral_configs.values()
    by_variant = {
        "control": [c for c in created if "_c_" in c["onlineEvaluationConfigName"]],
        "treatment": [c for c in created if "_t_" in c["onlineEvaluationConfigName"]],
    }
    assert [len(configs) for configs in by_variant.values()] == [1, 1]
    control_logs = by_variant["control"][0]["dataSourceConfig"]["cloudWatchLogs"]
    treatment_logs = by_variant["treatment"][0]["dataSourceConfig"]["cloudWatchLogs"]
    assert control_logs == {
        "logGroupNames": ["/aws/bedrock-agentcore/runtimes/runtime123-control"],
        "serviceNames": ["/aws/bedrock-agentcore/control"],
    }
    assert treatment_logs == {
        "logGroupNames": ["/aws/bedrock-agentcore/runtimes/runtime123-treatment"],
        "serviceNames": ["/aws/bedrock-agentcore/treatment"],
    }


def test_observe_logs_experiment_setup_events(deployment, monkeypatch):
    logs: list[str] = []
    monkeypatch.setattr("builtins.print", lambda message, **_kwargs: logs.append(message))

    deployment.observe_candidate(IMAGE, 60)

    events = _logged_events(logs)
    assert "evaluation-config-creating" in events
    assert "ab-test-creating" in events
    assert "ab-test-running" in events
    assert "listening-for-connections" in events


def test_observe_reuses_existing_treatment_endpoint_and_matching_gateway_targets(
    deployment, monkeypatch
):
    logs: list[str] = []
    monkeypatch.setattr("builtins.print", lambda message, **_kwargs: logs.append(message))
    deployment.aws.agentcore_control.endpoints["treatment"] = "1"
    deployment.aws.agentcore_control.targets = {
        name: {
            "targetId": name,
            "status": "READY",
            "targetConfiguration": {"http": {"agentcoreRuntime": {"arn": ARN, "qualifier": name}}},
        }
        for name in ("control", "treatment")
    }

    deployment.observe_candidate(IMAGE, 60)

    events = _logged_events(logs)
    assert "treatment-endpoint-existing" in events
    assert "treatment-endpoint-creating" not in events
    assert events.count("gateway-target-existing") == 2
    assert "gateway-target-creating" not in events


def test_observe_rolls_back_when_existing_gateway_target_mismatches(deployment):
    deployment.aws.agentcore_control.targets = {
        "control": {
            "targetId": "control",
            "status": "READY",
            "targetConfiguration": {
                "http": {"agentcoreRuntime": {"arn": ARN, "qualifier": "wrong-endpoint"}}
            },
        },
    }

    with pytest.raises(GatewayTargetMismatchError, match="does not match runtime/endpoint"):
        deployment.observe_candidate(IMAGE, 60)

    assert deployment.state["finished"] == "rolled_back"


# --- Recovery: rollback after failures and cancellation ---


def test_run_reraises_original_error_when_rollback_also_fails(deployment):
    deployment._observe = _raise(ValueError("Probe failed"))
    deployment.rollback = _raise(RuntimeError("AWS unavailable"))

    with pytest.raises(ValueError, match="Probe failed"):
        deployment.run(IMAGE, 60)

    # Left unfinished so the always() cleanup step retries the rollback.
    assert "finished" not in deployment.state


def test_run_rolls_back_when_workflow_is_cancelled(deployment):
    deployment._observe = _raise(WorkflowCancelledError("cancelled"))

    with pytest.raises(WorkflowCancelledError):
        deployment.run(IMAGE, 60)

    assert deployment.state["finished"] == "rolled_back"
    assert deployment.aws.agentcore_control.endpoints == {"control": "1", "treatment": "1"}
    ab_test_id = deployment.state["ab_test_id"]
    assert deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"


def test_promote_restores_baseline_when_control_update_response_is_lost(deployment):
    point = deployment._point

    def point_then_lose_response(name, version):
        point(name, version)
        if name == "control" and version == "2":
            raise RuntimeError("Lost response after update")

    deployment._point = point_then_lose_response

    with pytest.raises(RuntimeError, match="Lost response"):
        deployment.run(IMAGE, 60)

    assert deployment.aws.agentcore_control.endpoints == {"control": "1", "treatment": "1"}
    ab_test_id = deployment.state["ab_test_id"]
    assert deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"


def test_promote_restores_baseline_when_control_update_fails(deployment):
    point = deployment._point

    def point_with_failed_control_update(name, version):
        if name == "control" and version == "2":
            deployment.aws.agentcore_control.statuses["control"] = "UPDATE_FAILED"
            raise RuntimeError("Control update failed")
        point(name, version)

    deployment._point = point_with_failed_control_update

    with pytest.raises(RuntimeError, match="Control update failed"):
        deployment.run(IMAGE, 60)

    assert deployment.aws.agentcore_control.endpoints == {"control": "1", "treatment": "1"}
    assert deployment.aws.agentcore_control.statuses["control"] == "READY"
    ab_test_id = deployment.state["ab_test_id"]
    assert deployment.aws.agentcore.tests[ab_test_id]["executionStatus"] == "STOPPED"


def test_rollback_is_a_no_op_after_promotion(deployment):
    deployment.run(IMAGE, 60)

    deployment.rollback()

    assert deployment.state["finished"] == "promoted"
    assert deployment.aws.agentcore_control.endpoints == {"control": "2", "treatment": "2"}


# --- Cleanup: A/B test and ephemeral evaluation configs ---


def test_promote_deletes_ephemeral_evaluation_configs(deployment):
    deployment.run(IMAGE, 60)

    assert deployment.aws.agentcore_control.ephemeral_configs == {}
    assert deployment.state["ephemeral_control_config_id"] is None
    assert deployment.state["ephemeral_treatment_config_id"] is None


def test_rollback_deletes_ephemeral_evaluation_configs(deployment):
    deployment.quality_gates = {"Builtin.Helpfulness": 0.99}

    with pytest.raises(QualityGateFailedError):
        deployment.run(IMAGE, 60)

    assert deployment.aws.agentcore_control.ephemeral_configs == {}
    assert deployment.state["ephemeral_control_config_id"] is None
    assert deployment.state["ephemeral_treatment_config_id"] is None


@pytest.mark.parametrize(
    ("finish", "outcome"),
    [("promote_candidate", "promoted"), ("rollback", "rolled_back")],
    ids=["promote", "rollback"],
)
def test_finishing_tolerates_ab_test_already_stopped(deployment, finish, outcome):
    deployment.observe_candidate(IMAGE, 60)
    update_ab_test = deployment.aws.agentcore.update_ab_test

    def update_ab_test_already_stopped(**kwargs):
        if kwargs.get("executionStatus") == "STOPPED":
            raise ConflictException("AB test is already stopped")
        return update_ab_test(**kwargs)

    deployment.aws.agentcore.update_ab_test = update_ab_test_already_stopped

    getattr(deployment, finish)()

    assert deployment.state["finished"] == outcome


def test_promote_tolerates_ab_test_already_deleted(deployment):
    deployment.observe_candidate(IMAGE, 60)
    del deployment.aws.agentcore.tests[deployment.state["ab_test_id"]]

    deployment.promote_candidate()

    assert deployment.state["finished"] == "promoted"


def test_promote_continues_when_aws_fails_to_delete_evaluation_configs(deployment):
    deployment.observe_candidate(IMAGE, 60)
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "DeleteOnlineEvaluationConfig",
    )
    deployment.aws.delete_evaluation_config = _raise(throttled)

    deployment.promote_candidate()

    assert deployment.state["finished"] == "promoted"
    assert deployment.state["ephemeral_control_config_id"] is None
    assert deployment.state["ephemeral_treatment_config_id"] is None


def test_promote_surfaces_non_aws_errors_from_evaluation_config_cleanup(deployment):
    deployment.observe_candidate(IMAGE, 60)
    deployment.aws.delete_evaluation_config = _raise(
        RuntimeError("programming error, not an AWS failure")
    )

    with pytest.raises(RuntimeError, match="programming error"):
        deployment.promote_candidate()
