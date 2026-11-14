"""What each review judge was shown, recorded in the round's verdict row, and how to recreate it.

A judge reads a snapshot of the workspace and a prompt; its verdict is only explainable against both. Each
round records, per judge, the commit it reviewed, the project's base, a hash of the canonical diff between
them, a hash of the prompt and the engine, model and effort that answered. The diff itself is not stored:
`recreate_diff` runs the recorded command in a clone and checks the hash. See docs/judge-inputs.md.

Standard library only, and nothing runs on import: `python -m flyte_agent_factory._judge_inputs recreate`
is the reader's tool, and the Flyte bundler imports this module.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The canonical diff's identity. Change the argv below only with a new version: old rows name the one
#: they were hashed with, and `recreate_diff` refuses an argv that is not the one its spec names.
DIFF_SPEC = "git-diff/1"
#: Placeholders in the recorded argv, for the two commits.
BASE, HEAD = "{base}", "{head}"
#: The argv, with the user's and the repository's git config pinned. `GIT_NO_DRIVERS` (`_secret_env`) is
#: `--no-textconv --no-ext-diff`; it is spelled out here because this module must not import the engine.
#: Config that changes diff text is pinned with `-c` (a repository's own config beats nothing else);
#: `core.fsmonitor` and `core.hooksPath` are `_secret_env.GIT_SAFE_CONFIG`, so no configured program runs.
DIFF_ARGV: tuple[str, ...] = (
    "git",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.quotepath=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "color.ui=never",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.renames=false",
    "-c", "diff.algorithm=myers",
    "-c", "diff.indentHeuristic=true",
    "-c", "diff.context=3",
    "-c", "diff.interHunkContext=0",
    "-c", "diff.submodule=short",
    "-c", "diff.ignoreSubmodules=none",
    "-c", "diff.orderFile=/dev/null",
    "diff",
    "--binary",
    "--full-index",
    "--no-color",
    "--no-renames",
    "--no-ext-diff",
    "--no-textconv",
    BASE,
    HEAD,
    "--",
)  # fmt: skip
#: The argv each spec version stands for.
_SPECS: dict[str, tuple[str, ...]] = {DIFF_SPEC: DIFF_ARGV}

#: The working tree against HEAD, one NUL-terminated record per path; empty output is a clean tree.
_STATUS_ARGV = (
    "git",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "status",
    "--porcelain",
    "-z",
    "--untracked-files=all",
)
_DIFF_TIMEOUT_S = 120.0
_STATUS_TIMEOUT_S = 60.0
_CHUNK = 1 << 16
_ERROR_CHARS = 200
_PROVIDERS = {"claude": "anthropic", "codex": "openai"}
#: Environment git gets: no user or system config (so `diff.external`, `color.ui=always` and the like in a
#: user's own files cannot change the bytes), no pager, no prompt, and no secrets from this process.
_PASS_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")


class RecreateError(Exception):
    """The recorded diff could not be recreated."""


class CommitsUnreachable(RecreateError):
    """A commit the row names is not in the repository (a squash, a rebase, a clone of another repo)."""


class DiffMismatch(RecreateError):
    """The recreated diff does not hash to what was recorded."""


def resolve_provider(engine: str) -> str:
    """Return the model provider behind an engine name, or "" for one this does not know."""
    return _PROVIDERS.get(engine, "")


def _git_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASS_ENV if k in os.environ}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
        GIT_PAGER="cat",
    )
    return env


def _stream(
    argv: list[str],
    cwd: Path,
    timeout: float,
    on_chunk: Callable[[bytes], object],
    env: Mapping[str, str] | None = None,
) -> str:
    """Run `argv` in `cwd`, handing its stdout to `on_chunk` as it arrives; returns "" or a short error.

    Blocking, so callers on the event loop use `asyncio.to_thread`. The output is never held whole here:
    a diff of a large change is the point. stderr goes to a temporary file, not a pipe that could fill
    while stdout is being read.
    """
    try:
        with tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=dict(env) if env is not None else _git_env(),
                start_new_session=True,
            )
            timed_out = threading.Event()

            def expire() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(timeout, expire)
            timer.start()
            try:
                assert proc.stdout is not None
                while chunk := proc.stdout.read(_CHUNK):
                    on_chunk(chunk)
                code = proc.wait()
            finally:
                timer.cancel()
                proc.kill()  # a no-op after a normal exit; a reader that raised must not leave git running
                proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()
            if timed_out.is_set():
                return f"timed out after {timeout:.0f}s"
            if code:
                stderr.seek(0)
                first = stderr.read(2000).decode(errors="replace").strip().splitlines()
                return f"git exited {code}: {first[0][:_ERROR_CHARS] if first else 'no message'}"
    except OSError as e:
        return f"{type(e).__name__}: {e}"[:_ERROR_CHARS]
    return ""


def _argv(template: tuple[str, ...], base: str, head: str) -> list[str]:
    return [a.replace(BASE, base).replace(HEAD, head) for a in template]


def _git_common_dir(repo: Path) -> tuple[Path | None, str]:
    """Return (the git directory holding `repo`'s objects, "") or (None, a short error) for no repository."""
    found: list[bytes] = []
    if error := _stream(["git", "rev-parse", "--git-common-dir"], repo, _STATUS_TIMEOUT_S, found.append):
        return None, error
    return repo / b"".join(found).decode().strip(), ""


def _isolated_diff(repo: Path, argv: list[str], on_chunk: Callable[[bytes], object]) -> str:
    """Run a diff `argv` against `repo`'s objects alone; returns "" or a short error.

    `git diff A B` in a clone reads `.gitattributes` from the *working tree* and `info/attributes` from the
    clone, so the same two commits can diff differently in another checkout (a `binary` or `-diff` line
    turns text hunks into a binary patch). So the diff runs in an empty bare repository that borrows
    `repo`'s object store as an alternate: no working tree, no index, no attributes and no repository
    config, only the two commits. That is part of what `git-diff/1` means.
    """
    common, error = _git_common_dir(repo)
    if common is None:
        return error
    objects = (common / "objects").resolve()
    with tempfile.TemporaryDirectory(prefix="judge-diff-") as scratch:
        bare = Path(scratch) / "view.git"
        if error := _stream(
            ["git", "init", "-q", "--bare", str(bare)], Path(scratch), _STATUS_TIMEOUT_S, lambda _: None
        ):
            return error
        (bare / "objects" / "info" / "alternates").write_text(f"{objects}\n")
        (bare / "info").mkdir(exist_ok=True)
        (bare / "info" / "attributes").write_text("")
        env = {**_git_env(), "GIT_DIR": str(bare)}
        return _stream(argv, Path(scratch), _DIFF_TIMEOUT_S, on_chunk, env=env)


def _diff_sha256_sync(repo: Path, base: str, head: str) -> tuple[str | None, str]:
    if not base or not head:
        return None, "no base commit" if not base else "no head commit"
    digest = hashlib.sha256()
    error = _isolated_diff(repo, _argv(DIFF_ARGV, base, head), digest.update)
    return (None, error) if error else (digest.hexdigest(), "")


async def _diff_sha256(repo: Path, base: str, head: str) -> tuple[str | None, str]:
    """Return (sha256 of the canonical diff, "") or (None, a short error), without blocking the loop."""
    return await asyncio.to_thread(_diff_sha256_sync, repo, base, head)


def _worktree_clean_sync(repo: Path) -> bool | None:
    seen: list[bytes] = []
    if _stream(list(_STATUS_ARGV), repo, _STATUS_TIMEOUT_S, seen.append):
        return None
    return not seen


async def capture_round_inputs(workspace: Path, base: str, head: str, round_base: str) -> dict[str, Any]:
    """Return what every judge of this round shares: the commits, the diff hashes, the tree's state.

    Never raises: a git that fails (an unknown base, a missing repository) is a None and a short error,
    because recording must not decide whether a review runs. `diff_error` is present only on a failure.
    `round_diff_sha256` is the diff since the previous round was judged; None with no `round_base`.
    """
    inputs: dict[str, Any] = {
        "head": head,
        "base": base,
        "diff_spec": DIFF_SPEC,
        "diff_argv": list(DIFF_ARGV),
        "diff_sha256": None,
        "round_base": round_base,
        "round_diff_sha256": None,
        "worktree_clean": None,
    }
    try:
        inputs["diff_sha256"], error = await _diff_sha256(workspace, base, head)
        if error:
            inputs["diff_error"] = error
        if round_base:
            inputs["round_diff_sha256"], error = (
                (inputs["diff_sha256"], "")
                if round_base == base
                else await _diff_sha256(workspace, round_base, head)
            )
            if error:
                inputs["round_diff_error"] = error
        inputs["worktree_clean"] = await asyncio.to_thread(_worktree_clean_sync, workspace)
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as e:  # git, the scratch repo, undecodable output
        # Named after the hash it left null; a failure of `git status` alone leaves `worktree_clean` null.
        error = f"{type(e).__name__}: {e}"[:_ERROR_CHARS]
        if inputs["diff_sha256"] is None:
            inputs.setdefault("diff_error", error)
        elif round_base and inputs["round_diff_sha256"] is None:
            inputs.setdefault("round_diff_error", error)
    return inputs


def hash_prompt(prompt: str) -> str:
    """Return the sha256 of the brief string handed to a judge's call (the varying part of its prompt)."""
    return hashlib.sha256(prompt.encode()).hexdigest()


def build_judge_entry(
    round_inputs: Mapping[str, Any],
    *,
    engine: str,
    model: str,
    effort: str,
    prompt_sha256: str,
    outcome: str,
    attempts: int,
    carried: bool,
) -> dict[str, Any]:
    """Return one judge's record: the round's shared inputs plus where and how this judge answered."""
    return {
        **round_inputs,
        "engine": engine,
        "model": model,
        "effort": effort,
        "provider": resolve_provider(engine),
        "prompt_sha256": prompt_sha256,
        "outcome": outcome,
        "attempts": attempts,
        "carried": carried,
    }


@dataclass(frozen=True)
class JudgeInputs:
    """One judge's recorded inputs, as read back. A field a row lacks or mistypes is its empty value."""

    head: str = ""
    base: str = ""
    diff_spec: str = ""
    diff_argv: tuple[str, ...] = ()
    diff_sha256: str | None = None
    diff_error: str = ""
    round_base: str = ""
    round_diff_sha256: str | None = None
    worktree_clean: bool | None = None
    engine: str = ""
    model: str = ""
    effort: str = ""
    provider: str = ""
    prompt_sha256: str = ""
    outcome: str = ""
    attempts: int = 1
    carried: bool = False


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse(entry: Mapping[str, Any]) -> JudgeInputs:
    argv = entry.get("diff_argv")
    clean, attempts = entry.get("worktree_clean"), entry.get("attempts")
    return JudgeInputs(
        head=_text(entry.get("head")),
        base=_text(entry.get("base")),
        diff_spec=_text(entry.get("diff_spec")),
        diff_argv=tuple(argv) if isinstance(argv, list) and all(isinstance(a, str) for a in argv) else (),
        diff_sha256=_text_or_none(entry.get("diff_sha256")),
        diff_error=_text(entry.get("diff_error")),
        round_base=_text(entry.get("round_base")),
        round_diff_sha256=_text_or_none(entry.get("round_diff_sha256")),
        worktree_clean=clean if isinstance(clean, bool) else None,
        engine=_text(entry.get("engine")),
        model=_text(entry.get("model")),
        effort=_text(entry.get("effort")),
        provider=_text(entry.get("provider")),
        prompt_sha256=_text(entry.get("prompt_sha256")),
        outcome=_text(entry.get("outcome")),
        attempts=attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 1,
        carried=entry.get("carried") is True,
    )


def judge_inputs_of(row: Mapping[str, Any]) -> dict[str, JudgeInputs]:
    """Return judge -> `JudgeInputs` for a round row; {} for a row from before this was recorded.

    Never raises on an old or malformed row: this reads files written by every earlier version.
    """
    judges = row.get("judges") if isinstance(row, Mapping) else None
    if not isinstance(judges, Mapping):
        return {}
    return {str(name): _parse(entry) for name, entry in judges.items() if isinstance(entry, Mapping)}


def _commit_exists(repo: Path, sha: str) -> bool:
    return not sha.startswith("-") and not _stream(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo, _STATUS_TIMEOUT_S, lambda _: None
    )


def recreate_diff(repo: Path, entry: JudgeInputs | Mapping[str, Any], which: str = "project") -> bytes:
    """Re-run a recorded diff in `repo` and return its bytes, verified against the recorded hash.

    `which` is "project" (base to head) or "round" (what that round changed). Raises `CommitsUnreachable`
    when either commit is missing from `repo`, `DiffMismatch` when the bytes hash differently, and
    `RecreateError` for anything else (no hash recorded, git failing).

    The argv is the one the entry's `diff_spec` stands for, and the entry's own copy must equal it: a
    report is a file someone else may have written, and its argv would run as a command.
    """
    inputs = entry if isinstance(entry, JudgeInputs) else _parse(entry)
    if which not in ("project", "round"):
        raise ValueError(f"which must be 'project' or 'round', not {which!r}")
    base, want = (
        (inputs.base, inputs.diff_sha256)
        if which == "project"
        else (inputs.round_base, inputs.round_diff_sha256)
    )
    if not want:
        raise RecreateError(
            f"no {which} diff hash was recorded" + (f" ({inputs.diff_error})" if inputs.diff_error else "")
        )
    template = _SPECS.get(inputs.diff_spec)
    if template is None:
        raise RecreateError(f"unknown diff spec {inputs.diff_spec!r}")
    if inputs.diff_argv != template:
        raise RecreateError(f"the recorded argv is not the one {inputs.diff_spec} stands for")
    if (probe := _git_common_dir(repo))[0] is None:
        raise RecreateError(f"{repo} is not a git repository: {probe[1]}")
    missing = [sha for sha in (base, inputs.head) if not _commit_exists(repo, sha)]
    if missing:
        raise CommitsUnreachable(f"commit not in {repo}: {', '.join(missing)}")
    chunks: list[bytes] = []
    error = _isolated_diff(repo, _argv(template, base, inputs.head), chunks.append)
    if error:
        raise RecreateError(error)
    data = b"".join(chunks)
    if (got := hashlib.sha256(data).hexdigest()) != want:
        raise DiffMismatch(f"the recreated {which} diff hashes to {got}, the record says {want}")
    return data


# --- command line ---

_ROW_PATHS = (("review", "verdicts"), ("metrics", "rounds"), ("verdicts",), ("rounds",))
EXIT_BAD_INPUT, EXIT_UNREACHABLE, EXIT_MISMATCH = 2, 3, 4


def _round_rows(report: Any) -> list[Mapping[str, Any]]:
    """Every round row in a snapshot or metrics file, wherever it keeps them."""
    found: list[Mapping[str, Any]] = []
    for path in _ROW_PATHS:
        node: Any = report
        for key in path:
            node = node.get(key) if isinstance(node, Mapping) else None
        if isinstance(node, list):
            found += [row for row in node if isinstance(row, Mapping)]
    return found


def _recreate_command(args: argparse.Namespace) -> int:
    try:
        report = json.loads(Path(args.report).read_text())
    except (OSError, ValueError) as e:
        return _fail(EXIT_BAD_INPUT, f"cannot read {args.report}: {type(e).__name__}")
    rows = [row for row in _round_rows(report) if row.get("round") == args.round]
    if not rows:
        return _fail(EXIT_BAD_INPUT, f"round {args.round} not found in {args.report}")
    judges = next((j for row in rows if (j := judge_inputs_of(row))), {})
    if not judges:
        return _fail(EXIT_BAD_INPUT, f"round {args.round} has no recorded judge inputs (an older run)")
    if args.judge not in judges:
        return _fail(
            EXIT_BAD_INPUT, f"no judge {args.judge!r} in round {args.round}: {', '.join(sorted(judges))}"
        )
    try:
        data = recreate_diff(Path(args.repo), judges[args.judge], which=args.which)
    except CommitsUnreachable as e:
        return _fail(EXIT_UNREACHABLE, str(e))
    except DiffMismatch as e:
        return _fail(EXIT_MISMATCH, str(e))
    except RecreateError as e:
        return _fail(EXIT_BAD_INPUT, str(e))
    if args.out:
        try:
            Path(args.out).write_bytes(data)
        except OSError as e:
            return _fail(EXIT_BAD_INPUT, f"cannot write {args.out}: {type(e).__name__}: {e}")
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    return 0


def _fail(code: int, message: str) -> int:
    print(f"error: {' '.join(message.split())}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory._judge_inputs", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recreate = commands.add_parser("recreate", help="recreate the diff a judge was shown and verify its hash")
    recreate.add_argument("--report", required=True, help="a run snapshot or metrics JSON file")
    recreate.add_argument("--round", required=True, type=int)
    recreate.add_argument("--judge", required=True, help="correctness, completeness or craft")
    recreate.add_argument(
        "--repo", default=".", help="a clone holding both commits (default: the current directory)"
    )
    recreate.add_argument("--out", help="write the diff here instead of to stdout")
    recreate.add_argument("--which", choices=("project", "round"), default="project")
    recreate.set_defaults(run=_recreate_command)
    args = parser.parse_args(argv)
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
