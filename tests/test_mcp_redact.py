"""Redaction of MCP tool results: secrets go, diagnostic identifiers stay (criteria 10 and 11)."""

from __future__ import annotations

import base64
import json
import string
import time

import pytest

from flyte_agent_factory import _mcp_redact, _union_identity
from flyte_agent_factory._mcp_redact import MARKER, collect_secret_values_from_env, redact_result, redact_text

# Fixtures are built by concatenation so the repository holds no literal a secret scanner would flag.
_PEM = (
    "-----BEGIN "
    + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAfake\nZmFrZWtleWJvZHk=\n-----END "
    + "RSA PRIVATE KEY-----"
)
_UNION_KEY = base64.b64encode(
    ("dns:///example.invalid" + ":client-id:" + "s3cr3tvalue" + ":org1").encode()
).decode()

SECRETS = {
    "anthropic-api": "sk-" + "ant-api03-" + "AbCdEfGhIjKlMnOpQrSt",
    "anthropic-oat": "sk-" + "ant-oat01-" + "AbCdEfGhIjKlMnOpQrSt",
    "openai": "sk-" + "AbCdEfGhIjKlMnOpQrSt1234",
    "openai-proj": "sk-" + "proj-AbCdEfGhIjKlMnOpQrSt1234",
    "github-pat": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "github-oauth": "gh" + "o_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "github-app": "gh" + "s_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "github-user": "gh" + "u_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "github-fine-grained": "github" + "_pat_" + "11ABCDEFG0abcdefghij_klmnopqrstuvwxyz",
    "aws-akia": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "aws-asia": "AS" + "IA" + "ABCDEFGHIJKLMNOP",
    "slack": "xo" + "xb-" + "1234567890-abcdefghij",
    "google": "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",
    "jwt": "ey" + "JhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "c2lnbmF0dXJl",
    "union": _UNION_KEY,
    "tailscale": "ts" + "key-" + "auth-abcdefghijklmnop",
}
# (embedded form, the secret substring that must vanish)
ASSIGNMENTS = {
    "aws-secret-assignment": (
        "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "wJalrXUtnFEMI",
    ),
    "env-style": ("DB_PASSWORD=" + "hunter2hunter2", "hunter2hunter2"),
    "json-style": ('{"client_secret": "' + "plainvalue" + '"}', "plainvalue"),
    "bearer": ("Authorization: Bearer " + "abcdef1234567890abcdef", "abcdef1234567890abcdef"),
    "basic": ("Authorization: Basic " + "dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    "pem": (_PEM, "MIIEowIBAAKCAQEAfake"),
    "union-env-assignment": ("AGENT_FACTORY_UNION_KEY=" + _UNION_KEY, _UNION_KEY),
    "union-export": ("export PROJECT_KEY=" + _UNION_KEY + " tail", _UNION_KEY),
    "union-flag": ("--value=" + _UNION_KEY, _UNION_KEY),
    "union-query": ("https://x.invalid/?key=" + _UNION_KEY + "&z=1", _UNION_KEY),
    "union-json-escaped": (json.dumps({"msg": "minted key:\n" + _UNION_KEY})[1:-1], _UNION_KEY),
    "union-percent-equals": ("https://x.invalid/?q=key%3D" + _UNION_KEY, _UNION_KEY),
}


def _cases() -> list[tuple[str, str]]:
    return [*((v, v) for v in SECRETS.values()), *ASSIGNMENTS.values()]


@pytest.mark.parametrize("secret_text, needle", _cases(), ids=[*SECRETS, *ASSIGNMENTS])
def test_a_secret_is_removed_mid_line_and_in_a_nested_result(secret_text: str, needle: str) -> None:
    needle = needle if needle != secret_text else secret_text
    line = f"2026-11-19T10:00:00Z INFO starting worker {secret_text} done"
    out = redact_text(line)
    assert needle not in out
    assert MARKER in out
    assert "starting worker" in out

    nested = redact_result({"a": [{"b": line}], "n": 3})
    assert needle not in str(nested)
    assert MARKER in str(nested)
    assert nested["n"] == 3  # type: ignore[index]


@pytest.mark.parametrize(
    "text",
    [
        "run r-abc12345 finished",
        "commit " + "a" * 20 + "0123456789abcdef0123",  # 40 lowercase hex
        "digest sha256:" + "0123456789abcdef" * 4,
        "image registry.example/app@sha256:" + "fedcba9876543210" * 4 + " pulled",
        "request 123e4567-e89b-12d3-a456-426614174000 ok",
        "2026-11-19T10:00:00.123Z 5 tokens used",
        "a" * 64,
    ],
)
def test_diagnostic_identifiers_are_left_intact(text: str) -> None:
    assert redact_text(text) == text
    assert redact_result({"k": [text]}) == {"k": [text]}


def test_a_hex_sha_next_to_a_secret_survives() -> None:
    sha = "0123456789abcdef" * 2 + "01234567"
    out = redact_text(f"at {sha} key " + SECRETS["github-pat"])
    assert sha in out
    assert SECRETS["github-pat"] not in out


def test_a_secret_named_dict_key_masks_a_plain_value() -> None:
    out = redact_result({"env": {"API_KEY": "plain", "REGION": "eu-west-1", "n": 1}})
    assert out == {"env": {"API_KEY": MARKER, "REGION": "eu-west-1", "n": 1}}


def test_redact_result_keeps_other_types_and_container_kinds() -> None:
    assert redact_result((1, None, 2.5, True)) == (1, None, 2.5, True)
    assert redact_result(["x", ("y",)]) == ["x", ("y",)]


def test_the_pods_own_secret_values_are_masked_by_value(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "opaque-" + "pod-value-123"
    monkeypatch.setattr(_mcp_redact, "_ENV_SECRETS", collect_secret_values_from_env({"MY_SECRET": value}))
    assert value not in redact_text(f"leaked {value} here")


def test_secret_values_from_env_picks_by_name_and_length() -> None:
    env = {"A_TOKEN": "12345678", "B_KEY": "short", "PATH": "/usr/bin:/bin", "DB_PASSWORD": "x" * 20}
    assert set(collect_secret_values_from_env(env)) == {"12345678", "x" * 20}


def test_redaction_is_idempotent() -> None:
    once = redact_text("k=" + SECRETS["openai"] + " DB_PASSWORD=abc12345")
    assert redact_text(once) == once


# --- Linear time (defect A): each pattern's worst case, built to defeat a retry at every position. ---

#: CPU seconds, not wall time: a loaded CI node must not fail a linear redaction. A super-linear pattern takes
#: minutes on these inputs ('a-' * 50000 did not finish), so the margin hides nothing.
_BUDGET_S = 3.0


def _worst_cases() -> dict[str, str]:
    return {
        "dashes": "a-" * 50000,
        "underscores": "x_" * 30000,
        "name-then-dashes": "token" + "-a" * 50000,
        "dashes-then-separator": "a-" * 50000 + "=",
        "unterminated-quotes": 'TOKEN="x ' * 10000 + "TOKEN='x " * 10000,
        "long-token-run": "A" * 200000,
        "base64-run": "A" * 100000 + "===",
        "padded-base64-run": ("A" * 50 + "=") * 5000,
        "spaces-after-name": "password" + " " * 100000 + "x",
        "authorization-spaces": ("authorization: basic" + " " * 100) * 1000,
        "bearer-spaces": ("bearer" + " " * 100) * 1000,
        "url-scheme": "a-" * 50000 + "://",
        "url-userinfo": "a://" + "u" * 100000,
        "url-userinfo-colons": "a://u:" * 20000,
        "slack-webhook": "https://hooks.slack.com/services/" + "A/" * 50000,
        "sk-run": "sk-" * 30000,
        "xapp-run": "xapp-" * 20000,
        "aws-ids": ("AKIA" + "ABCDEFGHIJKLMNOP ") * 5000,
        "aws-secret-runs": ("A" * 40 + " ") * 5000,
        "pem-header": "-----BEGIN PRIVATE KEY-----" * 5000,
        "hex-run": "0123456789abcdef" * 10000,
        "dots": "a." * 50000,
        "newlines": "\n".join(["a-" * 250] * 1000),
        "escaped-runs": "\\n".join(["A" * 39] * 3000),
        "percent-runs": "%3D".join(["A" * 39] * 3000),
        "backslash-runs": "\\".join(["A" * 39] * 3000),
        "ansi-runs": "\x1b[1m" * 50000,
        "ansi-escaped-runs": "\\u001b[1m" * 30000,
        "junk-then-short-base64-runs": ("1m" + "A" * 48 + " ") * 10000,
        "separator-runs": "/" * 200000,
        "path-runs": ("/v1/apps/" * 6 + "A" * 48 + " ") * 4000,
        "unicode-escape-runs": "\\u00e9" * 50000,
        "unicode-escape-then-base64-runs": ("\\u201c" + "A" * 48 + " ") * 5000,
        "surrogate-runs": "\\ud83d\\ude00" * 30000,
        "octal-runs": "\\033" * 60000,
        "secret-name-then-backslash-runs": ("token: " + "\\" * 60 + '"x ') * 4000,
    }


@pytest.mark.parametrize("name", list(_worst_cases()))
def test_redact_text_is_linear_on_each_patterns_worst_case(name: str) -> None:
    text = _worst_cases()[name]
    started = time.process_time()
    redact_text(text)
    assert time.process_time() - started < _BUDGET_S


def test_redact_result_is_fast_on_many_long_lines() -> None:
    lines = {"lines": ["a-" * 250 for _ in range(1000)]}
    started = time.process_time()
    redact_result(lines)
    assert time.process_time() - started < _BUDGET_S


def test_a_secret_assignment_is_still_found_inside_a_long_token_run() -> None:
    out = redact_text("a-" * 1000 + "x_API_KEY=" + "hunter2hunter2" + " tail")
    assert "hunter2hunter2" not in out
    assert out.endswith(" tail")


# --- Context beats protection (defect B). ---

_HEX64 = "0123456789abcdef" * 4
_HEX40 = "0123456789abcdef" * 2 + "01234567"
_UUID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.parametrize("value", [_HEX64, _HEX40, _UUID], ids=["hex64", "hex40", "uuid"])
@pytest.mark.parametrize(
    "template",
    [
        "Authorization: Bearer {}",
        "Authorization: Basic {}",
        "request sent with bearer {} ok",
        "api_token={} ok",
        "DB_PASSWORD: {} ok",
        '{{"client_secret": "{}"}}',
    ],
)
def test_a_hex_or_uuid_secret_in_a_secret_context_is_redacted(template: str, value: str) -> None:
    out = redact_text(template.format(value))
    assert value not in out
    assert MARKER in out


def test_a_bearer_hex_token_is_redacted_in_a_nested_result() -> None:
    out = redact_result({"headers": {"Authorization": "Bearer " + _HEX64}, "list": ["Bearer " + _HEX64]})
    assert _HEX64 not in str(out)


@pytest.mark.parametrize("key", ["Authorization", "authorization", "AUTHORIZATION"])
def test_a_dict_value_under_authorization_is_masked_whatever_its_shape(key: str) -> None:
    assert redact_result({key: _UUID, "n": 1}) == {key: MARKER, "n": 1}


@pytest.mark.parametrize("value", [_HEX64, _UUID], ids=["hex64", "uuid"])
def test_an_env_secret_value_that_is_hex_is_redacted(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(_mcp_redact, "_ENV_SECRETS", collect_secret_values_from_env({"MY_SECRET": value}))
    out = redact_text(f"echoed {value} here")
    assert value not in out
    assert out == f"echoed {MARKER} here"


def test_a_bare_git_sha_in_a_log_line_is_kept() -> None:
    line = f"checked out {_HEX40} and built sha256:{_HEX64} for {_UUID}"
    assert redact_text(line) == line


# --- The Union API key decoder (defect C). ---


def _union_key(decoded: str) -> str:
    return base64.b64encode(decoded.encode()).decode()


_SECRET_64 = "Zk3" + "Qx9Lm2Vb7Nc4Xa1Sd8Fg5Hj0Kp6Rt" + "Wy2Ue4Io7Pq1As3Df5Gh8Jk0Lz9Xc6Vb2Nm"[:31]
_REAL_SHAPED_UNION_KEYS = {
    "host": _union_key("example.invalid:client-id-0123456789:" + _SECRET_64 + ":None"),
    "port": _union_key("example.invalid:443:client-id-0123456789:" + _SECRET_64 + ":None"),
    "dns": _union_key("dns:///example.invalid:client-id-0123456789:" + _SECRET_64 + ":None"),
}


@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_a_realistic_union_api_key_is_redacted(key: str) -> None:
    token = _REAL_SHAPED_UNION_KEYS[key]
    out = redact_text(f"2026-11-19 INFO FLYTE_API_KEY loaded {token} done")
    assert token not in out
    assert out == f"2026-11-19 INFO FLYTE_API_KEY loaded {MARKER} done"
    assert redact_result({"a": [token]}) == {"a": [MARKER]}


@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_a_realistic_union_api_key_after_an_equals_sign_is_redacted(key: str) -> None:
    token = _REAL_SHAPED_UNION_KEYS[key]
    exported = redact_text(f"export AGENT_FACTORY_UNION_KEY={token}")
    assert exported == f"export AGENT_FACTORY_UNION_KEY={MARKER}"
    assert redact_text(f"https://x.invalid/?k={token}&z=1") == f"https://x.invalid/?k={MARKER}&z=1"


#: A secret after an escape: a JSON-encoded log line (`\\n`, `\\t`), a URL-encoded `=` or `"`, a Windows path.
_ESCAPES = {
    "json-newline": lambda s: json.dumps({"msg": "minted key:\n" + s}),
    "json-tab": lambda s: json.dumps({"m": "k:\t" + s}),
    "percent-equals": lambda s: "?key%3D" + s,
    "percent-quote": lambda s: "x%22" + s,
    "backslash": lambda s: "C:\\" + s,
    # After a terminal colour code: raw, JSON-encoded (`\\u001b[1m`), as a Python repr, and the console line
    # structlog writes.
    "ansi-bold": lambda s: "\x1b[1m" + s + "\x1b[0m",
    "ansi-256-colour": lambda s: "\x1b[38;5;123m" + s + "\x1b[0m",
    "ansi-erase-line": lambda s: "\x1b[2K" + s,
    "ansi-json-escaped": lambda s: json.dumps({"msg": "\x1b[1m" + s + "\x1b[0m"}),
    "ansi-repr": lambda s: "\\x1b[1m" + s + "\\x1b[0m",
    "structlog-console": lambda s: (
        "\x1b[2m2026-11-21 01:12:36\x1b[0m [\x1b[32m\x1b[1minfo     \x1b[0m] \x1b[1m" + s + "\x1b[0m"
    ),
    # After an escape that writes a code point by number: JSON's ensure_ascii, Go's encoding/json, JSON inside
    # JSON, and Python's repr and a shell's octal.
    "json-unicode-quote": lambda s: json.dumps({"msg": "minted key \u201c" + s + "\u201d"}),  # \u201c
    "json-nbsp": lambda s: json.dumps({"msg": "key:\u00a0" + s}),
    "json-surrogate-pair": lambda s: json.dumps({"msg": "\U0001f511" + s}),  # \ud83d\udd11
    "go-json-angle": lambda s: "\\u003c" + s + "\\u003e",
    "json-in-json": lambda s: json.dumps({"outer": json.dumps({"msg": "\u201c" + s})}),  # \\u201c
    "python-repr-non-bmp": lambda s: "\\U0001f511" + s,
    "python-repr-hex": lambda s: "\\x1b" + s,
    "octal-escape": lambda s: "\\033" + s,
}


@pytest.mark.parametrize("escape", list(_ESCAPES))
@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_a_realistic_union_api_key_after_an_escape_is_redacted(key: str, escape: str) -> None:
    token = _REAL_SHAPED_UNION_KEYS[key]
    out = redact_text(_ESCAPES[escape](token))
    assert token not in out
    assert MARKER in out


@pytest.mark.parametrize("escape", list(_ESCAPES))
@pytest.mark.parametrize("secret_text, needle", _cases(), ids=[*SECRETS, *ASSIGNMENTS])
def test_every_bare_shape_is_still_found_after_an_escape(secret_text: str, needle: str, escape: str) -> None:
    out = redact_text(_ESCAPES[escape](secret_text))
    assert needle not in out
    assert MARKER in out


@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_a_realistic_union_api_key_is_found_whatever_character_is_next_to_it(key: str) -> None:
    """Every printable character in front of the key, and every one behind it that cannot extend a token.

    A character from the key's own alphabet (or `=`, its padding) right after it makes one longer token, which
    is outside the shape, as it is for `_bridge`'s fixed-length rules; so those are left out behind it.
    """
    token = _REAL_SHAPED_UNION_KEYS[key]
    leaks_after = [c for c in string.printable if token in redact_text(c + token + " ")]
    tail = [c for c in string.printable if c not in string.ascii_letters + string.digits + "+/_-="]
    leaks_before = [c for c in tail if token in redact_text(" " + token + c)]
    assert leaks_after == []
    assert leaks_before == []


# --- A factory variable that names a secret holds none (C8). ---

UNION_KEY = _REAL_SHAPED_UNION_KEYS["dns"]
POD_ENV = {  # the MCP pod as deployed (`env_names` in evidence/union-chat-check.json), plus the mounted key
    _union_identity.SECRET_ENV: "queue" + "-key",
    _union_identity.TIER_ENV: "project",
    _union_identity.KEY_ENV: UNION_KEY,
}


def test_a_factory_variable_that_names_a_secret_is_not_collected() -> None:
    env = {
        **POD_ENV,
        "AGENT_FACTORY_CLAUDE_TOKEN_SECRET": "claude-token",
        "AGENT_FACTORY_SECRETS": "a-secret,b-secret",
        "AGENT_FACTORY_DEPLOY_KEY_ENV": _union_identity.KEY_ENV,
        "MY_SECRET": "hunter" + "22222",
    }
    assert set(collect_secret_values_from_env(env)) == {UNION_KEY, "hunter" + "22222"}


@pytest.mark.parametrize(
    "line",
    [
        f"{_union_identity.SECRET_ENV}=factory-project-key",
        "secret factory-project-key is not mounted as AGENT_FACTORY_UNION_KEY",
    ],
)
def test_a_secret_name_assigned_to_a_factory_variable_is_kept(line: str) -> None:
    assert redact_text(line) == line


def test_the_mounted_key_beside_the_name_is_still_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_mcp_redact, "_ENV_SECRETS", collect_secret_values_from_env(POD_ENV))
    out = redact_text(f"{_union_identity.KEY_ENV}={UNION_KEY} {_union_identity.SECRET_ENV}=queue-key")
    assert out == f"{_union_identity.KEY_ENV}={MARKER} {_union_identity.SECRET_ENV}=queue-key"


def test_the_collector_takes_what_the_deploy_mounts_and_skips_only_names_of_what_it_mounts() -> None:
    """Held to the deploy's real environments, so the next plain `AGENT_FACTORY_X_SECRET` is checked here.

    Every mounted secret's variable is collected. A plain variable with a secret word in its name is skipped
    only when every comma-separated part of its value is the key or the variable of a secret the deploy
    mounts: it names a secret, it holds none. Anything else set in plain text under such a name is collected.
    """
    from tests.test_telemetry import _deployed_environments  # the list the SDK-switch test already walks

    envs = _deployed_environments()
    assert "factory-mcp" in envs
    mounts = [s for env in envs.values() for s in (getattr(env, "secrets", None) or [])]
    names = {s.key for s in mounts} | {s.as_env_var for s in mounts if s.as_env_var}
    mounted_missed = {
        s.as_env_var
        for s in mounts
        if s.as_env_var and not collect_secret_values_from_env({s.as_env_var: "v" * 8})
    }
    skipped = {
        (name, var, value)
        for name, env in envs.items()
        for var, value in (env.env_vars or {}).items()
        if _mcp_redact._ENV_NAME.search(var)
        and not collect_secret_values_from_env({var: value.ljust(8, "x")})
    }
    assert mounted_missed == set()
    assert {var for _, var, _ in skipped} >= {_union_identity.SECRET_ENV}  # the walk saw the MCP pod's one
    assert [(n, v) for n, v, value in skipped if not set(value.split(",")) <= names] == []


@pytest.mark.parametrize("junk", ["/v1/", "123m", "n", "3D"])
@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_a_realistic_union_api_key_behind_junk_in_its_own_token_is_found(key: str, junk: str) -> None:
    token = _REAL_SHAPED_UNION_KEYS[key]
    assert redact_text("x " + junk + token + " y") == "x " + junk + MARKER + " y"


@pytest.mark.parametrize("junk", ["/v1/apps/", "GET /v1/apps/factory-mcp/keys/", "session-", "/" * 40])
@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_a_realistic_union_api_key_behind_a_long_prefix_in_its_own_token_is_found(
    key: str, junk: str
) -> None:
    token = _REAL_SHAPED_UNION_KEYS[key]
    assert redact_text("x " + junk + token + " y") == "x " + junk + MARKER + " y"


def test_a_coloured_line_without_a_secret_comes_back_unchanged() -> None:
    line = (
        "\x1b[2m2026-11-21 01:12:36\x1b[0m [\x1b[32m\x1b[1minfo     \x1b[0m] \x1b[1mready\x1b[0m \\u001b[1mok"
    )
    assert redact_text(line) == line


@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_a_key_in_a_colour_code_keeps_the_codes_around_the_marker(key: str) -> None:
    token = _REAL_SHAPED_UNION_KEYS[key]
    assert redact_text("\x1b[1m" + token + "\x1b[0m") == "\x1b[1m" + MARKER + "\x1b[0m"


@pytest.mark.parametrize("key", list(_REAL_SHAPED_UNION_KEYS))
def test_the_union_decoder_is_what_catches_a_realistic_key(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _REAL_SHAPED_UNION_KEYS[key]
    assert len(token) > 100  # past the bridge's generic 60-70 character base64 rule
    monkeypatch.setattr(_mcp_redact, "_is_union_api_key", lambda _token: False)
    assert token in redact_text(f"loaded {token} done")


# --- More shapes (defect D). ---

_AWS_ID = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
_AWS_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
_NEW_SHAPES = {
    "url-password": ("postgres://app_user:" + "hunter2pass" + "@db.example.invalid:5432/prod", "hunter2pass"),
    "url-password-https": ("https://bot:" + "s3cr3tPw0rd" + "@git.example.invalid/x.git", "s3cr3tPw0rd"),
    "slack-app": (
        "xa" + "pp-1-A0123456789-0123456789012-" + "abcdef0123456789abcdef",
        "abcdef0123456789abcdef",
    ),
    "slack-webhook": (
        "https://hooks.slack" + ".com/services/T01234567/B01234567/" + "AbCdEfGhIjKlMnOpQrStUvWx",
        "AbCdEfGhIjKlMnOpQrStUvWx",
    ),
    "aws-secret-after-id": (f"{_AWS_ID} {_AWS_SECRET}", _AWS_SECRET),
    "aws-secret-after-id-quoted": (f'"{_AWS_ID}", "{_AWS_SECRET}"', _AWS_SECRET),
    "aws-secret-before-id": (f"{_AWS_SECRET}, {_AWS_ID}", _AWS_SECRET),
    "aws-secret-labelled": ("aws_secret_access_key: " + _AWS_SECRET, _AWS_SECRET),
    "aws-secret-labelled-hex": ("aws_secret_access_key=" + _HEX40, _HEX40),
}


@pytest.mark.parametrize("escape", list(_ESCAPES))
@pytest.mark.parametrize("secret_text, needle", list(_NEW_SHAPES.values()), ids=list(_NEW_SHAPES))
def test_every_added_shape_is_still_found_after_an_escape(secret_text: str, needle: str, escape: str) -> None:
    out = redact_text(_ESCAPES[escape](secret_text))
    assert needle not in out
    assert MARKER in out


@pytest.mark.parametrize("secret_text, needle", list(_NEW_SHAPES.values()), ids=list(_NEW_SHAPES))
def test_the_added_shapes_are_removed_mid_line_and_in_a_nested_result(secret_text: str, needle: str) -> None:
    line = f"2026-11-19T10:00:00Z INFO connecting {secret_text} done"
    out = redact_text(line)
    assert needle not in out
    assert MARKER in out
    assert "connecting" in out

    nested = redact_result({"a": [{"b": line}], "n": 3})
    assert needle not in str(nested)
    assert MARKER in str(nested)


def test_a_url_password_redaction_keeps_the_user_and_host() -> None:
    out = redact_text("postgres://app_user:hunter2pass@db.example.invalid:5432/prod")
    assert out == f"postgres://app_user:{MARKER}@db.example.invalid:5432/prod"


def test_a_url_without_userinfo_and_a_git_sha_beside_an_aws_id_are_kept() -> None:
    text = f"https://example.invalid:8080/a:b@c and {_HEX40} {_AWS_ID[:0]}done"
    assert redact_text(text) == text
    assert _HEX40 in redact_text(f"{_AWS_ID} {_HEX40}")


# A secret-named key whose value is empty, null or a list: the devbox `factory.status` reply had
# `"credentials": [], "snapshot_age_s": ...` turned into `"credentials": [REDACTED] "snapshot_age_s"`, which
# is not JSON. A redacted JSON text must still parse, and a real value under the key must still go.
_JSON_WITH_SECRET_KEYS = {
    "empty list": ({"credentials": [], "x": 1}, None),
    "null": ({"token": None, "x": 1}, None),
    "empty string": ({"password": "", "x": 1}, None),
    "booleans": ({"secret_set": True, "api_key_valid": False, "x": 1}, None),
    "string": ({"password": "hunter2pass", "x": 1}, "hunter2pass"),
    "list of strings": ({"secrets": ["s3cr3t-one", "s3cr3t-two"], "x": 1}, "s3cr3t-one"),
    "number": ({"api_key": 98765432, "x": 1}, "98765432"),
    "nested": ({"a": {"credentials": [], "token": "tok-value-1", "b": [1, None]}, "x": 1}, "tok-value-1"),
}


@pytest.mark.parametrize(
    "obj, secret", list(_JSON_WITH_SECRET_KEYS.values()), ids=list(_JSON_WITH_SECRET_KEYS)
)
@pytest.mark.parametrize("indent", [None, 2])
def test_redacted_json_text_still_parses_and_loses_only_the_secret(
    obj: dict[str, object], secret: str | None, indent: int | None
) -> None:
    for text in (json.dumps(obj, indent=indent), json.dumps({"reply": json.dumps(obj, indent=indent)})):
        out = redact_text(text)
        json.loads(out)  # raises if a delimiter was swallowed or a quote dropped
        if secret is None:
            assert out == text
        else:
            assert secret not in out
            assert MARKER in out
            assert '"x": 1' in out or '\\"x\\": 1' in out


@pytest.mark.parametrize(
    "line, kept",
    [
        ("credentials: none, retrying", "credentials: none, retrying"),
        ("token=abc123; path=/", f"token={MARKER}; path=/"),
        ("TOKEN=abc123, next", f"TOKEN={MARKER}, next"),
        ("password=a,b,c done", f"password={MARKER} done"),
        ('password="abc123', f"password={MARKER}"),
    ],
)
def test_a_bare_secret_value_stops_at_its_delimiter(line: str, kept: str) -> None:
    assert redact_text(line) == kept


def test_a_structured_value_under_a_secret_key_keeps_its_shape_and_loses_its_strings() -> None:
    obj = {"credentials": [], "token": None, "secrets": ["one", {"k": "two", "n": 3}], "x": "1"}
    out = redact_result(obj)
    assert out == {"credentials": [], "token": None, "secrets": [MARKER, {"k": MARKER, "n": 3}], "x": "1"}


@pytest.mark.parametrize(
    "text",
    [
        "MissingSecrets: claude_token",
        "env: GITHUB_TOKEN",
        "max_tokens=4096 input_tokens: 1234 output_tokens=17",
        '{"token_count": 5, "secret_names": ["queue-key"], "missing_secrets": ["claude-token"]}',
        "api_key_set=true",
        "MAX_TOKEN=4096 num_tokens=12 total_tokens: 99",
        '{"required_api_keys": ["a"], "unset_secrets": ["c"]}',
    ],
)
def test_the_name_or_count_of_a_secret_is_not_a_secret(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "text, secret",
    [
        ("client_secret=Zx9_token_q", "Zx9_token_q"),
        ("password=Secret_Token", "Secret_Token"),
        ("password=mypassword", "mypassword"),
        ('{"api_key": "abc-1234"}', "abc-1234"),
        # A name that merely starts like a count or a list of names still holds a secret.
        ("MINIO_ROOT_PASSWORD=" + "Zx9qW2" + "eR7tY5uI3o", "Zx9qW2" + "eR7tY5uI3o"),
        ("MINIO_SECRET_KEY=" + "Zx9qW2" + "eR7tY5uI3o", "Zx9qW2" + "eR7tY5uI3o"),
        ("INPUT_PASSWORD=" + "Zx9qW2" + "eR7tY5uI3o", "Zx9qW2" + "eR7tY5uI3o"),
        ("NUM_API_KEY=" + "Zx9qW2" + "eR7tY5uI3o", "Zx9qW2" + "eR7tY5uI3o"),
        ("expected_token=" + "Zx9qW2" + "eR7tY5uI3o", "Zx9qW2" + "eR7tY5uI3o"),
        ('{"TOTAL_API_KEY": "' + "Zx9qW2" + 'eR7tY5uI3o"}', "Zx9qW2" + "eR7tY5uI3o"),
    ],
)
def test_a_value_that_merely_contains_a_secret_word_is_still_redacted(text: str, secret: str) -> None:
    assert secret not in redact_text(text)


def test_a_prefixed_secret_name_is_collected_from_the_environment() -> None:
    value = "Zx9qW2" + "eR7tY5uI3o"
    environ = {"MINIO_ROOT_PASSWORD": value, "MAX_TOKENS": "4096xxxx"}
    assert collect_secret_values_from_env(environ) == (value,)


def test_an_escaped_line_without_a_secret_comes_back_unchanged() -> None:
    line = json.dumps({"msg": "caf\u00e9 \u201cquoted\u201d \t ok \x1b[1mbold\x1b[0m"})
    assert redact_text(line) == line


def test_a_structured_list_of_secret_names_is_kept() -> None:
    obj = {"secret_names": ["queue-key"], "token_count": 5, "missing_secrets": ["a"], "password": "pw-1"}
    assert redact_result(obj) == {**obj, "password": MARKER}


# --- a private key split over list items ---

_KEY_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_KEY_END = "-----END " + "PRIVATE KEY-----"
_KEY_BODY = base64.b64encode(bytes(range(256)) * 5).decode()


def _key_lines(width: int) -> list[str]:
    body = [_KEY_BODY[i : i + width] for i in range(0, len(_KEY_BODY), width)]
    return [_KEY_BEGIN, *body, _KEY_END]


def _assert_no_key_body(out: object) -> None:
    text = json.dumps(out)
    assert MARKER in text
    assert not any(_KEY_BODY[i : i + 20] in text for i in range(0, len(_KEY_BODY) - 19))


@pytest.mark.parametrize("width", [64, 76])
@pytest.mark.parametrize("shape", ["messages", "strings"])
def test_a_private_key_split_one_line_per_entry_is_masked_whole(width, shape):
    lines = _key_lines(width)
    items = [{"message": line} for line in lines] if shape == "messages" else list(lines)
    _assert_no_key_body(redact_result({"result": {"lines": items}}))


def test_lines_after_the_end_marker_are_not_masked():
    items = [{"message": line, "replica": "r1"} for line in [*_key_lines(64), "server started"]]
    out = redact_result({"lines": items})
    assert isinstance(out, dict)
    assert out["lines"][-1] == {"message": "server started", "replica": "r1"}
    assert out["lines"][0]["replica"] == "r1"  # other keys survive masking


def test_a_list_without_a_key_is_unchanged():
    items = [{"message": "listening on 8080"}, "plain line", 3, None]
    assert redact_result({"lines": items}) == {"lines": items}


@pytest.mark.parametrize("digest", ["a1b2c3d4" * 5, "0f" * 32])
def test_a_bare_hex_digest_line_stays_readable(digest):
    assert redact_text(digest) == digest
    assert redact_result(["built", digest]) == ["built", digest]


def test_an_orphan_key_body_line_is_masked():
    assert redact_text(_KEY_BODY[:64]) == MARKER


def test_shortening_redacts_first_and_keeps_a_marker_whole() -> None:
    aws_id, aws_key = "AK" + "IA" + "IOSFODNN7EXAMPLQ", "wJalrXUtnFEMI" + "/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
    pair = "y" * 280 + " " + aws_id + " " + aws_key + " tail"
    out = _mcp_redact.shorten_after_redaction(pair, 300)
    # The second marker starts before 300 and ends past it: it is kept whole, never cut in half.
    assert out == "y" * 280 + " " + MARKER + " " + MARKER

    token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"
    straddling = "z" * 290 + token + " end"
    out = _mcp_redact.shorten_after_redaction(straddling, 300)
    assert out == "z" * 290 + MARKER
    assert _mcp_redact.shorten_after_redaction("short " + token, 300) == "short " + MARKER
    assert _mcp_redact.shorten_after_redaction("a" * 500, 300) == "a" * 300
