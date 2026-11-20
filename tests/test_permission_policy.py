"""The author session's permission requests, decided in code (`_permission_policy`)."""

from __future__ import annotations

import types

import pytest

from flyte_agent_factory import _permission_policy
from flyte_agent_factory._permission_policy import Decision, Policy


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A settable `time.monotonic` for the policy module only."""
    now = [1000.0]
    monkeypatch.setattr(_permission_policy, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


def test_default_is_deny_with_guidance() -> None:
    decision = Policy().decide("Bash", {"command": "sudo reboot"})
    assert decision.behavior == "deny"
    assert not decision.interrupt
    assert "nobody can approve" in decision.message
    assert "ask-human" in decision.message
    # No `interrupt` key at all on a first denial: the turn goes on and the model changes approach.
    assert decision.to_control_response() == {"behavior": "deny", "message": decision.message}


@pytest.mark.parametrize(
    ("tool", "says"),
    [
        ("AskUserQuestion", "in-session question"),
        ("ExitPlanMode", "approve a plan"),
    ],
)
def test_tools_that_ask_a_person_get_their_own_guidance(tool: str, says: str) -> None:
    decision = Policy().decide(tool, {})
    assert decision.behavior == "deny"
    assert says in decision.message
    assert decision.message != Policy().decide("Bash", {}).message


def test_third_denial_of_one_tool_interrupts(clock: list[float]) -> None:
    policy = Policy()
    first = policy.decide("Bash", {})
    clock[0] += 10
    # Another tool's denials do not count towards this one.
    policy.decide("WebFetch", {})
    policy.decide("WebFetch", {})
    second = policy.decide("Bash", {})
    clock[0] += 10
    third = policy.decide("Bash", {})

    assert not first.interrupt and not second.interrupt
    assert "It has now been denied" not in first.message + second.message
    assert third.interrupt
    assert "It has now been denied 3 times" in third.message
    assert third.to_control_response()["interrupt"] is True
    assert third.to_control_response()["behavior"] == "deny"


def test_denials_outside_the_window_are_forgotten(clock: list[float]) -> None:
    policy = Policy(window_s=60.0)
    policy.decide("Bash", {})
    policy.decide("Bash", {})
    clock[0] += 61
    # The two earlier denials fell out of the window: this is the first again.
    assert not policy.decide("Bash", {}).interrupt
    clock[0] += 1
    assert not policy.decide("Bash", {}).interrupt
    assert policy.decide("Bash", {}).interrupt


def test_allow_list_allows_with_the_input_unchanged(clock: list[float]) -> None:
    policy = Policy(allow=frozenset({"Read"}))
    tool_input = {"file_path": "/etc/hostname"}
    for _ in range(5):
        decision = policy.decide("Read", tool_input)
        assert decision == Decision("allow", updated_input=tool_input)
        assert decision.to_control_response() == {"behavior": "allow", "updatedInput": tool_input}
    # Allowed calls are not denials: a later denial of another tool starts its own count.
    assert not policy.decide("Bash", {}).interrupt
