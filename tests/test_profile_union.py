"""The profile's [union] table and the [github] push/workflows fields."""

from __future__ import annotations

import pytest

from flyte_agent_factory import _profile, _profile_union

BASE = """\
name = "u"

[backend]
kind = "{kind}"
endpoint = "union.example.org"
project = "proj"
domain = "development"

[repo]
project = "git@github.com:example-org/sandbox.git"

[[slots]]
env = "agent-one"
subagent_env = "sub-one"

[secrets]
deploy_key = "sandbox_key"
claude_token = "work_claude_token"
{extra}
"""
UNION = '[union]\nproject_key = "factory-project-key"\ndeployer_key = "factory-deployer-key"\n'


def _parse(extra: str = UNION, kind: str = "union") -> _profile.Profile:
    return _profile.parse(BASE.format(kind=kind, extra=extra))


def test_tiers_default_to_the_project_key():
    union = _parse().union
    assert [union.key_for(t) for t in _profile_union.TIERS] == [
        "factory-project-key",
        "factory-project-key",
        "factory-project-key",
        "factory-deployer-key",
    ]


def test_session_and_queue_keys_are_optional_overrides():
    union = _parse(UNION + 'session_key = "s"\nqueue_key = "q"\n').union
    assert (union.key_for("session"), union.key_for("queue"), union.key_for("project")) == (
        "s",
        "q",
        "factory-project-key",
    )
    assert union.secrets == {"factory-project-key", "factory-deployer-key", "s", "q"}


def test_unknown_tier_is_an_error():
    with pytest.raises(ValueError, match="unknown Union tier 'review'"):
        _parse().union.key_for("review")


def test_union_backend_requires_the_table():
    with pytest.raises(_profile.ProfileError, match=r"\[union\] project_key is required for a Union backend"):
        _parse(extra="")


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('[union]\ndeployer_key = "d"\n', "project_key is required"),
        ('[union]\nproject_key = "p"\n', "deployer_key is required"),
        ('[union]\nproject_key = "p"\ndeployer_key = "d"\nreview_key = "r"\n', "unknown key.*review_key"),
        ('[union]\nproject_key = "p"\ndeployer_key = "d"\nsession_key = 3\n', "session_key must be the name"),
        ('[union]\nproject_key = "a b"\ndeployer_key = "d"\n', "project_key must be the name"),
    ],
)
def test_bad_union_tables(table, message):
    with pytest.raises(_profile.ProfileError, match=message):
        _parse(extra=table)


def test_oss_backend_needs_no_union_table():
    profile = _parse(extra="", kind="oss")
    assert profile.union == _profile_union.Union()
    assert profile.union.secrets == frozenset()


def test_union_must_be_a_table():
    with pytest.raises(_profile.ProfileError, match=r"\[union\] must be a table"):
        _profile.parse(BASE.format(kind="oss", extra="").replace("name =", 'union = "x"\nname =', 1))


def test_tolerant_parse_still_validates_the_union_table():
    # A deployer reading a newer profile (tolerate_unknown) must not skip checks on tables it knows.
    text = BASE.format(kind="union", extra=UNION + 'review_key = "r"\n')
    with pytest.raises(_profile.ProfileError, match="unknown key.*review_key"):
        _profile.parse(text, tolerate_unknown=True)


def test_bench_key_cannot_reuse_a_union_key():
    bench = (
        '[github]\napp_id = 1\nprivate_key_secret = "k"\n\n'
        '[bench]\nmax_usd = 1\nrepo = "git@github.com:example-org/b.git"\nrepo_key = "factory-deployer-key"\n'
    )
    with pytest.raises(_profile.ProfileError, match="repo_key must not be a secret the production profile"):
        _parse(extra=UNION + bench)


def test_github_push_and_workflows_defaults_and_values():
    github = _parse().github
    assert (github.push, github.workflows) == ("deploy_key", False)
    github = _parse(extra=UNION + '[github]\npush = "app"\nworkflows = true\n').github
    assert (github.push, github.workflows) == ("app", True)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('push = "ssh"', r'push must be "app" or "deploy_key"'),
        ("push = true", r'push must be "app" or "deploy_key"'),
        ('workflows = "yes"', "workflows must be true or false"),
    ],
)
def test_bad_github_fields(table, message):
    with pytest.raises(_profile.ProfileError, match=message):
        _parse(extra=UNION + f"[github]\n{table}\n")


def test_shipped_example_profiles_declare_a_union_table():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "profiles"
    for path in root.glob("example*.toml"):
        assert _profile.load(str(path)).union.project_key, path.name
