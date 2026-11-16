"""`[state]` backend selection and validation, the browser archive default, and the Flyte-bucket refusal."""

from __future__ import annotations

from typing import Any

import pytest

from flyte_agent_factory import _profile, _profile_state, _state_bucket, _state_store
from tests.conftest import TEST_PROFILE


def load(state: str = "", extra: str = "") -> _profile.Profile:
    return _profile.parse(TEST_PROFILE.read_text() + state + extra)


def test_the_backend_defaults_follow_the_fields_set():
    assert load().state.backend == "git"
    assert load('\n[state]\nbranch = "factory-state/x"\n').state.backend == "git"
    assert load('\n[state]\nbucket = "s3://bkt/factory"\n').state.backend == "bucket"
    assert load('\n[state]\nvolume = "/var/state"\nvolume_claim = "c"\n').state.backend == "volume"
    state = load('\n[state]\nbackend = "git"\n').state
    assert (state.retention_days, state.bucket, state.volume) == (30, "", "")


def test_every_field_is_read():
    state = load(
        '\n[state]\nbucket = "s3://bkt/p"\nendpoint = "http://localhost:9000"\nregion = "eu-west-1"\n'
        "retention_days = 7\n"
    ).state
    assert (state.bucket, state.endpoint, state.region, state.retention_days) == (
        "s3://bkt/p",
        "http://localhost:9000",
        "eu-west-1",
        7,
    )


@pytest.mark.parametrize(
    ("state", "field"),
    [
        ('backend = "dynamo"', "backend"),
        ('bucket = "https://bkt/p"', "bucket"),
        ('bucket = "az://bkt/p"', "bucket"),
        ('bucket = "s3:///p"', "bucket"),
        ('bucket = "s3://key:secret@bkt/p"', "bucket"),
        ('backend = "bucket"', "bucket"),
        ('backend = "volume"', "volume"),
        ('backend = "git"\nbucket = "s3://bkt/p"', "bucket"),
        ('backend = "volume"\nbucket = "s3://bkt/p"', "bucket"),
        ('backend = "bucket"\nvolume = "/v"', "volume"),
        ('bucket = "s3://bkt/p"\nvolume = "/v"', "volume"),
        ('volume = "relative/dir"', "volume"),
        ('volume = "/a/../b"', "volume"),
        ('volume = "/v"\nendpoint = "https://x"', "endpoint"),
        ('bucket = "gs://bkt/p"\nendpoint = "https://x"', "endpoint"),
        ('bucket = "s3://bkt/p"\nendpoint = "http://minio.example.com"', "endpoint"),
        ('bucket = "s3://bkt/p"\nendpoint = "ftp://x"', "endpoint"),
        ('bucket = "s3://bkt/p"\nendpoint = "https://k:s@x"', "endpoint"),
        ('bucket = "s3://bkt/p"\nretention_days = 0', "retention_days"),
        ('bucket = "s3://bkt/p"\nretention_days = "30"', "retention_days"),
        ('bucket = "s3://bkt/p"\nretention_days = true', "retention_days"),
        ('bucket = "s3://bkt/p"\nbranch = "x"', "branch"),
        ("bucket = 3", "bucket"),
        ('bucket = ""', "bucket"),
        ('bucket = "  "', "bucket"),
        ('bucket = "s3://bkt/p"\ncolour = "red"', "colour"),
    ],
)
def test_a_bad_state_table_is_a_profile_error_naming_the_field(state, field):
    with pytest.raises(_profile.ProfileError, match=rf"\[state\].*{field}|{field}.*\[state\]") as info:
        load(f"\n[state]\n{state}\n")
    assert "secret" not in str(info.value).replace("secret@", "")  # no credential is echoed back


def test_the_endpoint_may_be_plain_http_only_on_localhost():
    for host in ("localhost:9000", "127.0.0.1:9000"):
        load(f'\n[state]\nbucket = "s3://bkt/p"\nendpoint = "http://{host}"\n')
    load('\n[state]\nbucket = "s3://bkt/p"\nendpoint = "https://minio.example.com"\n')


def uri_of(profile: _profile.Profile, name: str) -> str:
    identity = profile.browser.identity(name)
    assert identity is not None
    return identity.profile_uri


BROWSER = '\n[browser.identities.work]\nexit_node = ""\n'
EXPLICIT = '\n[browser.identities.work]\nprofile_uri = "s3://mine/work.tar.gz"\n'


def test_a_browser_identity_defaults_its_archive_into_the_state_bucket_or_volume():
    bucket = load('\n[state]\nbucket = "s3://bkt/factory/"\n', BROWSER)
    assert uri_of(bucket, "work") == "s3://bkt/factory/browser/work/profile.tar.gz"
    volume = load('\n[state]\nvolume = "/var/state"\nvolume_claim = "c"\n', BROWSER)
    assert uri_of(volume, "work") == "file:///var/state/browser/work/profile.tar.gz"


def test_an_explicit_profile_uri_is_kept():
    profile = load('\n[state]\nbucket = "s3://bkt/factory"\n', EXPLICIT)
    assert uri_of(profile, "work") == "s3://mine/work.tar.gz"


def test_a_browser_identity_without_a_profile_uri_needs_a_bucket_or_volume():
    with pytest.raises(
        _profile.ProfileError, match=r"missing key 'profile_uri' in \[browser.identities.work\]"
    ):
        load("", BROWSER)


def test_the_legacy_personal_browser_defaults_too():
    profile = load('\n[state]\nbucket = "s3://bkt/f"\n', "\n[browser]\npersonal = true\n")
    assert uri_of(profile, "personal") == "s3://bkt/f/browser/personal/profile.tar.gz"


# --- never Flyte's bucket ----------------------------------------------------------------------------------

FLYTE = ("s3://flyte-data/raw",)


@pytest.mark.parametrize(
    "uri",
    [
        "s3://flyte-data/factory",
        "s3://flyte-data",
        "s3a://flyte-data/x",
        "s3n://flyte-data/x/y",
        "s3://Flyte-Data/raw/inside",
        "s3://u@flyte-data/factory",
        "s3://flyte-data:443/factory",
    ],
)
def test_a_uri_in_flytes_bucket_is_refused_whatever_the_prefix_or_scheme_alias(uri):
    with pytest.raises(_profile_state.StateConfigError, match="Flyte's own data bucket"):
        _profile_state.refuse_flyte_bucket(uri, FLYTE)


def test_gcs_aliases_and_other_buckets():
    with pytest.raises(_profile_state.StateConfigError):
        _profile_state.refuse_flyte_bucket("gcs://fl/x", ("gs://fl/raw",))
    _profile_state.refuse_flyte_bucket("s3://other/x", FLYTE)
    _profile_state.refuse_flyte_bucket("gs://flyte-data/x", FLYTE)  # same name, another cloud
    _profile_state.refuse_flyte_bucket("s3://anything", ())


def test_a_volume_path_overlapping_a_flyte_directory_is_refused():
    flyte = ("file:///var/flyte/raw",)
    for uri in ("file:///var/flyte/raw/state", "file:///var/flyte", "file:///var/flyte/raw"):
        with pytest.raises(_profile_state.StateConfigError):
            _profile_state.refuse_flyte_bucket(uri, flyte)
    _profile_state.refuse_flyte_bucket("file:///var/flyte-other/state", flyte)


def test_the_profile_refuses_a_state_bucket_in_flytes_bucket():
    flyte = 'flyte_bucket = "s3://flyte-data/raw"\n'
    with pytest.raises(_profile.ProfileError, match=r"\[state\] bucket is inside Flyte's own data bucket"):
        load(f'\n[state]\n{flyte}bucket = "s3://flyte-data/factory"\n')
    load(f'\n[state]\n{flyte}bucket = "s3://own-bucket/factory"\n')


def test_the_profile_refuses_a_browser_archive_in_flytes_bucket():
    state = '\n[state]\nflyte_bucket = ["s3://flyte-data/raw"]\nbucket = "s3://own-bucket/f"\n'
    with pytest.raises(
        _profile.ProfileError, match=r"\[browser\] profile_uri is inside Flyte's own data bucket"
    ):
        load(state, '\n[browser.identities.work]\nprofile_uri = "s3://flyte-data/browser/work.tar.gz"\n')


def test_the_runtime_check_refuses_flytes_raw_data_bucket(monkeypatch):
    monkeypatch.setattr(_profile_state, "runtime_flyte_prefixes", lambda: ("s3://flyte-data/raw/x",))
    state = _profile_state.State(backend="bucket", bucket="s3://flyte-data/factory")
    _state_store.configure(None)
    with pytest.raises(_profile_state.StateConfigError, match="Flyte's own data bucket"):
        _state_store._object_store(state)


# --- the config built for each scheme ----------------------------------------------------------------------


def test_s3_config_has_conditional_put_and_no_credential():
    assert _state_bucket.s3_config() == {"conditional_put": "etag"}
    config = _state_bucket.s3_config("http://localhost:9000", "eu-west-1")
    assert config == {
        "conditional_put": "etag",
        "region": "eu-west-1",
        "endpoint": "http://localhost:9000",
        "virtual_hosted_style_request": False,
        "client_options": {"allow_http": True},
    }
    https = _state_bucket.s3_config("https://minio.example.com")
    assert https["virtual_hosted_style_request"] is False
    assert "client_options" not in https
    assert not {"access_key_id", "secret_access_key", "token"} & set(config)
    with pytest.raises(ValueError, match="localhost"):
        _state_bucket.s3_config("http://minio.example.com")


def test_the_s3_store_is_built_with_prefix_and_config():
    adapter = _state_bucket.open_adapter("s3://bkt/a/b", endpoint="http://127.0.0.1:9", region="us-east-1")
    store: Any = adapter._store
    assert store.prefix == "a/b"
    assert store.config["bucket"] == "bkt"
    assert store.config["conditional_put"] == "etag"
    assert store.config["endpoint"] == "http://127.0.0.1:9"
    assert store.client_options["allow_http"] == "true"


def test_the_gcs_and_memory_stores():
    gcs: Any = _state_bucket.open_adapter("gs://bkt/pre/fix")._store
    assert (gcs.config["bucket"], gcs.prefix) == ("bkt", "pre/fix")
    assert _state_bucket.open_adapter("memory://") is not None
    with pytest.raises(ValueError):
        _state_bucket.open_adapter("ftp://bkt/p")


def test_current_selects_the_backend_the_profile_names(monkeypatch, tmp_path):
    _state_store.configure(None)
    volume = tmp_path / "vol"
    profile = load(f'\n[state]\nvolume = "{volume}"\nvolume_claim = "c"\nretention_days = 3\n')
    monkeypatch.setattr(_profile, "current", lambda: profile)
    monkeypatch.delenv("AGENT_FACTORY_STATE_REMOTE", raising=False)
    monkeypatch.delenv("AGENT_FACTORY_STATE_BRANCH", raising=False)
    store = _state_store.current()
    assert isinstance(store, _state_bucket.BucketStore)
    assert store.retention_days == 3
    assert _state_store.current() is store
    store.write({"k": b"v"}, "m")
    assert (volume / "state" / "k").read_bytes() == b"v"
    # The environment override is for git and wins over the profile.
    monkeypatch.setenv("AGENT_FACTORY_STATE_REMOTE", str(tmp_path / "remote.git"))
    assert isinstance(_state_store.current(), _state_store.StateStore)
    _state_store.configure(None)


def test_an_endpoint_with_a_query_fails_profile_load():
    for endpoint in ("https://minio.example.com?token=X", "https://minio.example.com#frag"):
        with pytest.raises(_profile.ProfileError) as caught:
            load(f'\n[state]\nbucket = "s3://bkt/p"\nendpoint = "{endpoint}"\n')
        assert "token" not in str(caught.value)


def test_plain_http_is_allowed_only_for_localhost():
    assert all(_profile_state.plain_http_is_allowed(h) for h in ("localhost", "127.0.0.1", "::1"))
    assert not any(_profile_state.plain_http_is_allowed(h) for h in ("minio.example.com", "", None))


def test_volume_needs_a_claim_and_other_backends_refuse_one():
    state = load('\n[state]\nvolume = "/state"\nvolume_claim = "factory-state"\n').state
    assert (state.backend, state.volume_claim) == ("volume", "factory-state")
    with pytest.raises(_profile.ProfileError, match="needs volume_claim"):
        load('\n[state]\nvolume = "/state"\n')
    with pytest.raises(_profile.ProfileError, match="volume_claim is set but backend is 'bucket'"):
        load('\n[state]\nbucket = "s3://bkt/p"\nvolume_claim = "factory-state"\n')
    with pytest.raises(_profile.ProfileError, match="volume_claim is set but backend is 'git'"):
        load('\n[state]\nvolume_claim = "factory-state"\n')


@pytest.mark.parametrize("key", ["factory_service_account", "browser_service_account"])
@pytest.mark.parametrize("backend", ["", 'bucket = "s3://bkt/p"\n'], ids=["git", "bucket"])
def test_service_account_names_are_validated_and_never_echoed(key, backend):
    assert getattr(load(f'\n[state]\n{backend}{key} = "flyte-factory"\n').state, key) == "flyte-factory"
    for bad in ("Flyte_Factory", "-lead", "trail-", "a b", "x" * 254, "key=SECRET-abc"):
        with pytest.raises(_profile.ProfileError, match=key) as refused:
            load(f'\n[state]\n{backend}{key} = "{bad}"\n')
        assert bad not in str(refused.value)
    with pytest.raises(_profile.ProfileError, match=key):
        load(f"\n[state]\n{backend}{key} = 3\n")


def test_a_bare_flyte_bucket_name_is_refused():
    # A bare name has no scheme, so the guard used to compare it with nothing and let the bucket through.
    with pytest.raises(_profile_state.StateConfigError, match="flyte_bucket"):
        _profile_state.parse({"flyte_bucket": "flyte-data"})
    with pytest.raises(_profile.ProfileError, match="flyte_bucket"):
        load('\n[state]\nflyte_bucket = "flyte-data"\n')
    with pytest.raises(_profile_state.StateConfigError):
        _profile_state.refuse_flyte_bucket("s3://flyte-data/f", ("flyte-data",))
    with pytest.raises(_profile_state.StateConfigError):
        _profile_state.refuse_flyte_bucket("s3://flyte-data/f", ("s3://",))
