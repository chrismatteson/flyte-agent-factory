import hashlib
import json
import multiprocessing
import os
import re

import pytest

from flyte_agent_factory import _seal_record

SEAL = {"net": True, "guard": True, "tools_off": True, "fs_readonly": True, "spec_sha256": "ab" * 32}


def register(root, role_id="author-1", kind="author", **kwargs):
    return _seal_record.register(root, role_id, kind, engine="claude", seal=SEAL, **kwargs)


def read(path):
    return json.loads(path.read_text())


def _register_in_process(args):
    root, role_id = args
    register(root, role_id, kind="subagent")


@pytest.fixture
def fake_claude_config(tmp_path):
    config = tmp_path / "claude-config"
    session = config / "projects" / "-work-repo"
    (session / "sess-1" / "subagents").mkdir(parents=True)
    (session / "sess-1.jsonl").write_text('{"type":"user"}\n{"type":"assistant"}\n')
    (session / "sess-1" / "subagents" / "agent-x.jsonl").write_text('{"type":"user","agentId":"x"}\n')
    (session / "sess-1" / "subagents" / "agent-x.meta.json").write_text('{"agentType":"Explore"}')
    (config / "settings.json").write_text("{}")  # not a transcript
    (config / "projects" / "-work-repo" / "notes.txt").write_text("not a transcript")
    return config


def finished_role(root, role_id="subagent-1", kind="subagent", lines=("a", "b")):
    role_dir = register(root, role_id, kind=kind)
    (role_dir / "guard.jsonl").write_text("".join(f"{line}\n" for line in lines))
    _seal_record.finish(root, role_id, 0)
    return role_dir


def test_new_role_id_has_kind_timestamp_and_six_hex():
    role_id = _seal_record.new_role_id("reviewer")

    assert re.fullmatch(r"reviewer-\d{8}T\d{6}Z-[0-9a-f]{6}", role_id)
    assert _seal_record.new_role_id("reviewer") != role_id


def test_register_writes_the_manifest_and_role_json(tmp_path):
    role_dir = register(tmp_path, "author-1", parent="", action="a0")

    assert role_dir == tmp_path / "roles" / "author-1"
    manifest = read(tmp_path / "manifest.json")
    assert manifest["spec_sha256"] == SEAL["spec_sha256"]
    [entry] = manifest["roles"]
    assert (entry["role_id"], entry["kind"]) == ("author-1", "author")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["registered_at"])
    role = read(role_dir / "role.json")
    assert role["kind"] == "author"
    assert role["engine"] == "claude"
    assert role["action"] == "a0"
    assert role["seal"] == SEAL
    assert role["started_at"].endswith("Z")
    assert (role_dir / "guard.jsonl").read_text() == ""
    assert (role_dir / "proxy.jsonl").read_text() == ""


def test_register_twice_keeps_one_manifest_entry(tmp_path):
    register(tmp_path)
    register(tmp_path)

    assert len(read(tmp_path / "manifest.json")["roles"]) == 1


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"role_id": "../escape"}, "invalid role id"),
        ({"role_id": "a/b"}, "invalid role id"),
        ({"kind": "wizard"}, "unknown role kind"),
    ],
)
def test_register_refuses_a_bad_id_or_kind(tmp_path, kwargs, match):
    with pytest.raises(ValueError, match=match):
        register(tmp_path, **kwargs)


def test_register_refuses_an_unknown_engine_and_an_incomplete_seal(tmp_path):
    with pytest.raises(ValueError, match="unknown engine"):
        _seal_record.register(tmp_path, "r", "author", engine="gpt", seal=SEAL)
    with pytest.raises(ValueError, match="tools_off"):
        _seal_record.register(tmp_path, "r", "author", engine="claude", seal={"net": True})


def test_register_refuses_a_finished_role(tmp_path):
    finished_role(tmp_path, "author-1", "author")

    with pytest.raises(ValueError, match="finished"):
        register(tmp_path, "author-1")


def test_role_paths_name_the_record_files(tmp_path):
    paths = _seal_record.role_paths(tmp_path, "r1")

    base = tmp_path / "roles" / "r1"
    assert (paths.dir, paths.proxy_log, paths.guard_log) == (base, base / "proxy.jsonl", base / "guard.jsonl")
    assert (paths.transcript_dir, paths.end) == (base / "transcript", base / "end.json")


def test_finish_writes_end_json_with_a_hash_and_line_count_for_every_other_file(tmp_path):
    role_dir = register(tmp_path)
    (role_dir / "guard.jsonl").write_text('{"a":1}\n{"a":2}\n')
    (role_dir / "proxy.jsonl").write_text('{"b":1}')  # no trailing newline still counts as a line
    (role_dir / "transcript" / "projects").mkdir()
    (role_dir / "transcript" / "projects" / "s.jsonl").write_text("x\ny\nz\n")

    end = _seal_record.finish(tmp_path, "author-1", 3)

    data = read(end)
    assert end == role_dir / "end.json"
    assert (data["exit_code"], data["ended_at"].endswith("Z")) == (3, True)
    assert set(data["files"]) == {"role.json", "guard.jsonl", "proxy.jsonl", "transcript/projects/s.jsonl"}
    guard = data["files"]["guard.jsonl"]
    assert guard == {"sha256": hashlib.sha256(b'{"a":1}\n{"a":2}\n').hexdigest(), "lines": 2}
    assert data["files"]["proxy.jsonl"]["lines"] == 1
    assert data["files"]["transcript/projects/s.jsonl"]["lines"] == 3


def test_finish_refuses_an_unregistered_or_finished_role(tmp_path):
    with pytest.raises(FileNotFoundError):
        _seal_record.finish(tmp_path, "nope", 0)
    finished_role(tmp_path, "r1")
    with pytest.raises(ValueError, match="already finished"):
        _seal_record.finish(tmp_path, "r1", 0)


def test_a_file_changed_after_finish_no_longer_matches_end_json(tmp_path):
    role_dir = finished_role(tmp_path)
    (role_dir / "guard.jsonl").write_text("tampered\n")

    recorded = read(role_dir / "end.json")["files"]["guard.jsonl"]["sha256"]
    assert recorded != hashlib.sha256(b"tampered\n").hexdigest()


def test_concurrent_registrations_from_eight_processes_keep_every_role(tmp_path):
    ids = [f"subagent-{n}" for n in range(8)]

    with multiprocessing.get_context("spawn").Pool(8) as pool:
        pool.map(_register_in_process, [(tmp_path, role_id) for role_id in ids])

    manifest = read(tmp_path / "manifest.json")
    assert sorted(r["role_id"] for r in manifest["roles"]) == ids
    assert all((tmp_path / "roles" / role_id / "role.json").is_file() for role_id in ids)


def test_collect_claude_preserves_the_subagents_layout(tmp_path, fake_claude_config):
    role_dir = register(tmp_path)

    copied = _seal_record.collect_claude(fake_claude_config, role_dir)

    base = role_dir / "transcript" / "projects" / "-work-repo"
    assert sorted(p.relative_to(role_dir / "transcript").as_posix() for p in copied) == [
        "projects/-work-repo/sess-1.jsonl",
        "projects/-work-repo/sess-1/subagents/agent-x.jsonl",
        "projects/-work-repo/sess-1/subagents/agent-x.meta.json",
    ]
    assert (base / "sess-1.jsonl").read_text() == (
        fake_claude_config / "projects/-work-repo/sess-1.jsonl"
    ).read_text()
    assert json.loads((base / "sess-1" / "subagents" / "agent-x.meta.json").read_text()) == {
        "agentType": "Explore"
    }
    assert not (role_dir / "transcript" / "settings.json").exists()
    assert not (base / "notes.txt").exists()


def test_collect_claude_of_an_empty_config_dir_copies_nothing(tmp_path):
    role_dir = register(tmp_path)

    assert _seal_record.collect_claude(tmp_path / "empty", role_dir) == []


def test_collect_codex_copies_the_rollouts(tmp_path):
    home = tmp_path / "codex"
    rollout = home / "sessions" / "2026" / "11" / "16" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"response_item"}\n')
    (home / "auth.json").write_text("{}")  # credentials must never be copied
    role_dir = register(tmp_path / "rec")

    _seal_record.collect_codex(home, role_dir)

    assert (
        role_dir / "transcript" / "sessions/2026/11/16/rollout-1.jsonl"
    ).read_text() == rollout.read_text()
    assert not list((role_dir / "transcript").rglob("auth.json"))


def test_bundle_and_merge_round_trip(tmp_path, fake_claude_config):
    child = tmp_path / "child-record"
    role_dir = register(child, "subagent-1", kind="subagent", parent="author-1")
    _seal_record.collect_claude(fake_claude_config, role_dir)
    _seal_record.finish(child, "subagent-1", 0)
    out = tmp_path / "returned-dir"
    out.mkdir()

    bundled = _seal_record.bundle(child, "subagent-1", out)
    parent = tmp_path / "parent-record"
    register(parent, "author-1")
    merged = _seal_record.merge(out, parent)

    assert bundled == out / ".seal-record" / "roles" / "subagent-1"
    assert merged == ["subagent-1"]
    assert _seal_record._describe_tree(parent / "roles" / "subagent-1") == _seal_record._describe_tree(
        role_dir
    )
    assert (parent / "roles" / "subagent-1" / "end.json").read_text() == (role_dir / "end.json").read_text()
    assert sorted(r["role_id"] for r in read(parent / "manifest.json")["roles"]) == ["author-1", "subagent-1"]


def test_merge_is_idempotent_for_identical_content(tmp_path):
    child = tmp_path / "child"
    finished_role(child)
    out = tmp_path / "out"
    out.mkdir()
    _seal_record.bundle(child, "subagent-1", out)
    parent = tmp_path / "parent"

    _seal_record.merge(out, parent)
    _seal_record.merge(out, parent)

    assert len(read(parent / "manifest.json")["roles"]) == 1


def test_merge_refuses_to_overwrite_a_finished_role_with_different_content(tmp_path):
    child = tmp_path / "child"
    finished_role(child, lines=("a", "b"))
    out = tmp_path / "out"
    out.mkdir()
    _seal_record.bundle(child, "subagent-1", out)
    parent = tmp_path / "parent"
    finished_role(parent, lines=("other",))

    with pytest.raises(ValueError, match="different content"):
        _seal_record.merge(out, parent)

    assert (parent / "roles" / "subagent-1" / "guard.jsonl").read_text() == "other\n"


def test_merge_replaces_the_placeholder_a_caller_registered(tmp_path):
    child = tmp_path / "child"
    finished_role(child)
    out = tmp_path / "out"
    out.mkdir()
    _seal_record.bundle(child, "subagent-1", out)
    parent = tmp_path / "parent"
    register(parent, "subagent-1", kind="subagent")

    _seal_record.merge(out, parent)

    assert (parent / "roles" / "subagent-1" / "end.json").is_file()
    assert (parent / "roles" / "subagent-1" / "guard.jsonl").read_text() == "a\nb\n"


def test_a_registered_role_that_is_never_merged_has_no_dir_and_no_end(tmp_path):
    child = tmp_path / "child"
    finished_role(child, "subagent-1")
    out = tmp_path / "out"
    out.mkdir()
    _seal_record.bundle(child, "subagent-1", out)
    parent = tmp_path / "parent"
    register(parent, "subagent-1", kind="subagent", create_dir=False)
    register(parent, "subagent-2", kind="subagent", create_dir=False)  # its pod was lost: no bundle

    _seal_record.merge(out, parent)

    assert sorted(r["role_id"] for r in read(parent / "manifest.json")["roles"]) == [
        "subagent-1",
        "subagent-2",
    ]
    assert (parent / "roles" / "subagent-1" / "end.json").is_file()
    assert not (parent / "roles" / "subagent-2").exists()


def test_merge_of_a_dir_with_no_bundle_does_nothing(tmp_path):
    assert _seal_record.merge(tmp_path / "nothing", tmp_path / "parent") == []
    assert not (tmp_path / "parent").exists()


def test_bundle_refuses_an_unfinished_role(tmp_path):
    register(tmp_path)

    with pytest.raises(FileNotFoundError, match="not finished"):
        _seal_record.bundle(tmp_path, "author-1", tmp_path / "out")


def test_merge_refuses_a_bundled_role_with_a_hostile_id(tmp_path):
    out = tmp_path / "out"
    (out / ".seal-record" / "roles" / "ok").mkdir(parents=True)
    (out / ".seal-record" / "roles" / "ok" / "end.json").write_text("{}")
    (out / ".seal-record" / "roles" / ".hidden").mkdir()
    (out / ".seal-record" / "roles" / ".hidden" / "end.json").write_text("{}")

    with pytest.raises(ValueError, match="invalid role id"):
        _seal_record.merge(out, tmp_path / "parent")


def test_copy_regular_refuses_a_link_a_fifo_and_a_linked_parent(tmp_path):
    within, secret = tmp_path / "config", tmp_path / "secret"
    (within / "projects").mkdir(parents=True)
    secret.write_text("harness only")
    (within / "plain.jsonl").write_text("ok")
    (within / "link.jsonl").symlink_to(secret)
    os.mkfifo(within / "fifo.jsonl")
    (within / "dir.jsonl").mkdir()
    (within / "linked").symlink_to(tmp_path, target_is_directory=True)
    out = tmp_path / "out"
    assert _seal_record.copy_regular(within / "plain.jsonl", out / "plain", within)
    assert (out / "plain").read_text() == "ok"
    for name in ["link.jsonl", "fifo.jsonl", "dir.jsonl", "linked/secret", "missing.jsonl"]:
        assert not _seal_record.copy_regular(within / name, out / name, within), name
        assert not (out / name).exists()


def test_collect_claude_does_not_follow_a_planted_link(tmp_path):
    config, secret = tmp_path / "claude", tmp_path / "secret.txt"
    session = config / "projects" / "p"
    session.mkdir(parents=True)
    secret.write_text("harness only")
    (session / "s.jsonl").write_text('{"type":"user"}\n')
    (session / "leak.jsonl").symlink_to(secret)
    (session / "dir.jsonl").mkdir()
    (config / "projects" / "q").symlink_to(tmp_path, target_is_directory=True)
    role_dir = register(tmp_path / "rec")
    copied = _seal_record.collect_claude(config, role_dir)
    assert [p.relative_to(role_dir / "transcript").as_posix() for p in copied] == ["projects/p/s.jsonl"]
    copies = [p.read_text() for p in (role_dir / "transcript").rglob("*") if p.is_file()]
    assert not any("harness only" in text for text in copies)


def test_collect_codex_does_not_follow_a_planted_link(tmp_path):
    home, secret = tmp_path / "codex", tmp_path / "auth.json"
    (home / "sessions").mkdir(parents=True)
    secret.write_text('{"token":"x"}')
    (home / "sessions" / "rollout-1.jsonl").symlink_to(secret)
    role_dir = register(tmp_path / "rec")
    assert _seal_record.collect_codex(home, role_dir) == []
