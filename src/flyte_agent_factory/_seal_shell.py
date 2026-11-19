"""What a tool call in a sealed run tries: shell parsing and upstream-route classification.

`find_routes` says which routes to the reference fix one tool call tries (git fetch family, hosting API, web
tools, task-project download, forbidden-path reads), judged from the call alone. Two modules read commands
through this one, so that they cannot disagree about what a command does: the guard (`_seal_guard`) denies a
call before it runs, and the audit (`_seal_audit`) decides from a recorded call's result whether it worked.
Codex roles have no guard hook, so for them the audit is the only command-level check, and a spelling only
one of the two understood was a route the other let through.

The guard and the audit differ only in what `Rules` gives them: the guard's `is_local_source` and
`requirement_lines` look at the filesystem, its local roots and the session's working directory; the audit's
defaults judge the text alone, because the same record must get the same verdict on any machine.

The shell handling is a heuristic over text, not a shell: shlex, wrappers, `bash -c`, `eval` and `$(...)` to
`MAX_DEPTH` levels, heredocs fed to a shell or an interpreter. What it cannot see is stated by the corpus
(`tests/seal_corpus.py`): code assembled at run time (`python -c "$(echo ... | base64 -d)"`) and a command
that swallows its own failure (`git fetch x 2>/dev/null; echo done`).
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple
from urllib.parse import unquote, urlparse

GIT_FETCH = "git-fetch"
HTTP_FETCH = "http-fetch"
GH = "gh"
WEB_TOOL = "web-tool"
BROWSER = "browser"
TASK_PACKAGE = "task-package"
FORBIDDEN_READ = "forbidden-read"
#: `git remote add|set-url NAME URL` with a URL that is not local. It retrieves nothing by itself, so the
#: audit does not count it, but the guard denies it: the next `git fetch` would use it.
GIT_REMOTE_ADD = "git-remote-add"
#: An install of a release file of the task project that is already on disk. The guard denies it (the file
#: may be a copy of the release); the audit does not count it: the route was whatever brought the file.
TASK_FILE = "task-file"

#: `bash -c`, `eval` and `$(...)` nest; the guard's bound, also the audit's.
MAX_DEPTH = 3
SHELLS = ("bash", "sh", "zsh", "dash")
HTTP_CLIENTS = ("curl", "wget", "aria2c", "http", "https", "xh", "httpie")
#: Clients whose first word that is not an option is the URL (`https api.github.com/user`).
URL_FIRST_CLIENTS = ("http", "https", "xh", "httpie")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
WEB_TOOLS = ("WebSearch", "WebFetch")
FILE_TOOLS = ("Read", "Grep", "Glob")

#: `<<EOF`, `<<'EOF'`, `<<-"EOF"`: a here-document whose body follows on the next lines.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
URL_HOST = re.compile(r"https?://(?:[^/\s\"'<>@]*@)?(\[[^\]]+\]|[^/\s:\"'<>?#\\]+)", re.IGNORECASE)
ANY_URL = re.compile(r"[A-Za-z][\w+.-]*://[^\s'\"]+")
BARE_HOST = re.compile(r"^(?:[\w-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:/.*)?$")
HOSTING_HOSTS = re.compile(r"api\.github\.com|api\.gitlab\.com|api\.bitbucket\.org", re.IGNORECASE)
INTERPRETER = re.compile(r"python[\d.]*|pypy\d*|node(js)?|ruby|perl")
PYTHON_WORD = re.compile(r"python[\d.]*|pypy\d*")
PIP_WORD = re.compile(r"pip[\d.]*")
ARCHIVE_SUFFIX = re.compile(r"\.(tar\.gz|tar\.bz2|tar\.xz|tgz|zip|whl|git)$", re.IGNORECASE)
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
#: Words that start a simple command without being its command.
SHELL_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time"}
#: Wrappers that run the rest of the line, with their options that take a separate value.
WRAPPERS: dict[str, set[str]] = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "nice": {"-n", "--adjustment"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
    "command": set(),
    "builtin": set(),
    "nohup": set(),
    "exec": set(),
    "setsid": set(),
}
UV_RUN_VALUE_OPTIONS = {"--with", "--python", "-p", "--project", "--directory", "--extra", "--group"}
#: Options of curl/wget/aria2c that take a separate value, so the value is not mistaken for a host.
CLIENT_VALUE_OPTIONS = {
    "-o", "-O", "-H", "-d", "-X", "-u", "-A", "-e", "-b", "-c", "-m", "-w", "-T", "-F", "-K", "-P", "-i",
    "-a", "-t", "-D", "-r", "-x", "-E", "-z", "-C", "-Q", "-U", "-j",
}  # fmt: skip
GIT_GLOBAL_VALUE_OPTIONS = {
    "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env",
}  # fmt: skip
#: Options of fetch/pull/clone/ls-remote/fetch-pack that take a separate value (clone's `-u` is added there).
GIT_FETCH_VALUE_OPTIONS = {
    "--depth", "--deepen", "--shallow-since", "--shallow-exclude", "-j", "--jobs", "--upload-pack",
    "--receive-pack", "-o", "--server-option", "--filter", "--refmap", "--negotiation-tip", "-b", "--branch",
    "--origin", "--template", "--reference", "--separate-git-dir", "--config", "--bundle-uri",
}  # fmt: skip
GIT_REMOTE_ADD_VALUE_OPTIONS = {"-t", "--track", "-m", "--master"}
#: Options of pip/uv/pipx install commands that take a separate value (`-r` and `-e` are read separately).
PIP_VALUE_OPTIONS = {
    "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f", "--find-links", "-t", "--target",
    "--python", "-p", "--prefix", "--root", "--no-binary", "--only-binary", "--platform", "--python-version",
    "--implementation", "--abi", "--upgrade-strategy", "--cache-dir", "--progress-bar", "--resolution",
    "--index-strategy", "--group", "--extra", "--src", "--index", "--default-index", "--exclude-newer",
    "-d", "--dest", "-w", "--wheel-dir", "-C", "--config-settings", "--with", "--with-requirements",
}  # fmt: skip
#: Options of `uvx`/`pipx run`/`uv tool run` that name the package the tool comes from.
TOOL_SPEC_OPTIONS = {"--from", "--spec"}


class Route(NamedTuple):
    """One route a tool call tries: its kind, the text that shows it, and the guard rule that denies it."""

    kind: str
    evidence: str
    rule: str = ""


class Command(NamedTuple):
    """One simple command, wrappers stripped, and how deep in `bash -c`/`eval`/`$(...)` it was found.

    `stdin_code` is a heredoc body the command reads as code (`python - <<EOF`).
    """

    words: list[str]
    depth: int
    stdin_code: str = ""


def is_local_host(host: str) -> bool:
    """Return whether a URL host stays on this machine."""
    host = host.lower()
    return host in LOCAL_HOSTS or host.startswith("127.") or host.endswith(".localhost")


def find_remote_host(text: str) -> str | None:
    """Return the first non-local host of an http(s) URL in the text, or None."""
    return next((m.group(1) for m in URL_HOST.finditer(text) if not is_local_host(m.group(1))), None)


def has_remote_url(text: str) -> bool:
    """Return whether the text names an http(s) URL on a non-local host."""
    return find_remote_host(text) is not None


def is_local_path(source: str, git_dir: str = "") -> bool:
    """Return whether a git source is path-shaped or a local URL, from its text alone (the audit's rule).

    `git_dir` (a `git -C` directory) is accepted to match `Rules.is_local_source` and changes nothing: a
    path is a path under any directory, and a remote's name is not one under any.
    """
    del git_dir
    if source in (".", "..") or source.startswith(("/", "./", "../", "~", "file://")):
        return True
    if "://" in source:
        return not has_remote_url(source)
    if re.match(r"[\w.-]+@[\w.-]+:|[\w.-]+:[^/\\]", source):
        return False
    # A bare word is a remote's name; `$VAR/dir` is a path under a variable.
    return bool(re.match(r"\$\{?\w+\}?/", source))


def read_no_requirements(path: str) -> list[str]:
    """Return no requirement lines: the audit reads no files, so a `-r FILE` names nothing it can see."""
    del path
    return []


@dataclass(frozen=True)
class Rules:
    """What the route patterns need to know about the task and the machine.

    `is_local_source(source, git_dir)` says whether a git source is local, given the `git -C` directory of
    the command ("" without one); `requirement_lines(path)` returns the requirements of a `-r FILE`.
    """

    task_project: str = ""
    forbidden: tuple[str, ...] = ()
    is_local_source: Callable[[str, str], bool] = is_local_path
    requirement_lines: Callable[[str], list[str]] = read_no_requirements


def normalize_project(name: str) -> str:
    """Return a PyPI project name in its normalized form (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- splitting ------------------------------------------------------------------------------------------


def split_heredocs(command: str) -> list[tuple[str | None, str]]:
    """Return a command's chunks in order: (None, shell lines) and (the pipeline reading it, a heredoc body).

    The line holding `<<TAG` stays in the shell chunk before its body; the pipeline is that line's commands
    joined by `|` around the operator (`cat <<EOF | bash` gives `cat | bash`). A `<<TAG` whose closing `TAG`
    line never comes leaves the following lines as shell lines, so nothing is hidden from the rules.
    """
    lines = command.split("\n")
    out: list[tuple[str | None, str]] = []
    shell: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        shell.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            end = next((k for k in range(i, len(lines)) if lines[k].strip() == m.group(2)), None)
            if end is None:
                break
            out.append((None, "\n".join(shell)))
            shell = []
            left = re.split(r";|&&|\|\||(?<![<>])&|\(", line[: m.start()])[-1]
            right = re.split(r";|&&|\|\||(?<![<>])&(?!>)|\)", line[m.end() :])[0]
            out.append((left + right, "\n".join(lines[i:end])))
            i = end + 1
    if shell:
        out.append((None, "\n".join(shell)))
    return out


def split_segments(script: str) -> list[list[str]]:
    """Split a script into simple commands on `;`, `&&`, `||`, `|`, `&`, parentheses and newlines."""
    # No comment handling: a `#` line must not swallow the commands after the newline, and `#` in a URL is
    # not a comment. A newline inside quotes (a multi-line `python -c "..."`) stays in its word.
    lexer = shlex.shlex(
        script.replace("\\\n", "").replace("\n", " ; "), posix=True, punctuation_chars=";&|()"
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        # Unbalanced quotes (an apostrophe in prose): split the text rather than let it through.
        tokens = [t for part in re.split(r"[;&|()\n]+", script) for t in [*part.split(), ";"]]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= set(";&|()"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def strip_redirects(tokens: list[str]) -> list[str]:
    """Return the tokens without redirections (`> file`) and the fd number before them."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok and set(tok) <= set("<>&"):
            if out and out[-1].isdigit():
                out.pop()
            i += 2
        else:
            out.append(tok)
            i += 1
    return out


def skip_options(args: Sequence[str], value_options: set[str]) -> int:
    """Return the index of the first argument that is not an option (values of `value_options` skipped)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in value_options else 1
    return i


def strip_wrappers(words: list[str]) -> list[str]:
    """Drop `VAR=x` prefixes, shell keywords and the wrappers (`env`, `timeout`, `sudo`, `uv run`...)."""
    while words:
        name = posixpath.basename(words[0])
        rest = words[1:]
        if ASSIGNMENT.match(words[0]) or name in SHELL_KEYWORDS:
            words = rest
        elif name in WRAPPERS:
            rest = rest[skip_options(rest, WRAPPERS[name]) :]
            words = rest[1:] if name == "timeout" else rest  # timeout's duration
        elif name == "uv" and rest[:1] == ["run"]:
            words = rest[1:][skip_options(rest[1:], UV_RUN_VALUE_OPTIONS) :]
        else:
            return words
    return words


def find_inner_script(words: list[str]) -> str | None:
    """Return the command text a `bash -c SCRIPT` / `eval ARGS` runs, if the command is one."""
    name, args = posixpath.basename(words[0]), words[1:]
    if name == "eval":
        return " ".join(args)
    if name not in SHELLS:
        return None
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg:
            return args[i + 1] if i + 1 < len(args) else None
    return None


def read_heredoc_pipeline(pipeline: str) -> tuple[str, list[str]]:
    """Return what reads a heredoc: ("shell", []), ("code", the interpreter's words) or ("", [])."""
    readers = [strip_wrappers(strip_redirects(part.split())) for part in pipeline.split("|")]
    readers = [r for r in readers if r]
    if any(posixpath.basename(r[0]) in SHELLS for r in readers):
        return "shell", []
    code = next((r for r in readers if INTERPRETER.fullmatch(posixpath.basename(r[0]))), None)
    return ("code", code) if code is not None else ("", [])


def walk_commands(command: str, depth: int = 0) -> Iterator[Command]:
    """Yield every simple command a shell command runs, in order, with what it nests to `MAX_DEPTH`.

    That is: the commands of each `$(...)` or backtick substitution (anywhere in the text, quoted or not:
    erring towards seeing too much), each simple command, then the script of a `bash -c`/`eval` it is, and
    the body of a heredoc fed to a shell. A heredoc fed to an interpreter comes as that interpreter's
    command with the body as `stdin_code`; any other heredoc body is data and yields nothing.
    """
    if depth < MAX_DEPTH:
        for match in SUBSTITUTION.finditer(command):
            inner = match.group(1) if match.group(1) is not None else match.group(2)
            yield from walk_commands(inner, depth + 1)
    for pipeline, body in split_heredocs(command):
        if pipeline is None:
            for tokens in split_segments(body):
                words = strip_wrappers(tokens)
                if not words:
                    continue
                yield Command(words, depth)
                script = find_inner_script(words)
                if script is not None and depth < MAX_DEPTH:
                    yield from walk_commands(script, depth + 1)
            continue
        reader, words = read_heredoc_pipeline(pipeline)
        if reader == "shell" and depth < MAX_DEPTH:
            yield from walk_commands(body, depth + 1)
        elif reader == "code":
            yield Command(words, depth, body)


def positionals(args: Sequence[str], value_options: set[str]) -> list[str]:
    """Return the non-option arguments, skipping the values of the options that take one; all after `--`."""
    found: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            found.extend(args[i + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            i += 2 if arg in value_options else 1
            continue
        found.append(arg)
        i += 1
    return found


# --- git ------------------------------------------------------------------------------------------------


def split_git(args: Sequence[str]) -> tuple[str, str, list[str]]:
    """Split `git [-C dir] [-c k=v] ... SUB ARGS` into (the `-C` directory or "", SUB, ARGS)."""
    git_dir = ""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            git_dir = posixpath.join(git_dir, args[i + 1])
            i += 2
        elif args[i] in GIT_GLOBAL_VALUE_OPTIONS:
            i += 2
        else:
            i += 1
    sub = args[i] if i < len(args) else ""
    return git_dir, sub, list(args[i + 1 :])


def classify_git(args: Sequence[str], rules: Rules) -> list[Route]:
    """Return the routes of a `git` command given its arguments."""
    git_dir, sub, rest = split_git(args)

    def remote(source: str) -> bool:
        return not rules.is_local_source(source, git_dir)

    if sub in ("fetch", "pull", "clone", "ls-remote", "fetch-pack"):
        value_options = GIT_FETCH_VALUE_OPTIONS | ({"-u"} if sub == "clone" else set())
        pos = positionals(rest, value_options)
        # No repository argument means the default remote: a named remote, which a sealed repo has not.
        if not pos or remote(pos[0]):
            return [Route(GIT_FETCH, " ".join(["git", sub, *pos[:1]]), GIT_FETCH)]
    elif sub == "archive":
        option = next((k for k, a in enumerate(rest) if a == "--remote" or a.startswith("--remote=")), None)
        if option is not None:
            source = rest[option].partition("=")[2] or (rest[option + 1] if option + 1 < len(rest) else "")
            if remote(source):
                return [Route(GIT_FETCH, f"git archive --remote={source}", GIT_FETCH)]
    elif sub == "remote" and rest[:1] == ["update"]:
        return [Route(GIT_FETCH, "git remote update", GIT_FETCH)]
    elif sub == "remote" and rest[:1] in (["add"], ["set-url"]) and "--delete" not in rest:
        pos = positionals(rest[1:], GIT_REMOTE_ADD_VALUE_OPTIONS if rest[0] == "add" else set())
        if len(pos) >= 2 and remote(pos[1]):
            evidence = f"git remote {rest[0]} {pos[0]} {pos[1]}"
            # `git remote add -f` fetches from the remote it adds.
            if rest[0] == "add" and ("-f" in rest or "--fetch" in rest):
                return [Route(GIT_FETCH, evidence, GIT_FETCH)]
            return [Route(GIT_REMOTE_ADD, evidence, GIT_REMOTE_ADD)]
    elif sub == "submodule" and "update" in rest:
        return [Route(GIT_FETCH, "git submodule update", GIT_FETCH)]
    return []


# --- packages -------------------------------------------------------------------------------------------


def url_names_project(url: str, project: str) -> bool:
    """Return whether a path segment of the URL or path is the project, a release file of it, or its repo."""
    for segment in unquote(urlparse(url.removeprefix("git+")).path).split("/"):
        stem = ARCHIVE_SUFFIX.sub("", segment.split("@")[0])
        if project in {normalize_project(stem), normalize_project(re.sub(r"-\d.*$", "", stem))}:
            return True
    return False


def collect_install_specs(args: Sequence[str], rules: Rules) -> list[str]:
    """Return the requirements an install names: positionals, `-r` file lines, `-e` URLs; not local dirs."""
    specs: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        value = args[i + 1] if i + 1 < len(args) else ""
        if arg in {"-r", "--requirement"}:
            specs += rules.requirement_lines(value)
            i += 2
        elif arg.startswith("--requirement="):
            specs += rules.requirement_lines(arg.split("=", 1)[1])
            i += 1
        elif arg in {"-e", "--editable"}:
            specs += [value] if "://" in value else []
            i += 2
        elif arg in PIP_VALUE_OPTIONS:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            specs.append(arg)
            i += 1
    return [
        s
        for s in specs
        if not s.startswith(("/", "./", "../", "~")) or "://" in s or ARCHIVE_SUFFIX.search(s)
    ]


def classify_install(args: Sequence[str], rules: Rules, via: str) -> list[Route]:
    """Return the routes of an install (`pip install`, `uv add`, `pipx install`...) given its arguments.

    Matched: `Tasklib`, `tasklib==1.2`, `tasklib[x]`, `tasklib @ URL`, a `git+` URL or an archive of it
    (a local archive is `TASK_FILE`); and an archive or `git+` URL of any project on a remote host, which
    is a download from outside the index.
    """
    project = normalize_project(rules.task_project)
    specs = collect_install_specs(args, rules)
    for spec in specs if project else []:
        name = REQUIREMENT_NAME.match(spec)
        if name and normalize_project(name.group(1)) == project:
            return [Route(TASK_PACKAGE, spec, "pip-task-project")]
        url = spec.partition(" @ ")[2] or spec
        if ("://" in spec or ARCHIVE_SUFFIX.search(spec)) and url_names_project(url, project):
            return [Route(TASK_PACKAGE if "://" in url else TASK_FILE, spec, "pip-task-project")]
    for spec in specs:
        if has_remote_url(spec) and (ARCHIVE_SUFFIX.search(spec) or spec.startswith("git+")):
            return [Route(TASK_PACKAGE, f"{via} {spec}", "pip-download")]
    return []


def classify_pip(args: Sequence[str], rules: Rules, via: str) -> list[Route]:
    """Return the routes of `pip ARGS`; also `python -m pip`, `uv pip` and `pipx`, whose ARGS start alike."""
    index = next((i for i, a in enumerate(args) if not a.startswith("-")), None)
    if index is None:
        return []
    sub = args[index]
    if sub == "download":
        return [Route(TASK_PACKAGE, f"{via} download", "pip-download")]
    if sub in ("install", "wheel"):
        return classify_install(args[index + 1 :], rules, f"{via} {sub}")
    return []


def classify_tool_run(args: Sequence[str], rules: Rules, via: str) -> list[Route]:
    """Return the routes of `uvx ARGS` / `pipx run ARGS`: a pip it runs, or the package the tool is from."""
    value_options = PIP_VALUE_OPTIONS | TOOL_SPEC_OPTIONS
    specs = [args[k + 1] for k, a in enumerate(args[:-1]) if a in TOOL_SPEC_OPTIONS]
    i = skip_options(args, value_options)
    if i < len(args) and PIP_WORD.fullmatch(args[i]):
        return classify_pip(args[i + 1 :], rules, f"{via} {args[i]}")
    return classify_install([*specs, *args[i : i + 1]], rules, via)


def classify_uv(args: Sequence[str], rules: Rules) -> list[Route]:
    """Return the routes of `uv ARGS` (`uv run` is a wrapper, stripped before)."""
    if args[:1] == ["pip"]:
        return classify_pip(args[1:], rules, "uv pip")
    if args[:1] == ["add"]:
        return classify_install(args[1:], rules, "uv add")
    if args[:2] == ["tool", "run"]:
        return classify_tool_run(args[2:], rules, "uv tool run")
    if args[:1] == ["tool"]:
        return classify_install(args[2:], rules, f"uv tool {' '.join(args[1:2])}")
    return []


def classify_pipx(args: Sequence[str], rules: Rules) -> list[Route]:
    """Return the routes of `pipx ARGS`."""
    index = next((i for i, a in enumerate(args) if not a.startswith("-")), None)
    if index is not None and args[index] == "run":
        return classify_tool_run(args[index + 1 :], rules, "pipx run")
    return classify_pip(args, rules, "pipx")


# --- network clients ------------------------------------------------------------------------------------


def find_url_host(text: str) -> str:
    """Return the lower-cased host of a URL or a bare `host/path`."""
    return (urlparse(text if "://" in text else f"//{text}").hostname or "").lower()


def find_client_host(name: str, args: Sequence[str]) -> str | None:
    """Return the non-local host an HTTP client (`curl`, `wget`, `https`...) is pointed at, or None."""
    previous = ""
    first_target_seen = False
    for arg in args:
        after_value_option = previous in CLIENT_VALUE_OPTIONS
        previous = arg
        if after_value_option and "://" not in arg:
            continue
        if "://" in arg:
            bad = next((h for h in map(find_url_host, ANY_URL.findall(arg)) if not is_local_host(h)), None)
            if bad is not None:
                return bad
            first_target_seen = True
        elif not arg.startswith(("-", "$")):
            if name in URL_FIRST_CLIENTS and not first_target_seen and not arg.isupper():
                first_target_seen = True
                if not is_local_host(find_url_host(arg)) and not arg.startswith(":"):
                    return arg
            elif (
                name not in URL_FIRST_CLIENTS
                and BARE_HOST.match(arg)
                and not is_local_host(find_url_host(arg))
            ):
                return arg
    # A hosting API named some other way (`-H 'Host: api.github.com'`, a quoted fragment).
    return next((m.group(0) for a in args if (m := HOSTING_HOSTS.search(a))), None)


def find_inline_code_host(args: Sequence[str]) -> str | None:
    """Return the non-local host named by an interpreter run with inline code (`-c`, `-e`, `-p`), or None."""
    inline = "--eval" in args or any(
        a[:1] == "-" and a[1:2] in {"c", "e", "p"} for a in args if a[:2] != "--"
    )
    if not inline:
        return None
    return next((h for a in args if (h := find_remote_host(a)) is not None), None)


# --- one command ----------------------------------------------------------------------------------------


def matches_forbidden(path: str, forbidden: tuple[str, ...]) -> bool:
    """Return whether a path (or the fixed start of a glob) is, or lies under, a forbidden glob."""
    prefix = re.split(r"[*?\[]", path, maxsplit=1)[0]
    for candidate in dict.fromkeys([path, prefix]):
        if not candidate:
            continue
        norm = posixpath.normpath(candidate)
        parents = [norm, *(str(p) for p in _parents(norm))]
        for glob in forbidden:
            base = re.sub(r"(/\*\*?)+$", "", glob).rstrip("/")
            if any(fnmatch.fnmatch(p, glob) or fnmatch.fnmatch(p, base) for p in parents):
                return True
    return False


def _parents(path: str) -> Iterator[str]:
    while path not in ("", "/", "."):
        path = posixpath.dirname(path)
        if path:
            yield path


def classify_segment(command: Command, rules: Rules) -> list[Route]:
    """Return the routes of one simple command (wrappers already stripped, as `walk_commands` yields it)."""
    words = strip_redirects(command.words)
    if not words:
        return []
    name, args = posixpath.basename(words[0]), words[1:]
    routes: list[Route] = []
    # `--file=/path` and `/path` both count.
    if rules.forbidden:
        hit = next(
            (
                p
                for a in args
                for p in (a, a.partition("=")[2])
                if p and matches_forbidden(p, rules.forbidden)
            ),
            None,
        )
        if hit is not None:
            routes.append(Route(FORBIDDEN_READ, hit, "forbidden-path"))
    if name == "git":
        routes += classify_git(args, rules)
    elif name == "gh":
        if args != ["--version"]:
            routes.append(Route(GH, "gh", GH))
    elif name in HTTP_CLIENTS:
        if (host := find_client_host(name, args)) is not None:
            routes.append(Route(HTTP_FETCH, host, "http-client"))
    elif PIP_WORD.fullmatch(name):
        routes += classify_pip(args, rules, name)
    elif name == "pipx":
        routes += classify_pipx(args, rules)
    elif name == "uv":
        routes += classify_uv(args, rules)
    elif name == "uvx":
        routes += classify_tool_run(args, rules, "uvx")
    elif INTERPRETER.fullmatch(name):
        if PYTHON_WORD.fullmatch(name) and args[:2] == ["-m", "pip"]:
            routes += classify_pip(args[2:], rules, f"{name} -m pip")
        elif (host := find_inline_code_host(args) or find_remote_host(command.stdin_code)) is not None:
            routes.append(Route(HTTP_FETCH, host, "script-url"))
    return routes


def classify_command(command: str, rules: Rules) -> list[str]:
    """Return the route kinds of a shell command, each once, in the order they are found."""
    return list(dict.fromkeys(r.kind for c in walk_commands(command) for r in classify_segment(c, rules)))


def string_leaves(value: Any) -> Iterator[str]:
    """Yield every string inside a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from string_leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from string_leaves(v)


def find_routes(tool: str, tool_input: dict[str, Any], rules: Rules) -> list[Route]:
    """Return the routes to the reference fix that one tool call tries, judged from the call alone.

    A Bash route's evidence is the whole command, one route per kind.
    """
    if tool in WEB_TOOLS:
        return [Route(WEB_TOOL, " ".join(string_leaves(tool_input)), "web-tool")]
    if tool.startswith("mcp__browser__"):
        url = " ".join(string_leaves(tool_input))
        return (
            [Route(BROWSER, url, "browser-mcp")] if "navigate" in tool.lower() and has_remote_url(url) else []
        )
    if tool in FILE_TOOLS:
        paths = [str(v) for k, v in tool_input.items() if k in ("file_path", "path", "pattern", "glob")]
        hit = next((p for p in paths if matches_forbidden(p, rules.forbidden)), None)
        return [Route(FORBIDDEN_READ, hit, "forbidden-path")] if hit is not None else []
    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str):
            first: dict[str, Route] = {}
            for c in walk_commands(command):
                for route in classify_segment(c, rules):
                    first.setdefault(route.kind, Route(route.kind, command, route.rule))
            return list(first.values())
    return []
