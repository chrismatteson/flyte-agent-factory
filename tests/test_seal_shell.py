"""The guard and the audit read a command the same way: both ask `_seal_shell`.

Every Bash command in the guard's and the audit's tables is run through both. The guard denies it exactly
when `find_routes` finds a route in it, judged on one machine (local roots /work and /tmp, cwd /work/repo).
The guard's forbidden-path denials are left out: reading a forbidden path is deciding, not reading a
command, and the guard alone knows the cwd a relative path resolves against.
"""

from __future__ import annotations

import pytest

from flyte_agent_factory import _seal_guard
from flyte_agent_factory import _seal_shell as shell
from flyte_agent_factory._seal_audit import NON_ROUTE_KINDS
from tests import test_seal_audit as audit_tests
from tests import test_seal_guard as guard_tests

LOCAL_ROOTS = ["/work", "/tmp"]
CWD = "/work/repo"

#: Sources the guard checks against the local roots and the audit judges from their text alone. A path
#: or a loopback URL outside the roots is local to the audit (it retrieves nothing from the reference
#: fix's host) but not to the guard, which allows only what it can see is under the roots.
LOCAL_SOURCE_DIFFERENCES = {
    "git fetch /elsewhere/repo",
    "git fetch file:///elsewhere/repo",
    "git fetch http://127.0.0.1:8000/x main",
}


def bash_commands(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
    """Return the string commands of the Bash calls in a guard table."""
    return [str(i["command"]) for tool, i in calls if tool == "Bash" and isinstance(i.get("command"), str)]


GUARD_COMMANDS = sorted(
    set(bash_commands([c for rule, c in guard_tests.DENIED if rule != "forbidden-path"]))
    | set(bash_commands(guard_tests.ALLOWED))
)
AUDIT_COMMANDS = sorted(
    set(audit_tests.GIT_FETCH_ROUTES)
    | set(audit_tests.NOT_GIT_FETCH)
    | {c for c, _ in audit_tests.HTTP_FETCH_CASES}
    | {c for c, _ in audit_tests.TASK_PACKAGE_CASES}
)


def guard_denies(command: str, task_project: str) -> bool:
    """Return whether the guard denies a Bash command on the parity machine."""
    config = {"task_project": task_project, "forbidden_paths": [], "local_roots": LOCAL_ROOTS}
    return not _seal_guard.decide("Bash", {"command": command}, config, cwd=CWD).allow


def audit_finds_route(command: str, task_project: str) -> bool:
    """Return whether the audit's reading finds any route in a Bash command, non-route kinds included."""
    return bool(shell.find_routes("Bash", {"command": command}, shell.Rules(task_project=task_project)))


def check_parity(command: str, task_project: str) -> None:
    """Assert the guard denies a command exactly when the audit finds a route, or the named difference."""
    denied, routed = guard_denies(command, task_project), audit_finds_route(command, task_project)
    if command in LOCAL_SOURCE_DIFFERENCES:
        assert denied and not routed, command
    else:
        assert denied == routed, command


@pytest.mark.parametrize("command", GUARD_COMMANDS)
def test_guard_table_commands_read_the_same_in_both(command: str):
    check_parity(command, "django")


@pytest.mark.parametrize("command", AUDIT_COMMANDS)
def test_audit_table_commands_read_the_same_in_both(command: str):
    check_parity(command, "tasklib")


def test_the_named_differences_are_in_the_tables():
    assert LOCAL_SOURCE_DIFFERENCES <= set(GUARD_COMMANDS) | set(AUDIT_COMMANDS)


@pytest.mark.parametrize(
    "command",
    [
        "git remote add up https://example.invalid/x.git",
        "git remote set-url origin https://github.com/acme/tasklib",
        "pip install ./dist/tasklib-1.2.tar.gz",
    ],
)
def test_non_route_kinds_are_denied_but_retrieve_nothing(command: str):
    found = shell.find_routes("Bash", {"command": command}, shell.Rules(task_project="tasklib"))
    assert found and {r.kind for r in found} <= NON_ROUTE_KINDS
    assert guard_denies(command, "tasklib")


def test_a_remote_added_with_fetch_is_a_fetch():
    found = shell.find_routes(
        "Bash", {"command": "git remote add -f up https://github.com/acme/tasklib"}, shell.Rules("tasklib")
    )
    assert [r.kind for r in found] == [shell.GIT_FETCH]
