"""Installation tokens are limited to one repository and to the permissions the caller names."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from flyte_agent_factory import _github
from tests.test_github import PRIVATE_PEM, REPO, Clock, FakeGithub


@pytest.fixture
def gh():
    clock = Clock()
    fake = FakeGithub(clock)
    client = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(fake.handler))
    fake.app = _github.GithubApp("12345", PRIVATE_PEM, client=client, clock=clock)
    return fake


def minted_bodies(gh: FakeGithub) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in gh.requests if r.url.path.endswith("/access_tokens")]


def test_every_token_is_limited_to_the_one_repository(gh):
    gh.app.installation_token(REPO, _github.CONTENTS_READ)
    assert minted_bodies(gh) == [{"repositories": ["factory"], "permissions": {"contents": "read"}}]


def test_a_token_without_named_permissions_cannot_be_asked_for(gh):
    # A mint without `permissions` would grant everything the installation holds (#179).
    with pytest.raises(TypeError):
        gh.app.installation_token(REPO)
    with pytest.raises(ValueError, match="must name its permissions"):
        gh.app.installation_token(REPO, {})
    assert minted_bodies(gh) == []


def test_an_app_made_without_permissions_refuses_api_calls(gh):
    with pytest.raises(ValueError, match="names no permissions"):
        gh.app.default_branch_sha(REPO)
    with pytest.raises(ValueError, match="names no permissions"):
        gh.app._call(REPO, "GET", "/x")
    assert minted_bodies(gh) == []


def test_an_app_made_with_permissions_mints_only_those(gh):
    app = _github.GithubApp(
        "12345", PRIVATE_PEM, client=gh.app._http, clock=gh.app._clock, permissions=_github.ISSUES_READ
    )
    app._token(REPO)
    assert minted_bodies(gh) == [{"repositories": ["factory"], "permissions": {"issues": "read"}}]


def test_permissions_are_posted_and_cached_per_scope(gh):
    ci = gh.app.installation_token(REPO, _github.CI_READ)
    push = gh.app.installation_token(REPO, _github.session_push_scope(False))
    assert ci != push
    assert gh.app.installation_token(REPO, _github.CI_READ) == ci
    assert minted_bodies(gh) == [
        {
            "repositories": ["factory"],
            "permissions": {"checks": "read", "statuses": "read", "actions": "read"},
        },
        {"repositories": ["factory"], "permissions": {"contents": "write"}},
    ]


def test_the_workflows_permission_is_asked_for_only_when_the_profile_says_so():
    assert "workflows" not in _github.session_push_scope(False)
    assert _github.session_push_scope(True)["workflows"] == "write"


def test_a_scoped_view_mints_its_scope_and_redact_knows_its_token(gh):
    verifier = gh.app.scoped(_github.VERIFIER)
    token = verifier.installation_token(REPO, _github.VERIFIER)
    assert gh.app.redact(f"oops {token}") == "oops ***"  # one cache, shared with the parent


def test_a_401_drops_only_the_scoped_token_and_retries_once(gh):
    app = gh.app.scoped(_github.CI_READ)
    first = app._token(REPO)
    other = gh.app.installation_token(REPO, _github.VERIFIER)
    seen: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/x":
            response: httpx.Response = gh.handler(request)  # installation lookups and token mints still work
            return response
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={})

    app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(send))
    app._call(REPO, "GET", "/x")
    assert seen[0] == f"Bearer {first}" and seen[1] != seen[0]
    assert gh.app.installation_token(REPO, _github.VERIFIER) == other  # untouched


def test_fresh_replaces_a_token_that_is_still_valid(gh):
    first = gh.app.installation_token(REPO, _github.CONTENTS_READ)
    assert gh.app.installation_token(REPO, _github.CONTENTS_READ, fresh=True) != first


@pytest.mark.parametrize("scope", [_github.CI_READ, _github.PR_WRITE, _github.VERIFIER])
def test_named_scopes_are_read_only_views(scope):
    with pytest.raises(TypeError):
        scope["contents"] = "write"
