"""The personal browser's profile archive: sealed, trimmed to session state, kept on a `file://` volume."""

import base64
import importlib.util
import io
import tarfile
from pathlib import Path

import flyte
import pytest

from flyte_agent_factory import _profile, browser

ASSETS = Path(__file__).resolve().parents[1] / "src/flyte_agent_factory/_assets/browser"
spec = importlib.util.spec_from_file_location("personal_archive_runtime", ASSETS / "personal.py")
assert spec is not None and spec.loader is not None
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
archive = runtime.browser_archive

KEK = bytes(range(32))
OTHER_KEK = bytes(range(1, 33))
COOKIE = b"sessionid=PLANTED-COOKIE-VALUE-0123456789"


def make_profile(root: Path) -> Path:
    """A profile with the real Chromium layout: session state, caches, history and saved passwords."""
    files = {
        "Local State": b"local state",
        "Default/Preferences": b"{}",
        "Default/Network/Cookies": COOKIE,
        "Default/Network/Cookies-journal": b"journal",
        "Default/Cookies": COOKIE,
        "Default/Local Storage/leveldb/000003.log": b"token=abc",
        "Default/Session Storage/000003.log": b"tab state",
        "Default/IndexedDB/https_example.com_0.indexeddb.leveldb/000003.log": b"idb",
        "Default/History": b"history",
        "Default/Login Data": b"saved passwords",
        "Default/Cache/Cache_Data/f_000001": b"cache",
        "Default/Code Cache/js/index": b"code cache",
        "Default/GPUCache/data_0": b"gpu",
        "Default/Service Worker/CacheStorage/abc/0": b"sw cache",
        "ShaderCache/data": b"shader",
        "SingletonCookie": b"x",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / "SingletonLock").symlink_to("somehost-1")
    return root


def names_in(plain_tar_gz: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(plain_tar_gz), mode="r:gz") as tar:
        return set(tar.getnames())


def stored(uri: str) -> bytes:
    fs, key = archive.open_fs(uri)
    with fs.open(key, "rb") as handle:
        return bytes(handle.read())


def test_round_trip_and_the_stored_object_hides_the_profile(tmp_path):
    uri = (tmp_path / "bucket/browser/github/profile.tar.gz").as_uri()
    runtime.snapshot(make_profile(tmp_path / "profile"), uri, kek=KEK)
    blob = stored(uri)
    assert blob.startswith(archive.MAGIC)
    assert not blob.startswith(archive.GZIP_MAGIC)
    assert b"PLANTED-COOKIE-VALUE" not in blob
    assert b"Cookies" not in blob  # no file name either
    # The planted value is in the archive once decrypted, so the check above is not vacuous.
    with tarfile.open(fileobj=io.BytesIO(archive.unseal(blob, KEK)), mode="r:gz") as tar:
        member = tar.extractfile("Default/Network/Cookies")
        assert member is not None and member.read() == COOKIE
    restored = tmp_path / "restored"
    runtime.restore(restored, uri, kek=KEK)
    assert (restored / "Default/Network/Cookies").read_bytes() == COOKIE


def test_every_archive_gets_its_own_data_key_and_nonce():
    first, second = archive.seal(b"same", KEK), archive.seal(b"same", KEK)
    assert first != second
    assert first[: archive.HEADER_LEN] != second[: archive.HEADER_LEN]
    # The key id names the key without being it.
    assert KEK not in first and archive.key_id(KEK) in first


def test_the_header_is_authenticated():
    blob = bytearray(archive.seal(b"payload", KEK))
    blob[len(archive.MAGIC) + 1 + archive.KEY_ID_LEN + 2] ^= 1  # a byte of the wrapped data key
    with pytest.raises(archive.ArchiveCorrupt):
        archive.unseal(bytes(blob), KEK)


def test_only_session_state_is_archived(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    runtime.snapshot(make_profile(tmp_path / "profile"), uri, kek=KEK)
    names = names_in(archive.unseal(stored(uri), KEK))
    assert names == {
        "Local State",
        "Default/Preferences",
        "Default/Network/Cookies",
        "Default/Network/Cookies-journal",
        "Default/Cookies",
        "Default/Local Storage/leveldb/000003.log",
        "Default/Session Storage/000003.log",
    }
    for dropped in ("Cache", "Code Cache", "GPUCache", "History", "CacheStorage", "Login Data", "Singleton"):
        assert not [n for n in names if dropped in n], dropped


def test_wrong_or_missing_key_is_an_error_and_nothing_is_overwritten(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    runtime.snapshot(make_profile(tmp_path / "profile"), uri, kek=KEK)
    before = stored(uri)
    local = tmp_path / "restored"
    local.mkdir()
    (local / "keep").write_text("local")
    for wrong in (OTHER_KEK, None):
        with pytest.raises(archive.ArchiveKeyError):
            runtime.restore(local, uri, kek=wrong)
    assert (local / "keep").read_text() == "local"  # not replaced by an empty profile
    assert stored(uri) == before


def test_a_wrong_key_does_not_fall_back_to_the_previous_generation(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    profile = make_profile(tmp_path / "profile")
    runtime.snapshot(profile, uri, kek=OTHER_KEK)
    runtime.snapshot(profile, uri, kek=KEK)  # the current generation; `.prev` is under the other key
    with pytest.raises(archive.ArchiveKeyError):
        runtime.restore(tmp_path / "restored", uri, kek=OTHER_KEK)


def test_a_damaged_archive_under_the_right_key_is_corrupt_not_a_key_error(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    runtime.snapshot(make_profile(tmp_path / "profile"), uri, kek=KEK)
    runtime.snapshot(tmp_path / "profile", uri, kek=KEK)
    path = tmp_path / "profile.tar.gz"
    damaged = bytearray(path.read_bytes())
    damaged[-5] ^= 1
    path.write_bytes(bytes(damaged))
    restored = tmp_path / "restored"
    runtime.restore(restored, uri, kek=KEK)  # falls back to `.prev`
    assert (restored / "Default/Cookies").read_bytes() == COOKIE


def test_no_key_means_no_snapshot(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    with pytest.raises(archive.ArchiveKeyError):
        runtime.snapshot(make_profile(tmp_path / "profile"), uri)
    assert not (tmp_path / "profile.tar.gz").exists()
    assert not list(tmp_path.glob("profile.tar.gz.*"))


def test_main_refuses_to_start_without_a_usable_key(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("BROWSER_PROFILE_URI", (tmp_path / "p.tar.gz").as_uri())
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "token")
    monkeypatch.delenv("BROWSER_SECRETS_DIR", raising=False)
    monkeypatch.delenv("BROWSER_ARCHIVE_KEY", raising=False)
    with pytest.raises(archive.ArchiveKeyError):
        runtime.main()
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "token")
    monkeypatch.setenv("BROWSER_ARCHIVE_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(archive.ArchiveKeyError, match="32 bytes"):
        runtime.main()


def test_a_key_error_at_start_neither_starts_chromium_nor_snapshots(tmp_path, monkeypatch):
    uri = (tmp_path / "bucket/profile.tar.gz").as_uri()
    runtime.snapshot(make_profile(tmp_path / "source"), uri, kek=OTHER_KEK)
    before = stored(uri)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(profile))
    monkeypatch.setenv("BROWSER_PROFILE_URI", uri)
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "token")
    monkeypatch.setenv("BROWSER_ARCHIVE_KEY", base64.b64encode(KEK).decode())
    monkeypatch.delenv("BROWSER_SECRETS_DIR", raising=False)
    launched = []
    monkeypatch.setattr(runtime, "ThreadingHTTPServer", lambda *a, **k: launched.append(True))
    monkeypatch.setattr(runtime.signal, "signal", lambda *args: None)
    with pytest.raises(archive.ArchiveKeyError):
        runtime.main()
    assert not launched
    assert stored(uri) == before
    assert not list((tmp_path / "bucket").glob("*.pending-*"))


def test_a_plaintext_archive_is_restored_once_and_the_next_snapshot_is_sealed(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    old = make_profile(tmp_path / "old")
    with tarfile.open(
        tmp_path / "profile.tar.gz", "w:gz"
    ) as tar:  # the pre-encryption format: all but caches
        tar.add(old / "Default/Cookies", arcname="Default/Cookies")
        tar.add(old / "Default/History", arcname="Default/History")
    restored = tmp_path / "restored"
    runtime.restore(restored, uri, kek=KEK)
    assert (restored / "Default/Cookies").read_bytes() == COOKIE
    runtime.snapshot(restored, uri, kek=KEK)
    blob = stored(uri)
    assert archive.is_sealed(blob)
    assert names_in(archive.unseal(blob, KEK)) == {"Default/Cookies"}


def test_a_legacy_archive_is_replaced_by_a_sealed_one_under_the_lease(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    with tarfile.open(tmp_path / "profile.tar.gz", "w:gz") as tar:
        tar.add(make_profile(tmp_path / "old") / "Default/Cookies", arcname="Default/Cookies")
    lease = runtime.Lease(uri)
    assert lease.renew()
    runtime.restore(tmp_path / "restored", uri, lease, kek=KEK)
    runtime.snapshot(tmp_path / "restored", uri, lease, kek=KEK)
    assert archive.is_sealed(Path(lease.archives[0]).read_bytes())
    # No plaintext copy of the cookies stays at rest once a sealed successor exists.
    for _ in range(2):
        runtime.snapshot(tmp_path / "restored", uri, lease, kek=KEK)
    for stored in tmp_path.glob("profile.tar.gz*"):
        if ".lease" in stored.name:
            continue
        assert archive.is_sealed(stored.read_bytes()), stored.name


def test_a_legacy_archive_is_resealed_without_a_lease(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    with tarfile.open(tmp_path / "profile.tar.gz", "w:gz") as tar:
        tar.add(make_profile(tmp_path / "old") / "Default/Cookies", arcname="Default/Cookies")
    runtime.restore(tmp_path / "restored", uri, kek=KEK)
    runtime.snapshot(tmp_path / "restored", uri, kek=KEK)
    for name in ("profile.tar.gz", "profile.tar.gz.prev"):
        assert archive.is_sealed((tmp_path / name).read_bytes()), name
    restored = tmp_path / "again"
    runtime.restore(restored, uri, kek=KEK)
    assert (restored / "Default/Cookies").exists()


def test_file_lease_admits_one_owner(tmp_path):
    uri = (tmp_path / "volume/browser/github/profile.tar.gz").as_uri()
    first, second = runtime.Lease(uri), runtime.Lease(uri)
    assert first.renew(now=100)
    assert not second.renew(now=101)
    assert first.renew(now=102)
    assert not second.renew(now=103)
    first.release()
    assert second.renew(now=104)
    assert not first.renew(now=105)  # the old owner has lost it for good


def test_file_lease_expires(tmp_path):
    uri = (tmp_path / "profile.tar.gz").as_uri()
    first, second = runtime.Lease(uri), runtime.Lease(uri)
    assert first.renew(now=100)
    assert not second.renew(now=219)
    assert second.renew(now=221)
    assert not first.renew(now=222)


def test_file_location_keeps_two_generations_under_the_lease(tmp_path):
    uri = (tmp_path / "volume/browser/github/profile.tar.gz").as_uri()
    lease = runtime.Lease(uri)
    assert lease.renew()
    profile = make_profile(tmp_path / "profile")
    for _ in range(3):
        runtime.snapshot(profile, uri, lease, kek=KEK)
    assert len(lease.archives) == 2
    other = runtime.Lease(uri)
    lease.release()
    assert other.renew()
    restored = tmp_path / "restored"
    runtime.restore(restored, uri, other, kek=KEK)
    assert (restored / "Default/Cookies").read_bytes() == COOKIE


def test_the_s3_endpoint_reaches_fsspec_and_obstore(monkeypatch):
    monkeypatch.setenv("BROWSER_PROFILE_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("BROWSER_PROFILE_REGION", "eu-west-1")
    assert archive.storage_options("s3://bucket/browser/x/profile.tar.gz") == {
        "client_kwargs": {"endpoint_url": "http://localhost:9000", "region_name": "eu-west-1"}
    }
    assert archive.storage_options("gs://bucket/x") == {}
    seen: dict[str, object] = {}
    monkeypatch.setattr(archive, "from_url", lambda url, **kwargs: seen.update(url=url, **kwargs))
    archive.lease_store("s3://bucket/browser/x/profile.tar.gz")
    assert seen == {
        "url": "s3://bucket",
        "config": {"endpoint": "http://localhost:9000", "region": "eu-west-1"},
        "client_options": {"allow_http": True},
    }


def test_the_archive_key_is_mounted_only_in_the_personal_app():
    profile = _profile.parse(
        (Path(__file__).parent / "profiles/work.toml").read_text()
        + '\n[browser]\nexit_node = ""\n[browser.identities.github]\nprofile_uri = "memory://github"\n'
    )
    (identity,) = profile.browser.identities
    app = browser.personal_app(profile, identity, "")
    mounted = {s.key: s.as_env_var for s in secrets_of(app)}
    assert mounted["browser_archive_key"] == "BROWSER_ARCHIVE_KEY"
    from flyte_agent_factory import agent

    for other in (browser.browser_env, agent.env):
        assert "browser_archive_key" not in {s.key for s in secrets_of(other)}


def secrets_of(app: flyte.app.AppEnvironment | flyte.TaskEnvironment) -> list[flyte.Secret]:
    secrets = [app.secrets] if isinstance(app.secrets, (str, flyte.Secret)) else app.secrets or []
    return [s for s in secrets if isinstance(s, flyte.Secret)]


def test_the_archive_key_is_staged_out_of_the_environment(monkeypatch):
    shell = (ASSETS / "start.sh").read_text()
    assert (
        "-u BROWSER_ARCHIVE_KEY" in shell
        and "for name in BROWSER_PERSONAL_TOKEN BROWSER_ARCHIVE_KEY" in shell
    )
    monkeypatch.setenv("BROWSER_ARCHIVE_KEY", "secret-value")
    assert "BROWSER_ARCHIVE_KEY" not in runtime.child_env()


def test_lease_store_refuses_plain_http_off_localhost(monkeypatch):
    monkeypatch.setenv("BROWSER_PROFILE_ENDPOINT", "http://KEY:SECRET@minio.example.com:9000")
    monkeypatch.setattr(archive, "from_url", lambda *args, **kwargs: pytest.fail("a store was opened"))
    with pytest.raises(ValueError, match="localhost") as caught:
        archive.lease_store("s3://bucket/browser/x/profile.tar.gz")
    assert "SECRET" not in str(caught.value) and "minio.example.com" not in str(caught.value)


def test_a_failed_lease_write_leaves_no_temp_file(tmp_path, monkeypatch):
    store = archive.LocalLeaseStore(tmp_path)
    monkeypatch.setattr(archive.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        store.put("lease", b"{}", "create")
    assert [p.name for p in tmp_path.iterdir() if not p.name.endswith(".flock")] == []
