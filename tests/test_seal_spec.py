import json
from dataclasses import fields

import pytest

from flyte_agent_factory import _seal_spec
from flyte_agent_factory._seal_spec import SealSpec

SHA = "a" * 40
VALID = {
    "task_repo": "https://github.com/example/task.git",
    "base_sha": SHA,
    "task_project": "Task_Project",
    "index_cutoff": "2025-03-01",
    "push_remote": "https://github.com/example/workspace.git",
    "forbidden_paths": ["/data/gold/*"],
}


def test_round_trip_and_defaults():
    spec = SealSpec.from_mapping(VALID)
    assert spec.model_hosts == ("api.anthropic.com",)
    assert spec.allow_index is True
    assert spec.index_cutoff == "2025-03-01T00:00:00Z"
    assert SealSpec.from_json(spec.to_json()) == spec


def test_json_is_canonical_and_hash_is_stable():
    spec = SealSpec.from_mapping(VALID)
    reordered = SealSpec.from_mapping(dict(reversed(list(VALID.items()))))
    assert spec.to_json() == reordered.to_json()
    assert " " not in spec.to_json().replace("Task_Project", "")
    assert list(json.loads(spec.to_json())) == sorted(json.loads(spec.to_json()))
    assert spec.sha256() == reordered.sha256()
    assert len(spec.sha256()) == 64
    assert SealSpec.from_mapping({**VALID, "base_sha": "b" * 40}).sha256() != spec.sha256()


def test_cutoff_is_normalised_to_utc():
    spec = SealSpec.from_mapping({**VALID, "index_cutoff": "2025-03-01T02:00:00+02:00"})
    assert spec.index_cutoff == "2025-03-01T00:00:00Z"
    spec = SealSpec.from_mapping({**VALID, "index_cutoff": "2025-03-01T10:00:00Z"})
    assert spec.index_cutoff == "2025-03-01T10:00:00Z"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_repo", "http://github.com/example/task.git"),
        ("task_repo", ""),
        ("task_repo", 5),
        ("base_sha", "abc123"),
        ("base_sha", "G" * 40),
        ("task_project", 3),
        ("index_cutoff", "yesterday"),
        ("index_cutoff", ""),
        ("model_hosts", "api.anthropic.com"),
        ("model_hosts", ["https://api.anthropic.com"]),
        ("model_hosts", []),
        ("push_remote", "http://example.com/w.git"),
        ("forbidden_paths", "/x"),
        ("forbidden_paths", [1]),
        ("allow_index", "yes"),
    ],
)
def test_invalid_field_is_named(field, value):
    with pytest.raises(ValueError, match=field):
        SealSpec.from_mapping({**VALID, field: value})


def test_missing_required_field_is_named():
    with pytest.raises(ValueError, match="base_sha"):
        SealSpec.from_mapping({k: v for k, v in VALID.items() if k != "base_sha"})


def test_unknown_key_is_rejected():
    with pytest.raises(ValueError, match="surprise"):
        SealSpec.from_mapping({**VALID, "surprise": 1})


def test_from_json_rejects_non_object_and_bad_json():
    with pytest.raises(ValueError, match="JSON"):
        SealSpec.from_json("[1]")
    with pytest.raises(ValueError, match="JSON"):
        SealSpec.from_json("{nope")


def test_from_toml(tmp_path):
    path = tmp_path / "seal.toml"
    path.write_text(
        f'task_repo = "{VALID["task_repo"]}"\nbase_sha = "{SHA}"\nindex_cutoff = 2025-03-01\n'
        'task_project = "x"\nallow_index = false\n'
    )
    spec = SealSpec.from_toml(path)
    assert spec.index_cutoff == "2025-03-01T00:00:00Z"
    assert spec.allow_index is False


def test_normalized_project_follows_pep_503():
    spec = SealSpec.from_mapping({**VALID, "task_project": "Foo__Bar.baz--Qux"})
    assert spec.normalized_project() == "foo-bar-baz-qux"
    assert SealSpec.from_mapping({**VALID, "task_project": ""}).normalized_project() == ""


def test_model_hosts_for_engine():
    assert _seal_spec.model_hosts_for("claude") == ("api.anthropic.com",)
    assert _seal_spec.model_hosts_for("codex") == ("api.anthropic.com", "api.openai.com", "chatgpt.com")


def test_task_dir_defaults_to_repo_and_must_stay_inside():
    assert SealSpec.from_mapping(VALID).task_dir == "repo"
    assert SealSpec.from_mapping({**VALID, "task_dir": "sub/task"}).task_dir == "sub/task"
    for bad in ("", "/abs", "../up", "a/../../b", 3):
        with pytest.raises(ValueError, match="task_dir"):
            SealSpec.from_mapping({**VALID, "task_dir": bad})


def test_spec_has_no_field_for_gold_or_patch_content():
    # The spec rides on the queue Request, which anyone with the repository can read.
    names = {f.name for f in fields(SealSpec)}
    assert not [n for n in names if "gold" in n or "patch" in n or "fix" in n]
