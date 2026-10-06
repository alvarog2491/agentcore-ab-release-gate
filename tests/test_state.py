"""State: reading and writing the recovery journal."""

import pytest

from agentcore_release_gate.exceptions import StateJournalError
from agentcore_release_gate.state import DeploymentState, load_state, save_state


def test_load_state_returns_empty_state_when_journal_is_missing(tmp_path):
    assert load_state(tmp_path / "state.json") == DeploymentState()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{truncated", "not valid JSON"),
        ("[]", "must contain a JSON object"),
        ('{"abtest_id": "typo"}', "has unknown fields"),
    ],
    ids=["corrupt", "not-an-object", "unknown-field"],
)
def test_load_state_rejects_unreadable_journal(tmp_path, content, message):
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(StateJournalError, match=message):
        load_state(path)


def test_save_state_round_trips_through_load_state(tmp_path):
    path = tmp_path / "state.json"
    state = DeploymentState(
        baseline="1",
        control_endpoint_name="prod",
        quality_gates={"Builtin.Helpfulness": 0.7},
        promoting=True,
        finished="rolled_back",
    )

    save_state(path, state)

    assert load_state(path) == state
    assert not path.with_suffix(".tmp").exists()
