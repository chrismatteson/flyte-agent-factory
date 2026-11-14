"""A documented command that runs the package directly says `PYTHONPATH=src` on it.

`pyproject.toml` keeps `package = false`, so `uv run python -m flyte_agent_factory...` and a `python` heredoc
or `-c` importing the package fail with ModuleNotFoundError without it. (The ops scripts find `src`
themselves: `test_ops_scripts_run_bare.py`.) A `flyte run` or `flyte deploy` of a file under `src/` finds the
package through `--root-dir src` instead, which the CLI appends to `sys.path` (without it both fail the same
way, #223); the docs put `PYTHONPATH=src` on those too, so every command that runs the package reads alike.
A description of what the factory runs internally names the module instead of a command.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: A `flyte run` or `flyte deploy` of a task file under `src/`, which also needs `--root-dir src`.
_FLYTE_FILE = re.compile(r"\bflyte\b.*\b(run|deploy)\b.*\bsrc/\S+\.py\b")
#: A command that needs the package importable: `-m flyte_agent_factory...`, a `_FLYTE_FILE`, or `python -c`
#: with an import of it.
_COMMAND = re.compile(
    "|".join(
        [r"-m\s+flyte_agent_factory\b", _FLYTE_FILE.pattern, r"\bpython3?\b.*\s-c\b.*\bflyte_agent_factory\b"]
    )
)
_HEREDOC = re.compile(r"\bpython3?\b.*<<-?\s*['\"]?(\w+)")
_EXPORT = re.compile(r"^\s*export\s+PYTHONPATH=src\b")
_FENCE = re.compile(r"^\s*(```|~~~)\s*(\w*)")
#: Fenced blocks that are code or data, not commands to type.
_NOT_SHELL = {"python", "py", "json", "toml", "yaml", "yml", "html", "js", "ts"}


def _documents() -> list[Path]:
    globs = ("docs/**/*.md", "skills/agent-factory/**/*.md", "ops/**/*.md")
    found = [p for pattern in globs for p in ROOT.glob(pattern)]
    found += [ROOT / name for name in ("README.md", "AGENTS.md", "profiles/README.md")]
    return sorted(set(found))


def _logical(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Join backslash continuations, so a command split over lines is checked as one."""
    out: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for number, line in lines:
        if pending is not None:
            pending = (pending[0], pending[1] + " " + line.strip())
        else:
            pending = (number, line)
        if not pending[1].rstrip().endswith("\\"):
            out.append(pending)
            pending = None
    if pending is not None:
        out.append(pending)
    return out


def _command_offences(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Return the command lines of one shell block (or docstring) that run the package without PYTHONPATH."""
    offences = []
    exported = False
    logical = _logical(lines)
    for i, (number, line) in enumerate(logical):
        exported = exported or bool(_EXPORT.match(line))
        needs = bool(_COMMAND.search(line))
        if heredoc := _HEREDOC.search(line):
            body = []
            for _, rest in logical[i + 1 :]:
                if rest.strip() == heredoc.group(1):
                    break
                body.append(rest)
            needs = needs or any("flyte_agent_factory" in rest for rest in body)
        if (needs and not exported and "PYTHONPATH=src" not in line) or _lacks_root_dir(line):
            offences.append((number, line.strip()))
    return offences


def _lacks_root_dir(command: str) -> bool:
    """Say whether `command` runs a task file with flyte but without `--root-dir src`."""
    return bool(_FLYTE_FILE.search(command)) and not re.search(r"--root-dir[ =]src\b", command)


def markdown_offences(text: str) -> list[tuple[int, str]]:
    """Return `(line, command)` for each command in `text` (markdown) that lacks PYTHONPATH=src."""
    offences: list[tuple[int, str]] = []
    block: list[tuple[int, str]] | None = None
    shell = False
    prose: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        fence = _FENCE.match(line)
        if fence and block is None:
            block, shell = [], fence.group(2).lower() not in _NOT_SHELL
            prose.append("")
            continue
        if fence and block is not None:
            if shell:
                offences += _command_offences(block)
            block = None
            prose.append("")
            continue
        if block is not None:
            block.append((number, line))
            prose.append("")
        else:
            prose.append(line)
    # Inline code spans in prose, which may wrap onto the next line.
    joined = "\n".join(prose)
    for span in re.finditer(r"`([^`]+)`", joined):
        code = span.group(1)
        flat = code.replace("\n", " ")
        if (_COMMAND.search(flat) and "PYTHONPATH=src" not in code) or _lacks_root_dir(flat):
            offences.append((joined.count("\n", 0, span.start()) + 1, code.replace("\n", " ")))
    return offences


def docstring_offences(source: str) -> list[tuple[int, str]]:
    """Return the commands in a module docstring that lack PYTHONPATH=src (line numbers within it)."""
    doc = ast.get_docstring(ast.parse(source), clean=False) or ""
    return _command_offences(list(enumerate(doc.splitlines(), 1)))


def test_documented_package_commands_carry_pythonpath():
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {command}"
        for path in _documents()
        for number, command in markdown_offences(path.read_text())
    ]
    offenders += [
        f"{path.relative_to(ROOT)} (docstring line {number}): {command}"
        for path in sorted(ROOT.glob("ops/**/*.py"))
        for number, command in docstring_offences(path.read_text())
    ]
    assert not offenders, "add PYTHONPATH=src (and --root-dir src on flyte) to these commands:\n" + "\n".join(
        offenders
    )


def test_the_scan_finds_each_kind_of_offence():
    text = "\n".join(
        [
            "Run `python -m flyte_agent_factory._grants seal`.",  # 1: an inline span
            "Or `PYTHONPATH=src uv run python -m flyte_agent_factory._grants seal`.",
            "A span that wraps: `uv run python -m",
            "flyte_agent_factory._gates standards .`",  # 3: reported where it starts
            "```bash",
            "uv run python -m flyte_agent_factory._judgement replay x.json",  # 6
            "flyte deploy --all --root-dir src \\",  # 7: continued onto the next line
            "  src/flyte_agent_factory/agent.py",
            "uv run python - <<'PY'",  # 9: a heredoc importing the package
            "from flyte_agent_factory import _profile",
            "PY",
            "uv run python -c 'import flyte_agent_factory'",  # 12
            "uv run python ops/deploy_work.py",  # an ops script finds src itself
            "uv run flyte run --root-dir src -p p -d d src/flyte_agent_factory/agent.py project",  # 14
            "PYTHONPATH=src uv run flyte run -p p -d d src/sandbox_probe.py probe",  # 15: no --root-dir
            "PYTHONPATH=src uv run flyte --config c run --root-dir src src/sandbox_probe.py probe",
            "# or: flyte run ... src/flyte_agent_factory/agent.py gcp_bootstrap",  # 17: even as a comment
            "```",
            "```bash",
            "export PYTHONPATH=src",
            "uv run python -m flyte_agent_factory._judgement replay x.json",
            "uv run flyte run src/sandbox_probe.py probe",  # 22: exported, but still no --root-dir
            "```",
            "```python",
            "from flyte_agent_factory.child import attach",
            "```",
        ]
    )
    assert [number for number, _ in markdown_offences(text)] == [6, 7, 9, 12, 14, 15, 17, 22, 1, 3]
    wrapped = "From a shell: `flyte run --root-dir src -p x\nsrc/flyte_agent_factory/agent.py t`"
    assert markdown_offences(wrapped)  # an inline span that wraps is still a flyte run
    doc = (
        '"""Probe.\n\n'
        "    uv run python -m flyte_agent_factory._x\n"
        "    PYTHONPATH=src uv run python -m flyte_agent_factory._x\n"
        '"""\n'
    )
    assert [number for number, _ in docstring_offences(doc)] == [3]
