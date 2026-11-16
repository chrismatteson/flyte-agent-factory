"""`ops/move_state.py`: one command moves the state between git, a bucket, a volume and the legacy layout."""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import pytest
from moto.server import ThreadedMotoServer

from flyte_agent_factory import _state_bucket, _state_store
from tests.state_git import make_remote

OPS = Path(__file__).resolve().parents[1] / "ops"


def _load():
    sys.path.insert(0, str(OPS))  # as running the script does: it imports migrate_state beside it
    spec = importlib.util.spec_from_file_location("ops_move_state", OPS / "move_state.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


move_state = _load()

# Every kind of key the factory writes: requests and markers, a lease, a report, an opaque body.
STATE = {
    "queue/alpha/0001.json": b'{"name": "alpha", "gen": 1}',
    "queue/alpha/0001.started": b"2026-01-01T00:00:00Z",
    "queue/.dispatch-lease.json": b'{"holder": "pod-1", "expires": 1.5e12}',
    "reserve.json": b'{"claude": 5.0}',
    "gardening/alpha.json": b"{}",
    "binary/blob": bytes(range(256)),
}


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_PROFILE", raising=False)
    monkeypatch.setattr(move_state, "_flyte_prefixes", lambda: ())
    move_state._memory.clear()


def _all(store: _state_store.Store) -> dict[str, bytes]:
    snap = store.snapshot(fresh=True)
    return dict(snap.read_many(snap.list()))


@pytest.fixture
def git_args(tmp_path) -> list[str]:
    remote = make_remote(tmp_path)
    return ["--git-remote", str(remote), "--git-branch", "factory-state/test-dev"]


@pytest.fixture
def git_source(tmp_path, git_args):
    store = _state_store.StateStore(
        git_args[1], git_args[3], tmp_path / "cache-source.git", ttl=0.0, retries=10
    )
    store.write(STATE, "seed")
    return store


def _git_store(tmp_path, git_args, name):
    return _state_store.StateStore(git_args[1], git_args[3], tmp_path / f"cache-{name}.git", ttl=0.0)


def test_git_to_memory_bucket_and_back_is_byte_identical(tmp_path, git_args, git_source, capsys):
    assert move_state.main(["--from", "git", "--to", "memory://b", *git_args]) == 0
    bucket = move_state.open_backend("memory://b")
    assert _all(bucket) == STATE
    # The way back goes onto another, empty branch: what comes out is what went in.
    (tmp_path / "other").mkdir()
    other = make_remote(tmp_path / "other")
    back = ["--git-remote", str(other), "--git-branch", "factory-state/test-dev"]
    assert move_state.main(["--from", "memory://b", "--to", "git", *back]) == 0
    restored = _state_store.StateStore(back[1], back[3], tmp_path / "cache-back.git", ttl=0.0)
    assert _all(restored) == STATE
    assert "unset bucket" in capsys.readouterr().out  # the switch-over line


def test_the_audit_records_are_not_copied(git_source, git_args):
    move_state.main(["--from", "git", "--to", "memory://b", *git_args])
    bucket = move_state.open_backend("memory://b")
    assert bucket.write({"x": b"1"}, "a change that leaves an audit record")
    assert bucket.recent_changes(5)
    move_state.main(["--from", "memory://b", "--to", "memory://c"])
    target = move_state.open_backend("memory://c")
    assert not any(key.startswith("audit/") for key in _all(target))
    assert target.recent_changes(5)[0]["message"] == "move state from memory://b"


def test_a_second_run_copies_nothing(git_source, git_args, capsys):
    move_state.main(["--from", "git", "--to", "memory://b", *git_args])
    bucket = move_state.open_backend("memory://b")
    before = bucket.recent_changes(100)
    capsys.readouterr()
    assert move_state.main(["--from", "git", "--to", "memory://b", *git_args]) == 0
    assert f"copied 0, same {len(STATE)}, conflict 0" in capsys.readouterr().out
    assert bucket.recent_changes(100) == before  # no write at all


def test_dry_run_prints_the_keys_and_writes_nothing(git_source, git_args, capsys):
    assert move_state.main(["--from", "git", "--to", "memory://b", "--dry-run", *git_args]) == 0
    out = capsys.readouterr().out
    assert f"would copy {len(STATE)}, same 0, conflict 0" in out
    for key in STATE:
        assert f"would copy: {key}" in out
    assert "switch over" not in out
    assert _all(move_state.open_backend("memory://b")) == {}


def test_dry_run_reports_what_only_the_destination_has(git_source, git_args, capsys):
    bucket = move_state.open_backend("memory://b")
    bucket.write({"queue/work/0009.json": b"{}"}, "the destination's own")
    assert move_state.main(["--from", "git", "--to", "memory://b", "--dry-run", *git_args]) == 1
    assert "only at destination (kept; --mirror deletes it): queue/work/0009.json" in capsys.readouterr().out
    assert move_state.main(["--from", "git", "--to", "memory://b", "--dry-run", "--mirror", *git_args]) == 0
    assert "would delete at the destination: queue/work/0009.json" in capsys.readouterr().out
    assert bucket.read("queue/work/0009.json") == b"{}"  # a dry run deletes nothing


def test_a_conflict_is_reported_and_kept_unless_overwrite(git_source, git_args, capsys):
    bucket = move_state.open_backend("memory://b")
    bucket.write({"reserve.json": b'{"claude": 1.0}'}, "the destination's own")
    assert move_state.main(["--from", "git", "--to", "memory://b", *git_args]) == 1
    captured = capsys.readouterr()
    out = captured.out
    assert "conflict (the destination's is kept" in out and "reserve.json" in out
    assert "switch over" not in out
    assert "--overwrite" in captured.err and "the stores differ" in captured.err
    assert bucket.read("reserve.json") == b'{"claude": 1.0}'
    assert bucket.read("gardening/alpha.json") == b"{}"  # the rest still came across
    assert move_state.main(["--from", "git", "--to", "memory://b", "--overwrite", *git_args]) == 0
    assert bucket.read("reserve.json") == STATE["reserve.json"]


def test_git_to_volume(tmp_path, git_source, git_args, capsys):
    volume = tmp_path / "volume"
    assert move_state.main(["--from", "git", "--to", f"file://{volume}", *git_args]) == 0
    assert _all(move_state.open_backend(f"file://{volume}")) == STATE
    out = capsys.readouterr().out
    assert f'volume = "{volume}"' in out and "volume_claim" in out


def test_a_relative_volume_is_refused(capsys):
    assert move_state.main(["--from", "memory://a", "--to", "file://relative/dir"]) == 1
    assert "absolute" in capsys.readouterr().err


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


def test_git_to_moto_s3_and_back(tmp_path, moto_endpoint, git_source, git_args, monkeypatch, capsys):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")  # moto accepts any; never a real credential
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    name = f"state-{uuid.uuid4().hex[:12]}"
    boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1").create_bucket(Bucket=name)
    uri = f"s3://{name}/factory"
    s3 = ["--endpoint", moto_endpoint, "--region", "us-east-1"]
    assert move_state.main(["--from", "git", "--to", uri, *s3, *git_args]) == 0
    out = capsys.readouterr().out
    assert f'bucket = "{uri}" region = "us-east-1" endpoint = "{moto_endpoint}"' in out
    bucket = _state_bucket.open_store(uri, endpoint=moto_endpoint, region="us-east-1")
    assert _all(bucket) == STATE
    assert move_state.main(["--from", "git", "--to", uri, *s3, *git_args]) == 0  # idempotent on S3 too
    assert f"copied 0, same {len(STATE)}" in capsys.readouterr().out
    other = tmp_path / "other"
    other.mkdir()
    back = ["--git-remote", str(make_remote(other)), "--git-branch", "factory-state/test-dev"]
    from_s3 = ["--from-endpoint", moto_endpoint, "--from-region", "us-east-1"]
    assert move_state.main(["--from", uri, *from_s3, "--to", "git", *back]) == 0
    assert _all(_state_store.StateStore(back[1], back[3], tmp_path / "cache-back.git", ttl=0.0)) == STATE


LEGACY = {
    "queue/alpha/0001.json": b'{"name": "alpha", "gen": 1}',
    "queue/alpha/0001.started": b"x",
    "reserve.json": b'{"claude": 5.0}',
    "checkpoints/alpha-run.json": b"{}",
}


def test_the_legacy_layout_moves_to_a_bucket(tmp_path, capsys):
    old = tmp_path / "old-bucket" / "prefix"
    for key, body in {**LEGACY, "waits.json": b"{}", "board/x.json": b"{}"}.items():
        (old / key).parent.mkdir(parents=True, exist_ok=True)
        (old / key).write_bytes(body)
    assert move_state.main(["--from-legacy", f"file://{old}", "--to", "memory://b"]) == 0
    out = capsys.readouterr().out
    assert f"copied {len(LEGACY)}, same 0, conflict 0, skipped 2" in out
    bucket = move_state.open_backend("memory://b")
    for key, body in LEGACY.items():
        assert bucket.read(key) == body
    assert bucket.read("waits.json") is None
    assert move_state.main(["--from-legacy", f"file://{old}", "--to", "memory://b"]) == 0
    assert f"copied 0, same {len(LEGACY)}" in capsys.readouterr().out


def test_an_empty_legacy_prefix_is_refused(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert move_state.main(["--from-legacy", f"file://{tmp_path / 'empty'}", "--to", "memory://b"]) == 2
    assert "no factory state" in capsys.readouterr().err


def test_a_flyte_bucket_destination_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(move_state, "_flyte_prefixes", lambda: ("gs://flyte-data/raw",))
    assert move_state.main(["--from", "memory://a", "--to", "gcs://flyte-data/other"]) == 1
    err = capsys.readouterr().err
    assert "Flyte's own data bucket" in err


def test_credentials_in_a_url_are_never_printed(capsys):
    code = move_state.main(
        [
            "--from",
            "git",
            "--to",
            "memory://b",
            "--git-remote",
            "https://tok3n@example.test/x.git",
            "--git-branch",
            "factory-state/x",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "tok3n" not in captured.out + captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["--from", "s3://b/f", "--from-endpoint", "http://AKX:EPSECRET@127.0.0.1:1", "--to", "memory://z"],
        ["--from", "s3://AKX:SuperSecret123@b/f", "--to", "memory://z"],
        ["--from", "memory://z", "--to", "s3://AKX:SuperSecret123@b/f"],
        ["--from", "memory://z", "--to", "s3://b/f", "--endpoint", "https://AKX:EPSECRET@example.test"],
        ["--from", "memory://z", "--to", "s3://b/f?token=SuperSecret123"],
        ["--from-legacy", "s3://AKX:SuperSecret123@b/p", "--to", "memory://z"],
        ["--from-legacy", "file:///x?token=SuperSecret123", "--to", "memory://z"],
        ["--from-legacy", "file:///x?token=SuperSecret123", "--to", "memory://z"],
        ["--browser", "me", "--from-profile-uri", "s3://AKX:SuperSecret123@b/p.tar.gz", "--to", "memory://z"],
    ],
)
def test_credentials_in_a_bucket_uri_or_endpoint_are_refused_and_never_printed(argv, capsys):
    assert move_state.main(argv) != 0
    captured = capsys.readouterr()
    shown = (captured.out + captured.err).lower()  # the key leaked lowercased once, via a library's message
    for secret in ("EPSECRET", "SuperSecret123", "AKX"):
        assert secret.lower() not in shown
    assert "Traceback" not in captured.err


def test_an_object_store_failure_prints_the_class_and_a_redacted_message(monkeypatch, capsys):
    # obstore puts the request URL in its messages; it must end in one redacted line, not a traceback.
    from obstore.exceptions import GenericError

    def boom(*args, **kwargs):
        raise GenericError("Error performing GET http://AKX:EPSECRET@127.0.0.1:1/x")

    monkeypatch.setattr(move_state, "copy_state", boom)
    assert move_state.main(["--from", "memory://a", "--to", "memory://b"]) == 1
    err = capsys.readouterr().err
    assert "GenericError" in err and "EPSECRET" not in err and "AKX" not in err


def test_the_switch_over_line_never_echoes_userinfo(capsys):
    line = move_state._switch_over_line(
        move_state._state_store._redact("s3://AKX:SuperSecret123@b/f"),
        endpoint="",
        region="",
        git_branch="",
    )
    assert "SuperSecret123" not in line and "AKX" not in line


def _moto_env(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")  # moto accepts any; never a real credential
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)


def test_an_empty_source_is_refused_and_mirror_never_empties_the_destination(git_source, git_args, capsys):
    # the destination branch holds state; the source is a mistyped (empty) memory bucket
    assert move_state.main(["--from", "memory://typo", "--to", "git", "--mirror", *git_args]) != 0
    assert _all(git_source) == STATE  # nothing deleted
    err = capsys.readouterr().err
    assert "no state keys" in err and "switch over" not in err
    assert (
        move_state.main(["--from", "memory://typo", "--to", "git", *git_args]) != 0
    )  # also without --mirror
    assert _all(git_source) == STATE
    # the explicit opt-in works only without --mirror
    assert move_state.main(["--from", "memory://typo", "--to", "memory://fresh", "--allow-empty-source"]) == 0
    code = move_state.main(
        ["--from", "memory://typo", "--to", "git", "--allow-empty-source", "--mirror", *git_args]
    )
    assert code != 0
    assert _all(git_source) == STATE


def test_a_mistyped_bucket_prefix_does_not_empty_the_git_branch(
    moto_endpoint, git_source, git_args, monkeypatch
):
    _moto_env(monkeypatch)
    name = f"state-{uuid.uuid4().hex[:12]}"
    boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1").create_bucket(Bucket=name)
    bucket = _state_bucket.open_store(f"s3://{name}/f", endpoint=moto_endpoint, region="us-east-1")
    bucket.write(STATE, "seed")
    args = ["--from-endpoint", moto_endpoint, "--from-region", "us-east-1", "--to", "git", "--mirror"]
    assert move_state.main(["--from", f"s3://{name}/fx", *args, *git_args]) != 0  # `f` typed as `fx`
    assert _all(git_source) == STATE


def _browser_moto(moto_endpoint):
    s3 = boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1")
    old, new = f"flyte-{uuid.uuid4().hex[:8]}", f"factory-{uuid.uuid4().hex[:8]}"
    s3.create_bucket(Bucket=old)
    s3.create_bucket(Bucket=new)
    return s3, old, new


def test_browser_archive_moves_between_moto_buckets(moto_endpoint, monkeypatch, capsys):
    _moto_env(monkeypatch)
    s3, old, new = _browser_moto(moto_endpoint)
    sealed = move_state.browser_archive.MAGIC + b"sealed bytes"
    s3.put_object(Bucket=old, Key="browser-github/profile.tar.gz", Body=b"legacy plaintext")
    s3.put_object(Bucket=old, Key="browser-github/profile.tar.gz.pending-1", Body=sealed)
    lease = {
        "archives": [f"{old}/browser-github/profile.tar.gz.pending-1", f"{old}/browser-github/profile.tar.gz"]
    }
    s3.put_object(Bucket=old, Key="browser-github/profile.tar.gz.lease", Body=json.dumps(lease).encode())
    args = [
        "--browser",
        "github",
        "--from-profile-uri",
        f"s3://{old}/browser-github/profile.tar.gz",
        "--from-endpoint",
        moto_endpoint,
        "--from-region",
        "us-east-1",
        "--to",
        f"s3://{new}/prod",
        "--endpoint",
        moto_endpoint,
        "--region",
        "us-east-1",
    ]
    assert move_state.main(args) == 0

    def get(key: str) -> bytes:
        return bytes(s3.get_object(Bucket=new, Key=key)["Body"].read())

    assert get("prod/browser/github/profile.tar.gz") == sealed
    assert get("prod/browser/github/profile.tar.gz.prev") == b"legacy plaintext"
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=new, Prefix="prod/")["Contents"]]
    assert not [k for k in keys if k.endswith(".lease")]
    assert (
        s3.get_object(Bucket=old, Key="browser-github/profile.tar.gz")["Body"].read() == b"legacy plaintext"
    )
    capsys.readouterr()
    assert move_state.main(args) == 0 and "same: profile.tar.gz " in capsys.readouterr().out
    # a different archive is there: kept, then replaced by --overwrite (a conditional write on its e_tag)
    s3.put_object(Bucket=new, Key="prod/browser/github/profile.tar.gz", Body=b"other")
    assert move_state.main(args) == 1
    assert get("prod/browser/github/profile.tar.gz") == b"other"
    assert move_state.main([*args, "--overwrite"]) == 0
    assert get("prod/browser/github/profile.tar.gz") == sealed


def test_browser_archive_moves_from_a_directory_to_moto_s3(tmp_path, moto_endpoint, monkeypatch):
    _moto_env(monkeypatch)
    s3, _, new = _browser_moto(moto_endpoint)
    _archive(tmp_path / "old", "profile.tar.gz", b"one")
    code = move_state.main(
        [
            "--browser",
            "me",
            "--from-profile-uri",
            f"file://{tmp_path}/old/profile.tar.gz",
            "--to",
            f"s3://{new}/prod",
            "--endpoint",
            moto_endpoint,
            "--region",
            "us-east-1",
        ]
    )
    assert code == 0
    assert s3.get_object(Bucket=new, Key="prod/browser/me/profile.tar.gz")["Body"].read() == b"one"


# -- a browser identity's archive -------------------------------------------------------------------


def _archive(root: Path, name: str, body: bytes) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(body)


def test_browser_archive_moves_the_newest_generation_and_keeps_the_source(tmp_path, capsys):
    old, new = tmp_path / "flyte" / "browser-github", tmp_path / "factory"
    sealed = move_state.browser_archive.MAGIC + b"sealed bytes"
    _archive(old, "profile.tar.gz", b"legacy plaintext")
    _archive(old, "profile.tar.gz.pending-1", sealed)
    _archive(
        old,
        "profile.tar.gz.lease",
        json.dumps(
            {"archives": [str(old / "profile.tar.gz.pending-1"), str(old / "profile.tar.gz")]}
        ).encode(),
    )
    code = move_state.main(
        ["--browser", "github", "--from-profile-uri", f"file://{old}/profile.tar.gz", "--to", f"file://{new}"]
    )
    assert code == 0
    target = new / "browser" / "github"
    assert (target / "profile.tar.gz").read_bytes() == sealed
    assert (target / "profile.tar.gz.prev").read_bytes() == b"legacy plaintext"
    assert not list(target.glob("*.lease"))
    assert (old / "profile.tar.gz").read_bytes() == b"legacy plaintext"  # the source is never touched
    out = capsys.readouterr().out
    assert "seals it on its next snapshot" in out  # said for the plaintext one
    assert "delete" in out and "pending-*" in out
    assert (
        move_state.main(
            [
                "--browser",
                "github",
                "--from-profile-uri",
                f"file://{old}/profile.tar.gz",
                "--to",
                f"file://{new}",
            ]
        )
        == 0
    )
    assert "same: profile.tar.gz " in capsys.readouterr().out


def test_browser_archive_dry_run_and_conflict(tmp_path, capsys):
    old, new = tmp_path / "old", tmp_path / "new"
    _archive(old, "profile.tar.gz", b"one")
    args = [
        "--browser",
        "github",
        "--from-profile-uri",
        f"file://{old}/profile.tar.gz",
        "--to",
        f"file://{new}",
    ]
    assert move_state.main([*args, "--dry-run"]) == 0
    assert not new.exists()
    _archive(new / "browser" / "github", "profile.tar.gz", b"other")
    assert move_state.main(args) == 1  # a different archive is already there: kept
    assert (new / "browser" / "github" / "profile.tar.gz").read_bytes() == b"other"
    assert move_state.main([*args, "--overwrite"]) == 0
    assert (new / "browser" / "github" / "profile.tar.gz").read_bytes() == b"one"


def test_a_key_the_factory_deleted_does_not_come_back_on_the_way_back(tmp_path, git_args, capsys):
    git = _git_store(tmp_path, git_args, "back")
    seeded = {"queue/work/0001.json": b'{"gen": 1}', "reserve.json": b'{"claude": 5.0}'}
    git.write(seeded, "seed")
    volume_uri = f"file://{tmp_path / 'vol'}"
    assert move_state.main(["--from", "git", "--to", volume_uri, *git_args]) == 0
    # What the factory does on the volume: consume a request and queue the next.
    volume = move_state.open_backend(volume_uri)
    volume.write({"queue/work/0001.json": None, "queue/work/0002.json": b'{"gen": 2}'}, "factory")
    capsys.readouterr()

    assert move_state.main(["--from", volume_uri, "--to", "git", *git_args]) == 1
    captured = capsys.readouterr()
    assert "only at destination (kept; --mirror deletes it): queue/work/0001.json" in captured.out
    assert "switch over" not in captured.out
    assert "--mirror" in captured.err
    assert _all(git) == seeded  # nothing deleted, and nothing merged in, without --mirror
    held = "held (nothing is written while only the destination has keys): queue/work/0002.json"
    assert held in captured.out

    assert move_state.main(["--from", volume_uri, "--to", "git", "--mirror", *git_args]) == 0
    assert set(_all(git)) == set(_all(volume)) == {"queue/work/0002.json", "reserve.json"}
    assert _all(git) == _all(volume)
    assert "switch over" in capsys.readouterr().out


def test_profile_backend_is_the_engines_store(monkeypatch):
    class Profile:
        state = type("State", (), {"backend": "bucket"})()

    sentinel = object()
    monkeypatch.setattr(move_state, "_profile_or_none", lambda: Profile())
    monkeypatch.setattr(move_state._state_store, "current", lambda: sentinel)
    assert move_state.open_backend("profile") is sentinel
