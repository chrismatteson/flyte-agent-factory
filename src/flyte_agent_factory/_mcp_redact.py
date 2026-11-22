"""Redaction of everything the factory's MCP server returns to a client.

`_bridge.redact_tokens` already knows the well-known credential shapes, but it writes to the task log and
its 60-70 character base64 rule can eat a 64-hex digest. This module wraps it for tool results: it protects
the identifiers a reader needs (git SHAs, image digests, UUIDs), adds the shapes `_bridge` lacks (OpenAI,
Union API keys, secret-named assignments, the server pod's own secret values but never the *name* of a
secret the pod carries in `AGENT_FACTORY_*_SECRET`), finds a secret after a terminal colour code or an
escape, and uses one marker.
`_bridge` is at the module-size limit, so the additions live here.

Factory-side by decision (docs/mcp-sdk-requirements.md, the table); the SDK side is gaps 4 and 10 there.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping

from ._bridge import redact, redact_tokens

MARKER = "[REDACTED]"

#: Values of these names are secrets even when they have no recognisable shape.
_SECRET_NAME = r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)"
_SECRET_NAME_ONLY = re.compile(_SECRET_NAME, re.IGNORECASE)
_ENV_NAME = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL", re.IGNORECASE)

#: Pure lowercase hex of exactly 40 (git SHA) or 64 (sha256 digest) characters, and UUIDs.
_PROTECTED = re.compile(
    r"(?<![0-9A-Za-z])(?:[0-9a-f]{64}|[0-9a-f]{40})(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])"
)
# Private-use characters cannot occur in the shapes below, so a placeholder is never part of a match's body.
_PLACEHOLDER = re.compile("\ue000(\\d+)\ue001")


def _start_of_token(alphabet: str) -> str:
    r"""Return a pattern for a position where a token may start, in plain, JSON- or URL-encoded text.

    After a character outside `alphabet` that is not part of an escape; after a JSON-escaped `\n`, `\t` or
    `\r`; after a lone backslash when the token does not begin with n/t/r (that would be the escape); after
    a percent-escape such as `%3D` or `%22`. The escape's own characters (`n`, `3D`) never start a token,
    or the match would begin there and swallow the secret. The backslash and `%` come first in the class, so
    an alphabet ending in `-` stays a literal `-` and not a range.
    """
    return (
        rf"(?:(?<![\\%{alphabet}])(?<!%[0-9A-Fa-f])"
        r"|(?<=\\[ntr])"
        r"|(?<=\\)(?![ntr])"
        r"|(?<=%[0-9A-Fa-f]{2}))"
    )


_BRIDGE_MARKER = re.compile(r"<redacted(?:, \d+ chars)?>")

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
#: A whole string of 60-80 base64 characters: one body line of a PEM key whose BEGIN and END lines are not
#: in the same string. A 64-hex digest has the same alphabet, so `_is_key_body_line` leaves pure hex alone.
_PEM_BODY_LINE = re.compile(r"^[A-Za-z0-9+/]{60,80}={0,2}$")
_HEX_ONLY = re.compile(r"[0-9a-fA-F]+")

_OPENAI_KEY = re.compile(_start_of_token("A-Za-z0-9") + r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")
#: `endpoint:client_id:secret:org`, base64 encoded: the format `flyte.remote` decodes an API key from.
#: `=` is not in the start-of-token class on purpose: `NAME=<key>`, `--flag=<key>` and `?k=<key>&` must start
#: a match. It stays in the lookahead, so a candidate ends at its own padding and not inside another token.
#: A candidate is a maximal run of token characters, and the key need not begin at the run's start: an
#: unrecognised sequence's tail (`123m`), a JSON escape's `n`, a URL escape's `3D` or a path element (`/v1/`)
#: can be in front of it, so `_mask_union_key_in` looks for the key up to `_JUNK_MAX` characters in, and
#: after each separator of the run. A key followed directly by more token characters (`<key>abc`) is one
#: longer token, outside the shape, as it is for `_bridge`'s 40-character AWS rule.
_BASE64_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_=-])")
_JUNK_MAX = 4
#: A key after a longer prefix in its own run (`/v1/apps/<key>`, `session-<key>`): the cut is tried after
#: each separator of the run, the separators being the alphabet's own `/`, `+`, `-`, `_` and `=`. Bounded,
#: so a run of separators costs a fixed number of decodes and the scan stays linear.
_SEPARATOR_CUTS_MAX = 16
_SEPARATORS = frozenset("/+-_=")
#: ECMA-48 CSI (parameter bytes, intermediate bytes, final byte), raw and in the textual forms a JSON
#: encoder, a Python repr or a shell script writes. Protected before any rule, so a secret right after a
#: colour code starts a token; restored at the end, so the line comes back as written.
_ANSI_CSI = re.compile(r"(?:\x1b|\\x1b|\\u001b|\\033|\\e)\[[0-?]*[ -/]*[@-~]")
#: An escape that writes a code point by number, as JSON (`\u201c`, a surrogate pair), Python's repr (`\xe9`,
#: `\U0001f600`), Go's %q (`\u003c`) and a shell (`\033`) write one. Its digits would otherwise be the head of
#: the token after it, and no rule starts a match inside a token. Protected before any rule, like a colour
#: code, so the token starts where the escape ends. The one-letter escapes are left to `_start_of_token`:
#: after a lone backslash, `\tskey-` and `\eyJ` are a path separator and a key, which protecting `\t` and
#: `\e` would behead.
_ESCAPE = re.compile(r"\\(?:x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|[0-7]{3})")
#: `NAME=value`, `NAME: value`, `"name": "value"` (also JSON-escaped, at any depth), for a secret-ish NAME.
#: The name stays.
#: The name is a whole token: the lookbehind lets a match start only where a token starts, and the lookahead
#: asks whether that token holds a secret word. A pattern with an unbounded run on each side of the word,
#: retried at every word boundary, is quadratic or worse on `a-a-a-...`. Quoted values are bounded for the
#: same reason (an unterminated quote would otherwise scan to the end of the text at every `NAME="`).
#: `-----BEGIN` is left to `_bridge`, which masks the whole key rather than its first word.
#: The value is one of: a quoted string (its quotes are kept, so JSON stays JSON), a flat `[...]` list (each
#: quoted item is masked), or a bare word. A bare word stops at whitespace, `"`, `;`, `}`, `]` and at a comma
#: that ends a JSON member (one before a space or a quote), and `null`, `none`, `true` and `false` are not
#: values, so `"credentials": [], "x": 1` and `"token": null}` keep their delimiters.
_SECRET_ASSIGNMENT = re.compile(
    rf"""(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*?{_SECRET_NAME})([A-Za-z0-9_-]+)(\\*["']?\s*[=:]\s*)"""
    r"""(?!-----BEGIN)(\[[^\[\]]{0,4096}\]|(\\*["'])(.{0,4096}?)(\4)"""
    r"""|(?!(?:null|none|true|false)(?![^\s,;}\]"]))(?![\[{])(?:\\*["'])?[^\s,;}\]"]+(?:,(?![\s"])[^\s,;}\]"]+)*)""",
    re.IGNORECASE,
)
#: A quoted item of a `[...]` value, plain or JSON-escaped at any depth.
_QUOTED_ITEM = re.compile(r"""(\\*["'])(?!\1)(.{1,4096}?)(\1)""")


#: A key that names or counts secrets rather than holding one: `secret_names`, `token_count`, `max_tokens`,
#: `missing_secrets`, `api_key_set`. Its value is the diagnosis (which secret is missing), so it stays. A
#: list-word (`missing_`) makes a list of names only when a plural secret word ends the key, and a count-word
#: (`max_`) counts only tokens: `MINIO_ROOT_PASSWORD` and `INPUT_PASSWORD` hold secrets.
_NAMES_SECRETS = re.compile(
    r"(?:names?|count|tokens|ids?|ttl(?:_s)?|expires?(?:_at)?|length|len|size|set|valid|refs?)$"
    r"|^(?:missing|required|expected|unknown|unset|needed)[_-](?:[a-z0-9]+[_-])*"
    r"(?:secrets|tokens|keys|credentials|passwords)$"
    r"|^(?:num|max|min|input|output|total)[_-]?tokens?$",
    re.IGNORECASE,
)
#: A value that is itself a secret's name, as in `MissingSecrets: claude_token` or `env: GITHUB_TOKEN`:
#: letters in one case, the secret word a whole `_`- or `-`-separated part. A key or password with digits or
#: mixed case (`Zx9_token_q`) is not taken for a name.
_SECRET_NAME_VALUE = re.compile(
    rf"(?:[a-z]+[_-])*(?i:{_SECRET_NAME})(?:[_-][a-z]+)*s?|(?:[A-Z]+[_-])*{_SECRET_NAME}(?:[_-][A-Z]+)*S?"
)


#: The factory's own variables whose value is a secret's *name*, by its naming rule:
#: `AGENT_FACTORY_<X>_SECRET` names the secret that `<X>` is mounted from under another variable
#: (`_union_identity.SECRET_ENV` beside `KEY_ENV`; `AGENT_FACTORY_CLAUDE_TOKEN_SECRET` beside
#: `CLAUDE_CODE_OAUTH_TOKEN`, in `_profile.deploy_env`), `AGENT_FACTORY_SECRETS` lists them, and
#: `AGENT_FACTORY_<X>_ENV` names a variable (`_deploy.DEPLOY_KEY_ENV`). A name is the diagnosis a
#: `secret_mount` failure needs. A test holds this rule to the deploy's real environments: what they
#: mount is collected, what they set in plain text is not.
_NAMES_A_SECRET = re.compile(r"^AGENT_FACTORY_(?:[A-Z0-9]+_)*(?:SECRETS?|ENV)$")


def _names_secrets(key: str) -> bool:
    return _NAMES_SECRETS.search(key.strip("_-")) is not None or _NAMES_A_SECRET.match(key) is not None


def _mask_assignment(match: re.Match[str]) -> str:
    name, sep, value = match.group(1), match.group(2), match.group(3)
    bare = match.group(5) if match.group(4) is not None else value
    if _names_secrets(name) or _SECRET_NAME_VALUE.fullmatch(bare or "-"):
        return match.group(0)
    if value.startswith("["):
        return name + sep + _QUOTED_ITEM.sub(lambda m: m.group(1) + MARKER + m.group(3), value)
    if match.group(4) is not None:  # quoted: keep the quotes; an empty string hides nothing
        return name + sep + (match.group(4) + MARKER + match.group(6) if match.group(5) else value)
    # A bare value under a quoted JSON key is a number or a word: quote the marker so the JSON still parses.
    quote = sep[: sep.index('"') + 1] if '"' in sep else ""
    return name + sep + quote + MARKER + quote


#: `Authorization: Bearer|Basic ...` and friends, whatever the value looks like.
_AUTHORIZATION = re.compile(
    "("
    + _start_of_token("A-Za-z0-9_")
    + r"""authorization\\?["']?\s*[:=]\s*\\?["']?)(?:bearer|basic|token|digest|negotiate)\s+[^\s"',}]+""",
    re.IGNORECASE,
)
#: A bearer token with no `Authorization` before it. `_bridge` has this rule too, but only sees the text
#: after hex values are protected, so a hex-only token would survive it.
_BEARER = re.compile(
    "(" + _start_of_token("A-Za-z0-9_") + r"bearer\s+)[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE
)
#: The password of `scheme://user:password@host`. The lookbehind and the bounded scheme keep a start from
#: being retried at every character of a long `a-a-a-`; user and password stop at `/`, so runs do not overlap.
_URL_PASSWORD = re.compile(
    "(" + _start_of_token("A-Za-z0-9+.-") + r"[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s:/@]+:)[^\s@/]+(?=@)"
)
_SLACK_APP_TOKEN = re.compile(_start_of_token("A-Za-z0-9") + r"xapp-[A-Za-z0-9-]{10,}")
#: The path of a Slack incoming webhook is its secret.
_SLACK_WEBHOOK = re.compile(r"(https://hooks\.slack\.com/services/)[A-Za-z0-9_/-]+")
#: A bare AWS secret access key (40 base64 characters) next to an access key id, in either order. The
#: gap holds the separator (space, comma, quote, newline). Run after `_PROTECTED`, so a git SHA is not
#: taken for one.
_AWS_KEY_ID = r"(?:AKIA|ASIA)[0-9A-Z]{16}"
_AWS_SECRET_KEY = r"[A-Za-z0-9/+]{40}"
_AWS_PAIR = re.compile(
    rf"({_AWS_KEY_ID}[^A-Za-z0-9/+]{{1,24}}){_AWS_SECRET_KEY}(?![A-Za-z0-9/+])"
    rf"|{_start_of_token('A-Za-z0-9/+')}{_AWS_SECRET_KEY}([^A-Za-z0-9/+]{{1,24}}{_AWS_KEY_ID})"
)


def collect_secret_values_from_env(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Return the values (8+ characters) of the variables whose names say they hold a secret.

    A factory variable that names a secret (`AGENT_FACTORY_UNION_SECRET=factory-project-key`) holds none and
    is left out, or every tool would mask the one word that says which secret an app mounts.
    """
    return tuple(
        v for k, v in environ.items() if _ENV_NAME.search(k) and not _names_secrets(k) and len(v) >= 8
    )


#: The server pod's own secrets, read once: they are what a tool that echoes its environment would leak.
_ENV_SECRETS = collect_secret_values_from_env(os.environ)


def _is_union_api_key(token: str) -> bool:
    padded = token.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    # The endpoint may hold colons of its own (`dns:///host`, `host:443`), so count from the right: the last
    # three fields are client id, secret and org, none of them empty. Prose or a path is not one token.
    fields = decoded.rsplit(":", 3)
    return (
        len(fields) == 4
        and decoded.isprintable()
        and not any(c.isspace() for c in decoded)
        and all(fields[:3])
    )


def _mask_union_key_in(match: re.Match[str]) -> str:
    """Mask the Union API key in a token run, keeping the characters in front of it that are not the key's."""
    run = match.group(0)
    if _is_union_api_key(run):
        return MARKER
    for cut in range(1, min(_JUNK_MAX, len(run) - 40) + 1):
        if _is_union_api_key(run[cut:]):
            return run[:cut] + MARKER
    # Nearest the key first: a key holds few separators of its own, so the true boundary is reached early
    # whatever the length of the prefix, and the bound only ever cuts off prefix.
    cuts = [i + 1 for i in range(len(run) - 41, _JUNK_MAX - 1, -1) if run[i] in _SEPARATORS]
    for cut in cuts[:_SEPARATOR_CUTS_MAX]:
        if _is_union_api_key(run[cut:]):
            return run[:cut] + MARKER
    return run


def _is_key_body_line(text: str) -> bool:
    return _PEM_BODY_LINE.fullmatch(text) is not None and _HEX_ONLY.fullmatch(text) is None


def mask_private_key_spans(items: list[object]) -> list[object]:
    """Mask every item from a `BEGIN ... PRIVATE KEY` line to its `END` line, across list items.

    A key logged one line per entry is split over many strings, so no per-string rule sees it whole.
    Items are strings or dicts with a `message` (the log tools' shape); other items pass through.
    """
    out: list[object] = []
    inside = False
    for item in items:
        if isinstance(item, str):
            text = item
        elif isinstance(item, Mapping) and isinstance(item.get("message"), str):
            text = item["message"]
        else:
            out.append(item)
            continue
        has_end = _PEM_END.search(text) is not None
        if inside or (_PEM_BEGIN.search(text) and not has_end):
            inside = not has_end
            out.append(MARKER if isinstance(item, str) else {**item, "message": MARKER})
        else:
            out.append(item)
    return out


def redact_text(text: str) -> str:
    """Replace every secret in `text` with `MARKER`, keeping git SHAs, digests and UUIDs readable.

    A secret named by its context (a known pod value, `Authorization: Bearer`, `password=`) goes first, before
    hex values are protected: the context makes it a secret even when it is 40 or 64 hex characters or a UUID.
    """
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\ue000{len(protected) - 1}\ue001"

    text = redact(text, *_ENV_SECRETS)  # by value, so a value holding an escape is found before it is hidden
    text = _ANSI_CSI.sub(protect, text)
    text = _ESCAPE.sub(protect, text)
    text = _AUTHORIZATION.sub(lambda m: m.group(1) + MARKER, text)
    text = _BEARER.sub(lambda m: m.group(1) + MARKER, text)
    if _SECRET_NAME_ONLY.search(text):  # a cheap scan that spares most lines the costlier pattern
        text = _SECRET_ASSIGNMENT.sub(_mask_assignment, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + MARKER, text)
    text = _SLACK_WEBHOOK.sub(lambda m: m.group(1) + MARKER, text)
    text = _PROTECTED.sub(protect, text)
    text = _AWS_PAIR.sub(lambda m: f"{m.group(1)}{MARKER}" if m.group(1) else f"{MARKER}{m.group(2)}", text)
    text = redact_tokens(text)
    text = _OPENAI_KEY.sub(MARKER, text)
    text = _SLACK_APP_TOKEN.sub(MARKER, text)
    text = _BASE64_CANDIDATE.sub(_mask_union_key_in, text)
    text = _BRIDGE_MARKER.sub(MARKER, text)
    text = _PLACEHOLDER.sub(lambda m: protected[int(m.group(1))], text)
    return MARKER if _is_key_body_line(text) else text


def shorten_after_redaction(text: str, limit: int) -> str:
    """Return `text` redacted and then cut to `limit` characters, never inside a marker.

    Cutting first would leave part of a secret that no rule then recognises (39 of an AWS key's 40
    characters). A marker that straddles the cut is kept whole, so the result may run up to
    `len(MARKER) - 1` past `limit`.
    """
    out = redact_text(text)
    if len(out) <= limit:
        return out
    marker_at = out.find(MARKER, max(0, limit - len(MARKER) + 1), limit + len(MARKER) - 1)
    return out[: marker_at + len(MARKER)] if 0 <= marker_at < limit else out[:limit]


def _is_secret_key(key: str) -> bool:
    return key.lower() == "authorization" or (
        _SECRET_NAME_ONLY.search(key) is not None and not _names_secrets(key)
    )


def redact_result(obj: object) -> object:
    """`redact_text` over every string in a JSON-like result; other types come back unchanged.

    A dict value under a secret-ish key, or under `authorization`, is masked whole, since
    `{"api_key": "hunter2"}` has no text shape and `{"authorization": "Bearer <hex>"}` is not worth parsing.
    Under such a key every string inside a list or dict is masked too; `None`, numbers and the shape are kept.
    """
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, Mapping):
        return {
            key: (
                _mask_strings(value) if isinstance(key, str) and _is_secret_key(key) else redact_result(value)
            )
            for key, value in obj.items()
        }
    # The spans are found before per-item redaction: `redact_text` already rewrites a lone BEGIN line, and the
    # span would then never start.
    if isinstance(obj, list):
        return [redact_result(item) for item in mask_private_key_spans(obj)]
    if isinstance(obj, tuple):
        return tuple(redact_result(item) for item in mask_private_key_spans(list(obj)))
    return obj


def _mask_strings(obj: object) -> object:
    """Every non-empty string in `obj` as `MARKER`, keeping its shape: the value of a secret-named key."""
    if isinstance(obj, str):
        return MARKER if obj else obj
    if isinstance(obj, Mapping):
        return {key: _mask_strings(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_mask_strings(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_mask_strings(item) for item in obj)
    return obj
