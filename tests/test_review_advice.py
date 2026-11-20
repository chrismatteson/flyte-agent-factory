"""A review nobody could run reaches the owner with its cause and a recommendation, never bare (#53)."""

from __future__ import annotations

from pathlib import Path

import pytest
from flyte.io import Dir

from flyte_agent_factory import _bridge_triage, _review, _review_advice, _reviewer, _timeline, _union_identity

#: What every reviewer said in the run of #53 (personal-wordstats on an OSS cluster).
OLD_ENGINE_ON_OSS = "NOT_RUN: IdentityMissing: " + _union_identity._MISSING.format(
    tier="unknown", secret="unknown"
)
UNDETERMINED = "NOT_RUN: IdentityMissing: " + _union_identity.missing_message("", "unknown", "unknown")


def test_the_reasons_are_summarised_once_each_with_their_reviewers():
    text = _review_advice.summarise({"craft": "boom", "correctness": "boom", "completeness": "other"})
    assert text == "completeness: other; correctness, craft: boom"


def test_a_long_reason_is_cut():
    text = _review_advice.summarise({"craft": "x" * 1000})
    assert len(text) < 1000 and text.endswith("...")


@pytest.mark.parametrize("reasons", [OLD_ENGINE_ON_OSS, UNDETERMINED])
def test_reviewers_that_could_not_tell_they_are_on_oss_are_told_to_redeploy_not_to_make_keys(reasons):
    advice = _review_advice.advise(reasons, "oss")
    assert "OSS" in advice.diagnosis
    assert advice.recommendation.startswith("redeploy the factory")
    assert "Do not run ops/bootstrap.py --union-keys" in advice.recommendation


def test_an_undetermined_identity_is_diagnosed_whatever_the_supervisor_knows():
    assert _review_advice.advise(UNDETERMINED, "").recommendation.startswith("redeploy the factory")


def test_a_union_pod_missing_its_key_is_still_sent_to_union_keys():
    advice = _review_advice.advise(OLD_ENGINE_ON_OSS, "union")
    assert "--union-keys" in advice.recommendation and "Do not" not in advice.recommendation


def test_an_oss_profile_with_keys_is_told_to_drop_them():
    reasons = _union_identity.missing_message("oss", "session", "s")
    assert "remove the [union] table" in _review_advice.advise(reasons, "oss").recommendation


def test_an_unknown_failure_still_gets_a_recommendation():
    advice = _review_advice.advise("NOT_RUN: RuntimeError: pod never scheduled", "union")
    assert advice.diagnosis and advice.recommendation.startswith("retry the review")


@pytest.mark.parametrize(
    "reasons", [OLD_ENGINE_ON_OSS, UNDETERMINED, "pod never scheduled", "Union refused x"]
)
@pytest.mark.parametrize("kind", ["oss", "union", ""])
def test_the_ask_carries_a_diagnosis_a_recommendation_and_a_default_and_is_safe_in_quotes(reasons, kind):
    diagnosis, ask = _review_advice.escalation(reasons, kind)
    assert diagnosis.startswith("Diagnosis: ") and "Recommendation: " in diagnosis
    assert "Diagnosis: " in ask and "My recommendation: " in ask and "Default if no answer: " in ask
    assert '"' not in ask and "`" not in ask  # it is sent inside `ask-human "..."`


def test_the_escalation_reads_the_supervisors_own_backend(monkeypatch):
    monkeypatch.setattr(_review_advice, "own_backend_kind", lambda: "oss")
    _, ask = _review_advice.escalation(OLD_ENGINE_ON_OSS)
    assert "redeploy the factory" in ask


@pytest.mark.asyncio
async def test_nobody_ran_carries_the_reviewers_reasons(monkeypatch):
    async def broken(**_):
        raise _union_identity.IdentityMissing(
            _union_identity._MISSING.format(tier="unknown", secret="unknown")
        )

    async def snapshot(_path):
        return object()

    monkeypatch.setattr(_reviewer, "_call_reviewer", broken)
    monkeypatch.setattr(Dir, "from_local", snapshot)
    with pytest.raises(_reviewer.ReviewUnavailable) as raised:
        await _review._evaluate(Path("."), "brief", "", 1, _timeline._Timeline())
    text = str(raised.value)
    assert text.startswith("none of the reviewers could run: ")
    assert "IdentityMissing: no Union identity for tier unknown" in text
    assert _review_advice.advise(text, "oss").recommendation.startswith("redeploy the factory")


def test_triage_without_a_judge_recommendation_shows_the_askers():
    _, ask = _review_advice.escalation(OLD_ENGINE_ON_OSS, "oss")
    shown = _bridge_triage._asker_recommendation(ask)
    assert shown.startswith("(the asker's) redeploy the factory")
    assert _bridge_triage._asker_recommendation("Should I retry?") == "(none given)"


def test_an_undetermined_identity_on_union_is_not_called_oss():
    advice = _review_advice.advise(UNDETERMINED, "union")
    assert advice.recommendation.startswith("redeploy the factory")
    assert "OSS" not in advice.diagnosis and "--union-keys" not in advice.recommendation


def test_quoted_reasons_are_safe_inside_a_double_quoted_shell_argument():
    cut = "NOT_RUN: IdentityMissing: store it with `python ops/bootst"  # a reason cut mid-backtick
    text = _review_advice.summarise({"craft": cut, "correctness": 'say "hi" $HOME \\ !'})
    assert not set(text) & set('`"$\\!')
    assert "python ops/bootst" in text


@pytest.mark.parametrize("key", ["BACKEND_KIND", "AGENT_FACTORY_BACKEND_KIND", "UNION_KEY", "UNION_TIER"])
def test_a_launch_policy_cannot_set_the_pods_identity_or_backend(key):
    from flyte_agent_factory import _factory_policy

    with pytest.raises(ValueError, match="only the deploy sets"):
        _factory_policy.normalize_policy({key: "oss"})


@pytest.mark.parametrize("kind,sent_to_union_keys", [("union", True), ("oss", False)])
def test_a_mounted_key_that_does_not_decode_is_advised_from_the_real_message(
    monkeypatch, kind, sent_to_union_keys
):
    """The reason is the one `establish` raises, so a rewording there cannot drop this branch silently."""
    from flyte_agent_factory import _profile, _profile_oss

    for var in (
        _profile.PROFILE_ENV,
        _profile.TOML_ENV,
        _union_identity.TIER_ENV,
        _union_identity.SECRET_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_profile, "PROFILES_DIR", Path("/nonexistent"))
    monkeypatch.setenv(_profile_oss.BACKEND_ENV, kind)
    monkeypatch.setenv(_union_identity.KEY_ENV, "not-a-key")
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    reason = f"none of the reviewers could run: craft: NOT_RUN: IdentityMissing: {raised.value}"
    assert _union_identity.BAD_KEY in reason
    advice = _review_advice.advise(reason, kind)
    assert ("--union-keys" in advice.recommendation) is sent_to_union_keys
    assert not advice.recommendation.startswith("retry the review")


_LONG_TIER = "acme-production-review-tier"
_LONG_SECRET = "acme-production-agent-factory-session-key"


@pytest.mark.parametrize(
    "kind,key,sent_to_union_keys",
    [
        ("union", "not-a-key", True),
        ("oss", "not-a-key", False),
        ("union", "", True),
        ("oss", "", False),
        ("", "", False),
    ],
)
def test_long_tier_and_secret_names_survive_the_reviewers_cut_reason(
    monkeypatch, kind, key, sent_to_union_keys
):
    """The reason goes through Flyte's wrapping and `_review.not_run_reason`'s cut, as a judge's does."""
    import flyte.errors

    from flyte_agent_factory import _profile, _profile_oss

    for var in (_profile.PROFILE_ENV, _profile.TOML_ENV, _union_identity.KEY_ENV, _profile_oss.BACKEND_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_profile, "PROFILES_DIR", Path("/nonexistent"))
    if kind:
        monkeypatch.setenv(_profile_oss.BACKEND_ENV, kind)
    if key:
        monkeypatch.setenv(_union_identity.KEY_ENV, key)
    monkeypatch.setenv(_union_identity.TIER_ENV, _LONG_TIER)
    monkeypatch.setenv(_union_identity.SECRET_ENV, _LONG_SECRET)
    with pytest.raises(_union_identity.IdentityMissing) as raised:
        _union_identity.establish()
    wrapped = flyte.errors.RuntimeUserError(
        "IdentityMissing", flyte.errors.CustomError.from_exception(raised.value).args[0]
    )
    reason = _review.not_run_reason(wrapped)
    assert len(reason) == 160  # the names really were long enough to be cut
    why = "none of the reviewers could run: " + _review_advice.summarise(
        {judge: f"{_review.NOT_RUN}: {reason}" for judge in ("contract", "craft")}
    )
    advice = _review_advice.advise(why, kind or "oss")
    # Told to run it, not told not to: the undetermined advice names `--union-keys` only to rule it out.
    assert ("python ops/bootstrap.py --union-keys" in advice.recommendation) is sent_to_union_keys
    assert not advice.recommendation.startswith("retry the review")
