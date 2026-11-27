"""The standards gate's per-file tools and `forbid` rules judge only what the project changed upstream."""

import hashlib
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_rules,
    _gate_tests,
    _gates,
    _module_size,
    _target_run,
    _vendored,
)
from tests import _standards as template

pytestmark = pytest.mark.real_factory_gates

BAD_SH = "#!/bin/sh\ngit tag $1\n"  # SC2086
CONFIGS = template.TEMPLATE / "languages/configs"
SHELL = {"languages/shell.md": template.FILES["languages/shell.md"]}
TREE = "copy"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)


def _self_made_checkout(root: Path, files: dict[str, str]) -> None:
    """A repository made in place: `files` committed on a local `main`, no remote, so no known base."""
    _write(root, files)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")


def _upstream_of(root: Path) -> Path:
    """Where a checkout's upstream repository lives: beside the project, outside everything the gate walks."""
    return root.parents[1] / "upstreams" / root.name


def _checkout(root: Path, files: dict[str, str]) -> None:
    """A clone of an upstream repository at its base: `files` committed on its `main`, cloned into `root`."""
    source = _upstream_of(root)
    _self_made_checkout(source, files)
    root.parent.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "clone", "-q", str(source), str(root))


def _fetched_checkout(
    root: Path, files: dict[str, str], *, remote: bool, local_branch: bool, depth: int | None = 1
) -> str:
    """The benchmark layout: a sha fetched (shallow by default) into a fresh repository; returns the base."""
    source = _upstream_of(root)
    _self_made_checkout(source, files)
    base = _git(source, "rev-parse", "HEAD")
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    depth_args = ["--depth", str(depth)] if depth else []
    if remote:  # the launch docs: `remote add origin` + `fetch --depth 1 origin <sha>`, no tracking branch
        _git(root, "remote", "add", "origin", f"file://{source}")
        _git(root, "fetch", "-q", *depth_args, "origin", base)
    else:  # the sphinx setup.sh: fetch by URL, no remote
        _git(root, "fetch", "-q", *depth_args, f"file://{source}", base)
    _git(root, "checkout", "-q", "FETCH_HEAD")
    if local_branch:
        _git(root, "branch", "master", base)
    return base


def _run(project: Path, rule: str, name: str = "engineering.md") -> _gates.Result:
    results = _gates.standards_results(project, {name: rule}, configs=CONFIGS)
    [result] = [r for r in results if r.rule == "X-1"]
    return result


def _command(rule_line: str, ident: str = "X-1") -> str:
    return f"## {ident} MUST\n{rule_line}\n"


def _owned_sh(project: Path) -> None:
    _write(project, {"checks.sh": "#!/bin/sh\necho ok\n", "evidence/run.sh": "#!/bin/sh\necho ok\n"})


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    _owned_sh(root)
    return root


# -- shellcheck's file list --


def test_an_unchanged_nested_checkout_script_is_not_linted(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert "repo/utils/bump.sh" not in result.output.splitlines()[0]
    assert "checks.sh" in result.output.splitlines()[0]


def test_a_modified_nested_checkout_script_is_linted_and_fails(project):
    # An unchanged script with the same finding beside it: only the changed one is the project's.
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH, "utils/other.sh": BAD_SH})
    (project / "repo/utils/bump.sh").write_text(BAD_SH + "git tag $2\n")
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert "repo/utils/bump.sh" in first
    assert "repo/utils/other.sh" not in first
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "SC2086" in result.output
    assert "repo/utils/other.sh" not in result.output
    assert "Nested git checkouts judged against their base: repo (base " in result.output


def test_a_new_script_in_a_nested_checkout_is_linted(project):
    _checkout(project / "repo", {"utils/ok.sh": "#!/bin/sh\necho ok\n", "utils/old.sh": BAD_SH})
    _write(project / "repo", {"utils/new.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert "repo/utils/new.sh" in first
    assert "repo/utils/old.sh" not in first
    assert result.status is _gate_base.Status.FAIL


def test_a_project_owned_script_is_always_linted(project):
    _checkout(project / "repo", {"utils/ok.sh": "#!/bin/sh\necho ok\n", "utils/bump.sh": BAD_SH})
    _write(project, {"scripts/mine.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert "scripts/mine.sh" in first
    assert "repo/utils/bump.sh" not in first
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/utils/bump.sh" not in result.output


def test_a_nested_checkout_with_no_known_base_is_linted_as_the_projects(project):
    _self_made_checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert "repo/utils/bump.sh" in result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo: base unknown, so its files are judged as the project's" in result.output


def test_the_output_names_the_nested_checkout_judged_against_its_base(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    base = _git(project / "repo", "rev-parse", "HEAD")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert f"Nested git checkouts judged against their base: repo (base {base[:12]})." in result.output


# -- a vendored.toml tree (a copy without .git) --


@pytest.fixture
def copy(tmp_path, monkeypatch):
    """A project holding a copy of a local upstream repository, declared in `vendored.toml`."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(_vendored, "_ALLOWED_SCHEMES", ("https", "file"))
    monkeypatch.setattr(_vendored, "_UNAVAILABLE", {})
    upstream = tmp_path / "upstream"
    _self_made_checkout(
        upstream, {"a.sh": BAD_SH, "b.sh": BAD_SH, "app.js": "var a = 1\n", "lib/x.py": "import os\n"}
    )
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    project = tmp_path / "proj"
    shutil.copytree(upstream, project / TREE, ignore=shutil.ignore_patterns(".git"))
    _owned_sh(project)
    commit = _git(upstream, "rev-parse", "HEAD")
    (project / "vendored.toml").write_text(
        f'[[tree]]\npath = "{TREE}"\nrepo = "{upstream.as_uri()}"\ncommit = "{commit}"\n'
    )
    return project


def test_an_identical_vendored_script_is_skipped_and_a_changed_one_is_linted(copy):
    (copy / TREE / "b.sh").write_text(BAD_SH + "git tag $2\n")
    result = _run(copy, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert f"{TREE}/b.sh" in first and f"{TREE}/a.sh" not in first
    assert result.status is _gate_base.Status.FAIL
    assert f"{TREE}/a.sh" not in result.output.split("\n", 1)[1].split("SC2086")[0]


def test_an_all_identical_vendored_tree_passes_and_is_named(copy):
    result = _run(copy, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert f"Files identical to upstream are not the project's (vendored.toml): {TREE} (" in result.output


# -- the benchmark layout, end to end --


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def test_an_untouched_checkout_does_not_fail_the_shell_pack(tmp_path):
    project = tmp_path / "proj"
    _write(project, {"checks.sh": "#!/bin/sh\necho ok\n", "setup.sh": "#!/bin/sh\necho ok\n"})
    _write(project, {"evidence/run.sh": "#!/bin/sh\necho ok\n"})
    base = _fetched_checkout(
        project / "repo", {"utils/bump_docker.sh": BAD_SH}, remote=False, local_branch=True
    )
    before = _digest(project)
    results = {r.rule: r for r in _gates.standards_results(project, SHELL, configs=CONFIGS)}
    sh002 = results[template.find(kind="command", arg="shellcheck", file="languages/shell.md")]
    assert sh002.status is _gate_base.Status.PASS, sh002.output
    assert f"Nested git checkouts judged against their base: repo (base {base[:12]})" in sh002.output
    assert _digest(project) == before


def test_a_checkout_fetched_by_sha_with_a_remote_but_no_tracking_branch_passes(project):
    base = _fetched_checkout(
        project / "repo", {"utils/bump_docker.sh": BAD_SH}, remote=True, local_branch=False
    )
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert f"repo (base {base[:12]})" in result.output


def test_a_fix_committed_on_top_of_the_fetched_base_is_linted(project):
    repo = project / "repo"
    files = {"utils/bump_docker.sh": "#!/bin/sh\necho ok\n", "utils/old.sh": BAD_SH}
    base = _fetched_checkout(repo, files, remote=False, local_branch=True)
    _git(repo, "checkout", "-q", "master")
    _write(repo, {"utils/bump_docker.sh": BAD_SH})
    _git(repo, "commit", "-qam", "fix")
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/utils/bump_docker.sh" in first
    assert "repo/utils/old.sh" not in first
    assert f"repo (base {base[:12]})" in result.output
    assert NO_BASE not in result.output


def test_a_full_fetch_of_the_base_with_a_local_branch_is_judged_against_it(project):
    files = {"utils/old.sh": BAD_SH}
    base = _fetched_checkout(project / "repo", files, remote=False, local_branch=True, depth=None)
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert f"repo (base {base[:12]})" in result.output
    assert NO_BASE not in result.output


def test_a_script_committed_on_top_of_a_full_fetch_is_linted(project):
    repo = project / "repo"
    base = _fetched_checkout(repo, {"utils/old.sh": BAD_SH}, remote=False, local_branch=True, depth=None)
    _git(repo, "checkout", "-q", "master")
    _write(repo, {"utils/mine.sh": BAD_SH})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "mine")
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/utils/mine.sh" in first
    assert "repo/utils/old.sh" not in first
    assert f"repo (base {base[:12]})" in result.output


def test_a_deeper_shallow_fetch_takes_the_fetched_commit_as_base_not_the_boundary(project):
    repo = project / "repo"
    source = _upstream_of(repo)
    _self_made_checkout(source, {"utils/ok.sh": "#!/bin/sh\necho ok\n"})
    for name in ("new1.sh", "new2.sh"):  # two more upstream commits; the base is the last
        _write(source, {f"utils/{name}": BAD_SH})
        _git(source, "add", "-A")
        _git(source, "commit", "-qm", name)
    base = _git(source, "rev-parse", "HEAD")
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "fetch", "-q", "--depth", "3", f"file://{source}", base)
    _git(repo, "checkout", "-q", "FETCH_HEAD")
    _git(repo, "branch", "master", base)
    assert len(_target_run.read_shallow_boundaries(repo)) == 1  # the boundary is there and is not the base
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert result.status is _gate_base.Status.PASS, result.output
    assert "new1.sh" not in first and "new2.sh" not in first
    assert f"repo (base {base[:12]})" in result.output


@pytest.mark.parametrize("layout", ["fetched", "fetched-with-remote", "self-made", "clone-without-remote"])
def test_nested_checkouts_take_the_fetched_commit_as_base(project, layout):
    repo = project / "repo"
    files = {"a.txt": "a\n"}
    base = ""
    if layout == "fetched":
        base = _fetched_checkout(repo, files, remote=False, local_branch=True)
    elif layout == "fetched-with-remote":
        base = _fetched_checkout(repo, files, remote=True, local_branch=False)
    elif layout == "self-made":
        _self_made_checkout(repo, files)
    else:
        _checkout(repo, files)
        _git(repo, "remote", "remove", "origin")
    [nested] = _target_run.nested_checkouts(project, _gate_base._SKIP_DIRS)
    if base:
        assert (nested.base, nested.changed) == (base, set())
    else:
        assert nested.changed is None


def test_module_size_skips_an_unchanged_over_limit_module_in_a_fetched_checkout(project):
    big = "x = 1\n" * (_module_size.MODULE_LINE_LIMIT + 100)
    _fetched_checkout(project / "repo", {"sphinx/cpp.py": big}, remote=False, local_branch=True)
    code, output = _module_size.check(project, None, _gate_tests._is_test)
    assert code == 0, output


# -- forbid rules --

FORBID = _command("Enforced: forbid TODO in **/*.txt")


def test_a_pattern_only_in_an_unchanged_checkout_file_passes(project):
    _checkout(project / "repo", {"notes.txt": "TODO upstream\n"})
    result = _run(project, FORBID)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "Nested git checkouts judged against their base: repo (base " in result.output


def test_the_pattern_in_a_changed_checkout_file_or_a_project_file_fails(project):
    _checkout(project / "repo", {"notes.txt": "TODO upstream\n"})
    (project / "repo/notes.txt").write_text("TODO upstream\nTODO mine\n")
    result = _run(project, FORBID)
    assert result.status is _gate_base.Status.FAIL
    assert "repo/notes.txt:1" in result.output
    (project / "repo/notes.txt").write_text("TODO upstream\n")
    _git(project / "repo", "checkout", "-q", "--", ".")
    assert _run(project, FORBID).status is _gate_base.Status.PASS
    _write(project, {"mine.txt": "TODO\n"})
    result = _run(project, FORBID)
    assert result.status is _gate_base.Status.FAIL and "mine.txt:1" in result.output
    assert "repo/notes.txt" not in result.output.split("Nested git")[0]


# -- ruff, eslint, prettier --

RUFF_PY = "import os\n"  # F401


@pytest.fixture
def temporary(monkeypatch):
    """The text of each file a command names after `--config` or `--ignore-path`, as the tool read it."""
    texts: dict[str, str] = {}
    run = _gate_base._run

    def read_then_run(args, *rest, **kwargs):
        for flag, value in itertools.pairwise(args):
            # `os.path.isfile` is False, where `Path.is_file` raises, for an inline value too long for a name.
            if flag in ("--config", "--ignore-path") and os.path.isfile(value):
                texts[value] = Path(value).read_text()
        return run(args, *rest, **kwargs)

    monkeypatch.setattr(_gate_base, "_run", read_then_run)
    return texts


def _named_file(result: _gates.Result, texts: dict[str, str], flag: str = "--config") -> str:
    """The text of the temporary file the recorded command names last after `flag`, which is gone now."""
    command = shlex.split(result.output.splitlines()[0])
    path = command[len(command) - command[::-1].index(flag)]
    assert not Path(path).exists(), path
    return texts[path]


def _ruff_config(result: _gates.Result, texts: dict[str, str]) -> dict[str, Any]:
    return tomllib.loads(_named_file(result, texts))


def _inline_excludes(result: _gates.Result) -> list[str]:
    """The excludes the recorded ruff command gives inline, as the one `--config` override holding them."""
    command = shlex.split(result.output.splitlines()[0])
    [override] = [v for f, v in itertools.pairwise(command) if f == "--config" and v.startswith("extend-")]
    excludes: list[str] = tomllib.loads(override)["extend-exclude"]
    return excludes


def _eslint_ignores(result: _gates.Result, texts: dict[str, str]) -> list[str]:
    ignores = re.search(r"\{ ignores: (\[.*\]) \}\];\n$", _named_file(result, texts), re.DOTALL)
    assert ignores, _named_file(result, texts)
    patterns: list[str] = json.loads(ignores[1])
    return patterns


def _ruff_rules() -> list[tuple[str, str]]:
    return [
        ("check", _command("Enforced: command ruff check --select F401 .")),
        ("lint", _command("Enforced: lint ruff F401")),
        ("format", _command("Enforced: command ruff format --check .")),
    ]


@pytest.mark.parametrize("kind", ["check", "lint", "format"])
def test_ruff_leaves_an_unchanged_checkout_alone_and_judges_a_changed_file(project, kind):
    bad = "x=1\n" if kind == "format" else RUFF_PY
    rule = dict(_ruff_rules())[kind]
    _write(project, {"mine.py": "x = 1\n"})
    _checkout(project / "repo", {"pkg/a.py": bad})
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output
    command = result.output.splitlines()[0]
    after_harness = command.split("--extend-exclude .claude-flyte")[1]
    assert """--config 'extend-exclude = ["./repo"]'""" in after_harness
    # A `lint` rule still reads no configuration; the others keep the baseline they had.
    assert ("--isolated" in command) == (kind == "lint")
    assert (str(CONFIGS / "ruff.toml") in after_harness) == (kind != "lint")
    _write(project / "repo", {"pkg/a.py": bad + "# changed\n"})
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pkg/a.py" in result.output


def test_ruff_excludes_file_by_file_when_the_tree_holds_a_changed_one(project):
    _checkout(project / "repo", {"a.py": RUFF_PY, "b.py": "x = 1\n"})
    (project / "repo/b.py").write_text("x = 2\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    assert _inline_excludes(result) == ["./repo/a.py"]


def test_a_vendored_tree_is_excluded_from_ruff_too(copy):
    result = _run(copy, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    (copy / TREE / "lib/x.py").write_text("import os\nimport sys\n")
    assert _run(copy, _ruff_rules()[0][1]).status is _gate_base.Status.FAIL


ESLINT = _command("Enforced: command eslint .")


MISSING_PACKAGE_CONFIG = 'import x from "no-such-pkg-zz";\nexport default [x];\n'


@pytest.mark.parametrize("option", ["--config", "-c"])
def test_eslint_failing_on_the_config_the_rule_names_is_not_a_missing_baseline_dependency(tmp_path, option):
    # The rule's own config runs, not the baseline, so its failure is the project's.
    _write(tmp_path, {"a.js": "var a = 1;\n", "my.mjs": MISSING_PACKAGE_CONFIG})
    result = _run(tmp_path, _command(f"Enforced: command eslint {option} my.mjs ."))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "ESLint baseline dependency missing" not in result.output
    assert "Cannot find package 'no-such-pkg-zz'" in result.output


def test_a_baseline_eslint_config_missing_a_package_is_reported_as_such(tmp_path):
    project = tmp_path / "proj"
    _write(project, {"a.js": "var a = 1;\n"})
    configs = tmp_path / "configs"
    _write(configs, {"eslint.config.mjs": MISSING_PACKAGE_CONFIG})
    results = _gates.standards_results(
        project, {"engineering.md": _command("Enforced: command eslint .")}, configs=configs
    )
    [result] = [r for r in results if r.rule == "X-1"]
    assert result.status is _gate_base.Status.TOOL_UNAVAILABLE, result.output
    assert result.output.startswith("eslint . --config ")
    assert "ESLint baseline dependency missing" in result.output


def test_eslint_leaves_an_unchanged_checkout_alone(project, temporary):
    # The project's own config: the baseline one needs packages this image does not carry.
    _write(
        project,
        {"own.js": "var b = 2;\n", "eslint.config.mjs": 'export default [{ rules: { semi: "error" } }];\n'},
    )
    _checkout(project / "repo", {"lib/x.js": "var a = 1\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output
    *ignores, wrapper = _eslint_ignores(result, temporary)
    assert ignores == ["repo"] and wrapper.endswith(".mjs")
    (project / "repo/lib/x.js").write_text("var a = 2\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/lib/x.js" in result.output.split("\n", 1)[1]


PRETTIER = _command("Enforced: command prettier --check .")


def test_prettier_leaves_an_unchanged_checkout_alone(project):
    _write(project, {"own.js": "var b = 2;\n"})
    _checkout(project / "repo", {"lib/x.js": "var   a = 1\n"})
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "[warn] repo/lib/x.js" not in result.output
    (project / "repo/lib/x.js").write_text("var   a = 2\n")
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/lib/x.js" in result.output


# -- the helpers' contract --


def test_without_upstreams_every_script_is_listed(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    command = _gate_rules.with_baseline_config(["shellcheck"], project, CONFIGS, ("extra.sh",))
    assert command == ["shellcheck", "checks.sh", "evidence/run.sh", "repo/utils/bump.sh", "extra.sh"]


def test_prettier_judges_a_changed_mdx_beside_unchanged_javascript(project):
    _checkout(project / "repo", {"a.js": "const a = 1;\n", "page.mdx": "# ok\n"})
    (project / "repo/page.mdx").write_text("#   changed\n")
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/page.mdx" in result.output


def test_ruff_excludes_an_explicit_unchanged_file(project):
    _checkout(project / "repo", {"bad.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 repo/bad.py"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_ruff_still_lints_an_owned_file_named_on_the_command_line_that_its_config_excludes(tmp_path):
    # `--force-exclude` comes only with upstream excludes: without them a named project file is linted.
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[tool.ruff]\nextend-exclude = ["gen.py"]\n')
    (project / "gen.py").write_text("import os\n")
    result = _run(project, _command("Enforced: command ruff check gen.py"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "F401" in result.output


def _large_checkout(project: Path) -> None:
    """A checkout of 4,000 unchanged files with an F401 each, whose excludes outgrow the command line."""
    names = [f"flat/module_{i:04}_with_a_longer_name.py" for i in range(4000)]
    _checkout(project / "repo", {**dict.fromkeys(names, RUFF_PY), "flat/changed.py": "x = 1\n"})
    _write(project, {"repo/flat/changed.py": "y = 2\n"})


def test_ruff_lints_a_named_project_file_its_config_excludes_beside_a_named_unchanged_upstream_file(project):
    _write(project, {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["gen.py"]\n', "gen.py": RUFF_PY})
    _checkout(project / "repo", {"bad.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 gen.py repo/bad.py"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "gen.py" in result.output and "repo/bad.py" not in result.output.split("\n", 1)[1]


def test_ruff_lints_a_named_project_file_its_config_excludes_beside_an_unchanged_checkout(project):
    _write(project, {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["gen.py"]\n', "gen.py": RUFF_PY})
    _checkout(project / "repo", {"bad.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check gen.py"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "F401" in result.output


def test_ruff_drops_a_named_unchanged_upstream_directory_and_lints_nothing_else(project):
    _checkout(project / "repo", {"a.py": RUFF_PY})
    _write(project, {"mine.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 repo"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert "mine.py" not in result.output


def test_a_named_upstream_directory_is_walked_with_its_unchanged_files_excluded(project):
    _checkout(project / "repo", {"a.py": RUFF_PY})
    _write(project, {"repo/b.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 repo"))
    assert result.status is _gate_base.Status.FAIL, result.output
    found = result.output.split("\n", 1)[1]
    assert "repo/b.py" in found and "repo/a.py" not in found


@pytest.mark.parametrize("kind", ["check", "format"])
@pytest.mark.parametrize("config", ["project", "checkout"])
def test_a_file_the_configuration_excludes_below_a_named_upstream_directory_stays_unlinted(
    project, kind, config
):
    # The configuration (the checkout's own, or the project's root one) excludes a fixture directory, the
    # project adds a fixture there, and the rule names the checkout: ruff walking `repo` skips the fixture,
    # so the gate must leave ruff the directory to walk rather than name the files in it.
    bad = "x=1\n" if kind == "format" else RUFF_PY
    # An unchanged file with the same finding beside the fixtures: upstream's, so not linted either.
    files = {"tests/test_a.py": bad, "tests/roots/old_fixture.py": bad}
    root = "line-length = 100\n"
    if config == "checkout":
        files["pyproject.toml"] = '[tool.ruff]\nextend-exclude = ["tests/roots/*"]\n'
    else:
        root += 'extend-exclude = ["repo/tests/roots/*"]\n'
    _write(project, {"ruff.toml": root})
    _checkout(project / "repo", files)
    _write(project / "repo", {"tests/roots/new_fixture.py": bad})
    options = "--check" if kind == "format" else "--select F401"
    result = _run(project, _command(f"Enforced: command ruff {kind} {options} repo"))
    assert result.status is _gate_base.Status.PASS, result.output
    first = result.output.splitlines()[0]
    assert "repo" in shlex.split(first), first  # ruff walks the directory the rule named
    found = result.output.split("\n", 1)[1]
    assert "new_fixture.py" not in found and "test_a.py" not in found, found
    assert "Nested git checkouts judged against their base: repo (base " in found


def test_a_named_project_file_is_still_linted_when_the_excludes_outgrow_the_command_line(project):
    _large_checkout(project)
    _write(project, {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["gen.py"]\n', "gen.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 . gen.py"))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert "--force-exclude" not in command, command[:500]
    assert "gen.py" in found and "repo/" not in found, found[:2000]


def test_the_projects_excludes_still_apply_to_the_files_the_gate_names_in_place_of_dot(project):
    _large_checkout(project)
    _write(project, {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["gen"]\n', "gen/g.py": RUFF_PY})
    _write(project, {"mine.py": "x = 1\n"})
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.PASS, result.output[:2000]


@pytest.mark.parametrize(
    ("command", "operands"),
    [
        (["ruff", "check", "a.py", "b"], [(2, "a.py"), (3, "b")]),
        (["ruff", "check", "--select", "F401", "a.py"], [(4, "a.py")]),
        (["ruff", "check", "--select=F401", "a.py"], [(3, "a.py")]),
        (["ruff", "check", "--fix", "--no-cache", "a.py"], [(4, "a.py")]),
        (["ruff", "check", "--select", "F401", "--", "-x.py", "--select"], [(5, "-x.py"), (6, "--select")]),
        (["ruff", "check", "--select", "F401"], []),
    ],
)
def test_list_ruff_operands(command, operands):
    assert _gate_rules._list_ruff_operands(command) == operands


def test_shellcheck_with_no_scripts_passes(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_prettier_excludes_explicit_unchanged_file(project):
    _checkout(project / "repo", {"bad.js": "var   a = 1\n"})
    result = _run(project, _command("Enforced: command prettier --check repo/bad.js"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_shellcheck_with_options_and_no_scripts_passes(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck --severity warning"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_prettier_checks_explicit_changed_file_alongside_unchanged_file(project):
    _checkout(project / "repo", {"a.js": "var   a = 1\n", "b.js": "var b = 1;\n"})
    (project / "repo/b.js").write_text("var   b = 2\n")
    result = _run(project, _command("Enforced: command prettier --check repo/a.js repo/b.js"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/b.js" in result.output.split("Checking formatting...")[1]
    assert "repo/a.js" not in result.output.split("Checking formatting...")[1]


def test_shellcheck_invalid_options_still_fail_when_no_scripts(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck --not-an-option"))
    assert result.status is _gate_base.Status.FAIL, result.output


def test_shellcheck_optional_color_with_no_scripts_passes(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck --color"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_shellcheck_filters_explicit_upstream_operands_and_keeps_option_values(project):
    _checkout(project / "repo", {"bad.sh": BAD_SH, "changed.sh": BAD_SH})
    (project / "repo/changed.sh").write_text(BAD_SH + "git tag $2\n")
    result = _run(
        project, _command("Enforced: command shellcheck --severity info repo/bad.sh -- repo/changed.sh")
    )
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/bad.sh" not in result.output.splitlines()[0]
    assert "repo/changed.sh" in result.output
    assert "checks.sh" in result.output


@pytest.mark.parametrize("option", ["--severity nonsense", "--color=bogus"])
def test_shellcheck_invalid_option_values_fail_without_scripts(tmp_path, option):
    result = _run(tmp_path, _command(f"Enforced: command shellcheck {option}"))
    assert result.status is _gate_base.Status.FAIL, result.output


def test_prettier_separator_keeps_generated_options_before_operands(project):
    _checkout(project / "repo", {"bad.js": "var   a = 1\n"})
    _write(project, {"own.js": "var b = 1;\n"})
    result = _run(project, _command("Enforced: command prettier --check -- repo/bad.js own.js"))
    assert result.status is _gate_base.Status.PASS, result.output


@pytest.mark.parametrize("arguments", ["repo/bad.sh", "--severity warning repo/bad.sh", "-- repo/bad.sh"])
def test_shellcheck_only_explicit_unchanged_script_passes(project, arguments):
    _checkout(project / "repo", {"bad.sh": BAD_SH})
    result = _run(project, _command(f"Enforced: command shellcheck {arguments}"))
    assert result.status is _gate_base.Status.PASS, result.output


@pytest.mark.parametrize("arguments", ["--not-an-option repo/bad.sh", "--severity nonsense repo/bad.sh"])
def test_shellcheck_invalid_options_fail_with_only_unchanged_script(project, arguments):
    _checkout(project / "repo", {"bad.sh": BAD_SH})
    result = _run(project, _command(f"Enforced: command shellcheck {arguments}"))
    assert result.status is _gate_base.Status.FAIL, result.output


def test_shellcheck_generated_scripts_before_separator_are_checked(project):
    _checkout(project / "repo", {"bad.sh": BAD_SH})
    _write(project, {"own.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck -- repo/bad.sh"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "own.sh" in result.output
    assert "SC2086" in result.output


@pytest.mark.parametrize("arguments", ["", "--"])
def test_shellcheck_checks_generated_script_starting_with_dash(tmp_path, arguments):
    _write(tmp_path, {"-bad.sh": BAD_SH})
    result = _run(tmp_path, _command(f"Enforced: command shellcheck {arguments}"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "SC2086" in result.output


# -- what counts as the project's in a nested checkout --


NO_BASE = "base unknown, so its files are judged as the project's"


def test_a_repository_the_project_made_itself_is_the_projects(project):
    _self_made_checkout(project / "app", {"deploy.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "app/deploy.sh" in result.output.splitlines()[0]
    assert f"Nested git checkout app: {NO_BASE}" in result.output


def test_a_fix_committed_on_the_clones_local_main_is_linted(project):
    repo = project / "repo"
    _checkout(repo, {"utils/bump.sh": "#!/bin/sh\necho ok\n", "utils/old.sh": BAD_SH})
    _write(repo, {"utils/bump.sh": BAD_SH})
    _git(repo, "commit", "-qam", "fix")
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/utils/bump.sh" in first
    assert "repo/utils/old.sh" not in first
    assert "judged against their base: repo (base " in result.output


def test_a_clone_without_a_remote_is_the_projects(project):
    repo = project / "repo"
    _checkout(repo, {"utils/bump.sh": BAD_SH})
    _git(repo, "remote", "remove", "origin")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/utils/bump.sh" in result.output.splitlines()[0]
    assert f"Nested git checkout repo: {NO_BASE}" in result.output


def test_ruff_lints_a_repository_the_project_made_itself(project):
    _self_made_checkout(project / "app", {"mod.py": RUFF_PY})
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "app/mod.py" in result.output.split("\n", 1)[1]
    assert f"Nested git checkout app: {NO_BASE}" in result.output


def test_a_gitignored_new_script_in_a_nested_checkout_is_the_projects(project):
    _checkout(project / "repo", {"utils/bad.sh": BAD_SH, ".gitignore": "build/\n"})
    _write(project / "repo", {"build/new.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/build/new.sh" in result.output.splitlines()[0]
    assert "repo/utils/bad.sh" not in result.output.splitlines()[0]


def test_a_gitignored_new_python_file_in_a_nested_checkout_is_not_excluded_from_ruff(project):
    _checkout(project / "repo", {"lib/ok.py": "x = 1\n", ".gitignore": "build/\n"})
    _write(project / "repo", {"build/x.py": RUFF_PY})
    # Walking a directory, ruff itself skips git-ignored files, the project's own as well: only the gate's
    # excludes are the upstream's question.
    result = _run(project, _ruff_rules()[0][1])
    assert _inline_excludes(result) == ["./repo/lib"], result.output
    result = _run(project, _command("Enforced: command ruff check --select F401 repo/build/x.py"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/build/x.py" in result.output.split("\n", 1)[1]


def test_a_script_edited_inside_a_submodule_of_a_nested_checkout_is_linted(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    _self_made_checkout(tmp_path / "subsrc", {"s.sh": "#!/bin/sh\necho ok\n"})
    _checkout(project / "repo", {"a.txt": "a\n"})
    _git(
        project / "repo",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(tmp_path / "subsrc"),
        "sub",
    )
    _git(project / "repo", "commit", "-q", "-m", "sub")
    (project / "repo/sub/s.sh").write_text(BAD_SH)
    result = _run(project, _command("Enforced: command shellcheck"))
    assert "repo/sub/s.sh" in result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output


def _submodule_checkout(tmp_path: Path, project: Path, sub_files: dict[str, str], at: str = "sub") -> str:
    """A clone of an upstream holding a submodule, made with --recurse-submodules; returns the gitlink."""
    _self_made_checkout(tmp_path / "subsrc", sub_files)
    upstream = _upstream_of(project / "repo")
    _self_made_checkout(upstream, {"a.txt": "a\n"})
    _git(upstream, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(tmp_path / "subsrc"), at)
    _git(upstream, "commit", "-q", "-m", "sub")
    allow = ("-c", "protocol.file.allow=always")
    _git(project, *allow, "clone", "-q", "--recurse-submodules", str(upstream), "repo")
    return _git(project / "repo", "rev-parse", f"HEAD:{at}")


def _commit_second_submodule_version(tmp_path: Path) -> str:
    """Upstream of the submodule moves on: `lib.sh` edited, `new.sh` added, `keep.sh` as it was."""
    _write(tmp_path / "subsrc", {"lib.sh": BAD_SH + "echo $2\n", "new.sh": BAD_SH})
    _git(tmp_path / "subsrc", "add", "-A")
    _git(tmp_path / "subsrc", "commit", "-q", "-m", "v2")
    return _git(tmp_path / "subsrc", "rev-parse", "HEAD")


SUBMODULE_FILES = {"lib.sh": BAD_SH, "keep.sh": "#!/bin/sh\necho ok\n"}


def test_an_unchanged_script_in_a_submodule_at_its_recorded_commit_is_not_linted(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    gitlink = _submodule_checkout(tmp_path, project, {"lib.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert "repo/sub/lib.sh" not in result.output.splitlines()[0]
    assert f"repo/sub (base {gitlink[:12]})" in result.output


def test_an_unchanged_submodule_at_a_deeper_path_is_not_linted(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    gitlink = _submodule_checkout(tmp_path, project, {"lib.sh": BAD_SH}, at="vendor/sub")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert f"repo/vendor/sub (base {gitlink[:12]})" in result.output


def test_an_unchanged_clone_inside_a_nested_checkout_is_not_linted(project, tmp_path):
    _checkout(project / "repo", {"a.txt": "a\n"})
    _self_made_checkout(tmp_path / "innersrc", {"tox.sh": BAD_SH})
    _git(project / "repo", "clone", "-q", str(tmp_path / "innersrc"), "inner")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert "repo/inner (base " in result.output


def test_a_submodule_the_project_moved_on_is_judged_against_the_recorded_commit(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    gitlink = _submodule_checkout(tmp_path, project, SUBMODULE_FILES)
    moved = _commit_second_submodule_version(tmp_path)
    _git(project / "repo/sub", "fetch", "-q", "origin")
    _git(project / "repo/sub", "checkout", "-q", moved)
    result = _run(project, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/sub/lib.sh" in first and "repo/sub/new.sh" in first
    assert "repo/sub/keep.sh" not in first
    assert f"repo/sub (base {gitlink[:12]})" in result.output


def test_a_submodule_lacking_the_recorded_commit_is_judged_as_the_projects(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    _submodule_checkout(tmp_path, project, SUBMODULE_FILES)
    moved = _commit_second_submodule_version(tmp_path)
    sub = project / "repo/sub"
    shutil.rmtree(sub)
    sub.mkdir()
    _git(sub, "init", "-q")
    _git(sub, "fetch", "-q", "--depth", "1", f"file://{tmp_path / 'subsrc'}", moved)
    _git(sub, "checkout", "-q", "FETCH_HEAD")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/sub/lib.sh" in result.output.splitlines()[0]
    unknown = "Nested git checkout repo/sub: base unknown, so its files are judged as the project's"
    assert unknown in result.output


def test_ruff_leaves_an_unchanged_submodule_alone(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    _submodule_checkout(tmp_path, project, {"x.py": "import os\n"})
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.PASS, result.output


def test_nested_checkouts_list_a_submodule_after_its_outer_one_with_the_recorded_commit_as_base(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    gitlink = _submodule_checkout(tmp_path, project, {"lib.sh": BAD_SH})
    [outer, inner] = _target_run.nested_checkouts(project, _gate_base._SKIP_DIRS)
    assert (outer.root, inner.root) == (project / "repo", project / "repo/sub")
    assert (inner.base, inner.changed) == (gitlink, set())
    assert outer.changed == set()


# -- excludes: anchored, collapsed, escaped --


@pytest.mark.parametrize("kind", ["check", "lint", "format"])
def test_ruff_still_judges_a_project_directory_named_like_a_checkout(project, kind):
    _checkout(project / "repo", {"lib/ok.py": "x = 1\n"})
    _write(project, {"tools/repo/mine.py": "x=1\n" if kind == "format" else RUFF_PY})
    result = _run(project, dict(_ruff_rules())[kind])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "tools/repo/mine.py" in result.output.split("\n", 1)[1]


ESLINT_CONFIG = {"eslint.config.mjs": 'export default [{ rules: { semi: "error" } }];\n'}


def test_eslint_still_judges_a_project_directory_named_like_a_checkout(project):
    _checkout(project / "repo", {"lib/ok.js": "var a = 1;\n"})
    _write(project, {**ESLINT_CONFIG, "tools/repo/mine.js": "var b = 2\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "tools/repo/mine.js" in result.output.split("\n", 1)[1]


def test_unchanged_directories_are_excluded_whole_so_a_large_checkout_fits_the_command_line(project):
    files = {f"pkgs/p{i}/src/m{j}.py": RUFF_PY for i in range(40) for j in range(75)}
    _checkout(project / "repo", {**files, "pkgs/p0/changed.py": "x = 1\n"})
    (project / "repo/pkgs/p0/changed.py").write_text("x = 2\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    assert len(result.output.splitlines()[0]) < 2000, result.output.splitlines()[0][:500]
    others = sorted(f"./repo/pkgs/p{i}" for i in range(1, 40))
    assert _inline_excludes(result) == ["./repo/pkgs/p0/src", *others]


def test_a_changed_file_beside_unchanged_ones_and_directories_is_linted(project):
    _checkout(project / "repo", {"pkg/a.py": RUFF_PY, "pkg/b.py": "x = 1\n", "pkg/sub/c.py": RUFF_PY})
    (project / "repo/pkg/b.py").write_text(RUFF_PY)
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert _inline_excludes(result) == ["./repo/pkg/a.py", "./repo/pkg/sub"]
    found = result.output.split("\n", 1)[1]
    assert "repo/pkg/b.py" in found and "repo/pkg/a.py" not in found and "repo/pkg/sub/c.py" not in found


@pytest.mark.parametrize("kind", ["check", "format"])
def test_gitignored_bytecode_does_not_keep_a_large_checkout_from_being_excluded_whole(project, kind):
    # Python leaves a git-ignored `__pycache__` in every package; such a file is the project's, but not one
    # ruff reads, so each package is excluded whole rather than file by file.
    files = {f"pkg{i}/module_{j:02}_with_a_longer_name.py": "x = 1\n" for i in range(150) for j in range(20)}
    _checkout(project / "repo", {**files, ".gitignore": "__pycache__/\n"})
    _write(project / "repo", {f"pkg{i}/__pycache__/x.pyc": "" for i in range(150)})
    rule = dict(_ruff_rules())[kind]
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    assert _inline_excludes(result) == ["./repo"]
    (project / "repo/pkg7/module_03_with_a_longer_name.py").write_text(
        "x=1\n" if kind == "format" else RUFF_PY
    )
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert "repo/pkg7/module_03_with_a_longer_name.py" in result.output.split("\n", 1)[1]


@pytest.mark.parametrize("kind", ["check", "lint", "format"])
def test_a_flat_directory_of_thousands_of_unchanged_files_keeps_the_command_short(project, kind):
    # No directory can be excluded whole beside a changed file, so each file would be: 4,000 paths in one
    # argument pass the kernel's 128 KiB limit on a single argument. Ruff is given the project's files
    # instead, leaving out a git-ignored one as walking `.` does.
    names = [f"repo/flat/module_{i:04}_with_a_longer_name.py" for i in range(4000)]
    files = {name.removeprefix("repo/"): "x = 1\n" for name in names}
    _checkout(project / "repo", {**files, ".gitignore": "flat/generated.py\n"})
    _write(project, {names[17]: "y = 2\n", "repo/flat/generated.py": RUFF_PY, "tools/mine.py": "x = 1\n"})
    rule = dict(_ruff_rules())[kind]
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    command = result.output.splitlines()[0]
    assert len(command) < 2000, command[:500]
    assert "extend-exclude =" not in command and "Ruff read no configuration" not in result.output
    words = shlex.split(command)
    assert "." not in words and words[words.index(names[17]) + 1] == "tools", command
    assert ("--isolated" in command) == (kind == "lint")
    (project / names[17]).write_text("x=1\n" if kind == "format" else RUFF_PY)
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[17] in result.output.split("\n", 1)[1]


def test_ruff_with_excludes_still_reads_the_projects_own_configuration(project):
    _write(
        project,
        {
            "pyproject.toml": '[tool.ruff]\nextend-exclude = ["gen"]\n[tool.ruff.lint]\nselect = ["F401"]\n',
            "gen/g.py": RUFF_PY,
            "mine.py": "x = 1\n",
        },
    )
    _checkout(project / "repo", {"a.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.PASS, result.output
    assert _inline_excludes(result) == ["./repo"]
    _write(project, {"mine.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "mine.py" in result.output.split("\n", 1)[1] and "gen/g.py" not in result.output


STRICT = {
    "pyproject.toml": '[tool.ruff.lint]\nselect = ["F401"]\n',
    # A nested configuration stricter than the project root's, for the files below it.
    "strict/ruff.toml": 'line-length = 20\n[lint]\nselect = ["F401", "E501"]\n',
    "strict/long.py": 'x = "a line longer than twenty"\n',
}


@pytest.mark.parametrize("unchanged", [1, 4000])
def test_ruff_with_excludes_still_applies_a_nested_stricter_configuration(project, unchanged):
    # Inline excludes for one unchanged file; the project's files named instead for 4,000 of them.
    names = [f"flat/module_{i:04}_with_a_longer_name.py" for i in range(unchanged)]
    _checkout(project / "repo", {**dict.fromkeys(names, "x = 1\n"), "flat/changed.py": "x = 1\n"})
    _write(project, {"repo/flat/changed.py": "y = 2\n", **STRICT})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("extend-exclude =" in command) == (unchanged == 1) and len(command) < 2000, command[:500]
    assert "E501" in found and "strict/long.py" in found and "repo/" not in found, found[:2000]
    _write(project, {"strict/long.py": "x = 1\n"})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.PASS, result.output[:2000]


def test_too_many_project_files_to_name_fall_back_to_a_configuration_file_and_say_so(project, temporary):
    # Half of 3,200 files changed: neither the 1,600 unchanged ones nor the 1,600 changed ones fit the
    # command line. Excluded through a file, ruff reads no nested configuration, and the output says so.
    names = [f"repo/flat/module_{i:04}_with_a_longer_name.py" for i in range(3200)]
    _checkout(project / "repo", {name.removeprefix("repo/"): "x = 1\n" for name in names})
    _write(project, dict.fromkeys(names[1::2], "y = 2\n"))
    _write(project, STRICT)
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    assert len(result.output.splitlines()[0]) < 2000, result.output.splitlines()[0][:500]
    assert "Ruff read no configuration nested below the project root" in result.output
    assert _ruff_config(result, temporary) == {
        "extend": str(project / "pyproject.toml"),
        "extend-exclude": [f"./{name}" for name in names[::2]],
    }
    _write(project, {names[1]: RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[1] in result.output.split("\n", 1)[1]


MARKDOWN = "# T\n\n```python\nx=[1,2,\n3]\n```\n"  # ruff format reads Python blocks in Markdown


def test_ruff_format_leaves_an_unchanged_upstream_markdown_file_alone(project):
    _checkout(project / "repo", {"README.md": MARKDOWN, "docs/guide.md": MARKDOWN, "pkg/a.py": "x = 1\n"})
    _write(project, {"repo/pkg/a.py": "x = 2\n"})
    result = _run(project, dict(_ruff_rules())["format"])
    assert result.status is _gate_base.Status.PASS, result.output
    assert _inline_excludes(result) == ["./repo/README.md", "./repo/docs"]
    _write(project, {"repo/README.md": MARKDOWN + "\n"})
    result = _run(project, dict(_ruff_rules())["format"])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/README.md" in result.output.split("\n", 1)[1]


@pytest.mark.parametrize("unchanged", [1, 4000])
def test_ruff_still_reads_the_projects_markdown_and_extend_include_files(project, unchanged):
    # Inline excludes for one unchanged file; the project's files named instead for 4,000 of them. Both a
    # Markdown file (ruff's default `include`) and a `.pyw` the project's `extend-include` adds are read,
    # and a new `.pyw` beside an unchanged `.py` keeps its directory from being excluded whole.
    names = [f"flat/module_{i:04}_with_a_longer_name.py" for i in range(unchanged)]
    files = {**dict.fromkeys(names, "x = 1\n"), "flat/changed.py": "x = 1\n", "other/a.py": "x = 1\n"}
    _checkout(project / "repo", files)
    _write(project, {"repo/flat/changed.py": "y = 2\n", "ruff.toml": 'extend-include = ["*.pyw"]\n'})
    _write(project, {"README.md": MARKDOWN})
    result = _run(project, dict(_ruff_rules())["format"])
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("extend-exclude =" in command) == (unchanged == 1) and len(command) < 2000, command[:500]
    assert "README.md" in found and "repo/" not in found, found[:2000]
    _write(project, {"README.md": "# T\n", "repo/other/new.pyw": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    found = result.output.split("\n", 1)[1]
    assert "repo/other/new.pyw" in found and "flat/module" not in found, found[:2000]


def test_a_lint_rule_with_excludes_still_reads_no_project_configuration(project):
    _write(
        project,
        {
            "pyproject.toml": (
                '[tool.ruff]\nextend-exclude = ["gen"]\n'
                '[tool.ruff.lint.per-file-ignores]\n"mine.py" = ["F401"]\n'
            ),
            "gen/g.py": RUFF_PY,
            "mine.py": RUFF_PY,
        },
    )
    _checkout(project / "repo", {"a.py": RUFF_PY})
    configured = _run(project, _ruff_rules()[0][1])
    assert configured.status is _gate_base.Status.PASS, configured.output
    result = _run(project, dict(_ruff_rules())["lint"])
    assert result.status is _gate_base.Status.FAIL, result.output
    found = result.output.split("\n", 1)[1]
    assert "gen/g.py" in found and "mine.py" in found and "repo/a.py" not in found


def _thousands_of_js_files(project: Path) -> list[str]:
    """A checkout holding 3,000 files in one directory, one of them changed (and still well formatted)."""
    names = [f"repo/lib/module_{i:04}_with_a_longer_name.js" for i in range(3000)]
    _checkout(project / "repo", {name.removeprefix("repo/"): "var a = 1;\n" for name in names})
    (project / names[5]).write_text("var a = 2;\n")
    return names


def test_eslint_with_thousands_of_unchanged_files_is_given_the_projects_files(project):
    # Each ignore pattern is matched against every file ESLint walks: thousands of them took minutes over a
    # large checkout. ESLint is given the project's files instead, and still applies the project's config.
    names = _thousands_of_js_files(project)
    config = 'export default [{ rules: { semi: "error" } }, { ignores: ["repo/lib/generated.js"] }];\n'
    # A file of the project's that its config ignores, and one with no config for its extension.
    _write(
        project, {"eslint.config.mjs": config, "repo/lib/generated.js": "var g = 1\n", "tools/t.ts": "x\n"}
    )
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    command = shlex.split(result.output.splitlines()[0])
    assert len(shlex.join(command)) < 2000 and "--config" not in command, command[:20]
    assert {names[5], "repo/lib/generated.js", "tools", "eslint.config.mjs"} <= set(
        command
    ) and "." not in command
    assert "warning" not in result.output
    (project / names[5]).write_text("var a = 3\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[5] in result.output.split("\n", 1)[1]


def test_eslint_with_thousands_of_excludes_and_no_dot_reads_them_from_a_config(project, temporary):
    # A command naming its own paths cannot have them swapped for the project's files.
    names = _thousands_of_js_files(project)
    _write(project, {**ESLINT_CONFIG, "own.js": "var b = 2;\n"})
    rule = _command("Enforced: command eslint repo own.js")
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    assert len(result.output.splitlines()[0]) < 2000, result.output.splitlines()[0][:500]
    assert _eslint_ignores(result, temporary)[:-1] == [name for name in names if name != names[5]]
    (project / names[5]).write_text("var a = 3\n")
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[5] in result.output.split("\n", 1)[1]


def test_prettier_with_thousands_of_unchanged_files_is_given_the_projects_files(project):
    names = _thousands_of_js_files(project)
    unformatted = "var   g = 1\n"
    _write(
        project,
        {
            # Prettier's own ignore files still apply to the files it is given, and a file it has no
            # parser for is skipped as walking `.` skips it.
            ".prettierignore": "repo/lib/skipped.js\n",
            ".gitignore": "repo/lib/built.js\n",
            "repo/lib/skipped.js": unformatted,
            "repo/lib/built.js": unformatted,
            "repo/lib/data.unknown": unformatted,
        },
    )
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    command = shlex.split(result.output.splitlines()[0])
    assert len(shlex.join(command)) < 2000 and "--ignore-path" not in command, command[:20]
    assert {names[5], "repo/lib/skipped.js", "repo/lib/data.unknown", "evidence"} <= set(command)
    assert "." not in command
    (project / names[5]).write_text(unformatted)
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[5] in result.output.split("\n", 1)[1]


@pytest.mark.parametrize("option", ["-c strict.mjs", "--config strict.mjs", "--config=strict.mjs"])
def test_eslint_with_excludes_still_uses_the_config_the_command_names(project, option, temporary):
    _write(
        project,
        {
            "eslint.config.mjs": "export default [];\n",
            "strict.mjs": 'export default [{ rules: { semi: "error" } }];\n',
            "tools/x.js": "var x = 1\n",
        },
    )
    _checkout(project / "repo", {"lib/up.js": "var u = 1\n"})
    result = _run(project, _command(f"Enforced: command eslint {option} ."))
    assert result.status is _gate_base.Status.FAIL, result.output
    found = result.output.split("\n", 1)[1]
    assert "tools/x.js" in found and "semi" in found and "repo/lib/up.js" not in found
    assert "strict.mjs" not in result.output.splitlines()[0]
    assert 'import base from "./strict.mjs";' in _named_file(result, temporary)


def test_eslint_with_a_legacy_config_keeps_the_ignore_file_the_command_names(project, temporary, monkeypatch):
    monkeypatch.setenv("ESLINT_USE_FLAT_CONFIG", "false")
    _write(
        project,
        {
            ".eslintrc.json": '{"root": true, "rules": {"semi": "error"}}\n',
            ".eslintignore": "own.js\n",
            ".myignore": "gen\n",
            "gen/g.js": "var g = 1\n",
            "own.js": "var b = 2\n",
        },
    )
    _checkout(project / "repo", {"lib/x.js": "var a = 1\n"})
    result = _run(project, _command("Enforced: command eslint --ignore-path .myignore ."))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert _named_file(result, temporary, "--ignore-path") == "gen\n\n/repo\n"
    assert ".myignore" not in result.output.splitlines()[0]
    found = result.output.split("\n", 1)[1]
    assert "own.js" in found and "gen/g.js" not in found and "repo/lib/x.js" not in found


def test_eslint_with_a_legacy_config_reads_excludes_from_an_ignore_file(project, temporary, monkeypatch):
    monkeypatch.setenv("ESLINT_USE_FLAT_CONFIG", "false")  # ESLint 9 reads `.eslintrc` only when told to
    _write(
        project,
        {
            ".eslintrc.json": '{"root": true, "rules": {"semi": "error"}}\n',
            ".eslintignore": "gen\n",
            "gen/g.js": "var g = 1\n",
            "own.js": "var b = 2;\n",
        },
    )
    _checkout(project / "repo", {"lib/x.js": "var a = 1\n", "lib/[id].js": "var c = 1\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output
    assert _named_file(result, temporary, "--ignore-path") == "gen\n\n/repo\n"
    (project / "repo/lib/x.js").write_text("var a = 2\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert _named_file(result, temporary, "--ignore-path") == "gen\n\n/repo/lib/\\[id\\].js\n"
    found = result.output.split("\n", 1)[1]
    assert "repo/lib/x.js" in found and "[id].js" not in found and "gen/g.js" not in found


def test_ruff_excludes_an_unchanged_file_whose_name_holds_glob_characters(project):
    _checkout(project / "repo", {"pkg/a[1].py": RUFF_PY, "pkg/st*r,{x}.py": RUFF_PY, "pkg/ok.py": "x = 1\n"})
    _write(project, {"pkg/a1.py": "x = 1\n", "pkg/stXr,x.py": "x = 1\n"})
    (project / "repo/pkg/ok.py").write_text("x = 2\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    (project / "repo/pkg/a[1].py").write_text("import sys\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pkg/a[1].py" in result.output.split("\n", 1)[1]


def test_eslint_excludes_an_unchanged_file_whose_name_holds_glob_characters(project):
    _checkout(project / "repo", {"pages/[id].js": "var a = 1\n", "pages/ok.js": "var c = 3;\n"})
    _write(project, ESLINT_CONFIG)
    (project / "repo/pages/ok.js").write_text("var c = 4;\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output
    (project / "repo/pages/[id].js").write_text("var a = 2\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pages/[id].js" in result.output.split("\n", 1)[1]


def test_eslint_does_not_exclude_a_project_file_a_glob_character_would_match(project):
    _checkout(project / "repo", {"pages/[id].js": "var a = 1;\n", "pages/ok.js": "var c = 3;\n"})
    _write(project, ESLINT_CONFIG)
    (project / "repo/pages/ok.js").write_text("var c = 4;\n")
    _write(project / "repo", {"pages/i.js": "var d = 1\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pages/i.js" in result.output.split("\n", 1)[1]


QUESTION_MARK_JS = {"e?.js": "var   a = 1\n", "keep.js": "const k = 1;\n"}


def test_prettier_excludes_an_unchanged_file_whose_name_holds_a_question_mark(project):
    _checkout(project / "repo", QUESTION_MARK_JS)
    (project / "repo/keep.js").write_text("const k = 2;\n")
    result = _run(project, PRETTIER)
    first = result.output.splitlines()[0]
    assert result.status is _gate_base.Status.PASS, result.output
    assert "--ignore-path" not in first and "repo/keep.js" in first, first
    (project / "repo/e?.js").write_text("var   a = 2\n")
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "[warn] repo/e?.js" in result.output


def test_prettier_does_not_exclude_a_project_file_a_question_mark_would_match(project):
    _checkout(project / "repo", QUESTION_MARK_JS)
    (project / "repo/keep.js").write_text("const k = 2;\n")
    _write(project / "repo", {"ex.js": "var   b = 1\n"})
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "[warn] repo/ex.js" in result.output
    assert "[warn] repo/e?.js" not in result.output


PRETTIER_REPO = _command("Enforced: command prettier --check repo")


def test_prettier_without_a_dot_operand_writes_a_bare_question_mark_where_no_other_file_matches(
    project, temporary
):
    _checkout(project / "repo", QUESTION_MARK_JS)
    (project / "repo/keep.js").write_text("const k = 2;\n")
    result = _run(project, PRETTIER_REPO)
    assert result.status is _gate_base.Status.PASS, result.output
    assert _named_file(result, temporary, "--ignore-path") == "/repo/e?.js\n"


def test_prettier_without_a_dot_operand_judges_an_upstream_file_an_ignore_file_cannot_spell_and_says_so(
    project, temporary
):
    _checkout(project / "repo", QUESTION_MARK_JS)
    (project / "repo/keep.js").write_text("const k = 2;\n")
    _write(project / "repo", {"ex.js": "var   b = 1\n"})
    result = _run(project, PRETTIER_REPO)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert _named_file(result, temporary, "--ignore-path") == ""
    assert "[warn] repo/ex.js" in result.output
    assert (
        "cannot spell `?`" in result.output and "`repo/e?.js` (`repo/ex.js` would match it)" in result.output
    )


def test_eslint_with_a_legacy_config_excludes_a_file_whose_name_holds_a_question_mark(
    project, temporary, monkeypatch
):
    monkeypatch.setenv("ESLINT_USE_FLAT_CONFIG", "false")
    _write(project, {".eslintrc.json": '{"root": true, "rules": {"semi": "error"}}\n'})
    _checkout(project / "repo", {"lib/e?.js": "var a = 1\n", "lib/x.js": "var x = 1;\n"})
    (project / "repo/lib/x.js").write_text("var x = 2;\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output
    assert _named_file(result, temporary, "--ignore-path") == "/repo/lib/e?.js\n"
    _write(project / "repo", {"lib/ex.js": "var e = 1\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/lib/ex.js" in result.output.split("\n", 1)[1]
    assert "`repo/lib/e?.js` (`repo/lib/ex.js` would match it)" in result.output


@pytest.mark.parametrize("option", ["-Calways", "-Cauto", "-aCalways", "-Cnever", "-sbash"])
def test_shellcheck_filters_an_upstream_operand_after_an_attached_value(project, option):
    _checkout(project / "repo", {"x.sh": BAD_SH})
    upstreams = _vendored.load(project, _gate_base._SKIP_DIRS)
    command = ["shellcheck", option, "repo/x.sh", "mine.sh"]
    assert _gate_rules._drop_unchanged_operands(command, upstreams) == ["shellcheck", option, "mine.sh"]


def test_the_recorded_prettier_command_is_the_one_that_ran(project):
    _write(project, {"own.js": "var b = 2;\n"})
    _checkout(project / "repo", {"lib/x.js": "var   a = 1\n"})
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.PASS, result.output
    first = result.output.splitlines()[0]
    assert "--ignore-path .gitignore --ignore-path .prettierignore --ignore-path " + str(project) in first


@pytest.mark.parametrize("unchanged", [5, 60])
def test_eslint_lints_the_projects_files_of_any_extension_its_config_reads(project, unchanged):
    # Few unchanged directories (a wrapper config ignores them) or more than `_MAX_IGNORES` (the project's
    # files named instead): either way the config's `files`, not an extension, decide what ESLint reads, so
    # a project file only `files` matches is linted and an unchanged upstream one is not.
    upstream = {f"d{i:02}/u{i}.js": "var a = 1;\n" for i in range(unchanged)}
    _checkout(project / "repo", {**upstream, "d01/x.es6": "var u = 1\n"})
    config = 'export default [{ files: ["**/*.es6"], rules: { semi: "error" } }];\n'
    _write(
        project, {"repo/d02/u2.js": "var a = 2;\n", "eslint.config.mjs": config, "lib/x.es6": "var b = 2\n"}
    )
    _write(project, {"repo/d01/mine.es6": "var m = 2\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("--config" in command) == (unchanged == 5), command[:500]
    assert "lib/x.es6" in found and "repo/d01/mine.es6" in found and "repo/d01/x.es6" not in found, found
    assert "2 problems (2 errors, 0 warnings)" in found, found


def test_eslint_with_no_config_lookup_imports_no_config_beside_excludes(project):
    _write(project, {**ESLINT_CONFIG, "tools/x.js": "var x = 1\n"})
    _checkout(project / "repo", {"lib/up.js": "var u = 1\n"})
    assert (
        _run(project, _command("Enforced: command eslint --no-config-lookup .")).status
        is _gate_base.Status.PASS
    )
    assert _run(project, ESLINT).status is _gate_base.Status.FAIL


@pytest.mark.parametrize("unchanged", [1, 3000])
def test_ruff_reads_the_files_the_projects_include_names(project, unchanged):
    # An `include` with no `extend-include` (ruff shows the empty one as `[]`), beside a checkout whose own
    # `pyproject.toml` includes something else: the project root's configuration decides, inline and with
    # the project's files named in place of `.`.
    files = {f"d02/many_{i:05}.py": "x = 1\n" for i in range(unchanged)}
    files["pyproject.toml"] = '[tool.ruff]\ninclude = ["pkg/**/*.py"]\n'
    _checkout(project / "repo", {**files, "d02/u2.py": "x = 1\n"})
    _write(
        project,
        {"repo/d02/u2.py": "x = 2\n", "ruff.toml": 'include = ["*.py", "*.pyw"]\n', "lib/x.pyw": RUFF_PY},
    )
    assert _gate_rules._list_ruff_includes(["ruff", "check", "."], project) == ("*.py", "*.pyw")
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("extend-exclude =" in command) == (unchanged == 1), command[:500]
    assert "lib/x.pyw" in found and "repo/" not in found, found[:2000]


def test_ruff_reads_the_projects_include_even_when_its_excludes_match_the_gates_empty_file(project):
    # The gate asks ruff about an empty root file named `tmp*.py`; a project that force-excludes such names
    # must still have its `extend-include` read, or an owned `.pyw` beside unchanged files goes unlinted.
    files = {f"d00/u{i}.py": "x = 1\n" for i in range(5)}
    _checkout(project / "repo", files)
    config = 'force-exclude = true\nextend-exclude = ["tmp*"]\nextend-include = ["*.pyw"]\n'
    _write(project, {"ruff.toml": config, "repo/d00/mine.pyw": RUFF_PY})
    assert "*.pyw" in _gate_rules._list_ruff_includes(["ruff", "check", "."], project)
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert "repo/d00/mine.pyw" in result.output.split("\n", 1)[1], result.output[:2000]


# -- factory:ui-evidence --

UI_RULE = _command("Enforced: check factory:ui-evidence")


def _ui_status(project: Path) -> _gate_base.Status:
    results = _gates.standards_results(project, {"engineering.md": UI_RULE}, configs=CONFIGS)
    [result] = [r for r in results if r.rule == "factory:ui-evidence"]
    return result.status


def test_an_unchanged_upstream_page_is_not_the_projects_ui(project):
    _write(project, {"a.py": "x = 1\n"})
    _checkout(project / "repo", {"index.html": "<p>hi</p>\n"})
    assert _ui_status(project) is _gate_base.Status.NOT_APPLICABLE
    (project / "repo/index.html").write_text("<p>changed</p>\n")
    assert _ui_status(project) is _gate_base.Status.TOOL_UNAVAILABLE


def test_a_project_page_beside_an_unchanged_upstream_one_is_ui(project):
    _write(project, {"ui/index.html": "<p>mine</p>\n"})
    _checkout(project / "repo", {"index.html": "<p>hi</p>\n"})
    assert _ui_status(project) is _gate_base.Status.TOOL_UNAVAILABLE


# -- suppressions policy, when no base commit gives it a diff --

SUPPRESSED_PY = "x = 1  # noqa: E501\ntry:\n    pass\nexcept Exception:\n    pass\n"
LINT = _command("Enforced: lint ruff E722")


def _suppressions(project: Path) -> _gates.Result:
    results = _gates.standards_results(project, {"engineering.md": LINT}, configs=CONFIGS)
    [result] = [r for r in results if r.rule == "factory:suppressions"]
    return result


def test_a_suppression_in_an_unchanged_checkout_file_passes_without_a_base(project):
    _checkout(project / "repo", {"pkg/a.py": SUPPRESSED_PY})
    result = _suppressions(project)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "checked every line: no base commit to diff against" in result.output
    assert "Nested git checkouts judged against their base: repo (base " in result.output


def test_a_suppression_in_a_changed_checkout_file_or_a_project_file_still_fails_without_a_base(project):
    _checkout(project / "repo", {"pkg/a.py": SUPPRESSED_PY})
    _write(project / "repo", {"pkg/a.py": SUPPRESSED_PY + "# changed\n"})
    result = _suppressions(project)
    assert result.status is _gate_base.Status.FAIL and "repo/pkg/a.py:1" in result.output
    _git(project / "repo", "checkout", "-q", "--", ".")
    _write(project, {"mine.py": SUPPRESSED_PY})
    result = _suppressions(project)
    assert result.status is _gate_base.Status.FAIL and "mine.py:1" in result.output
    assert "repo/pkg/a.py" not in result.output.split("Nested git")[0]


def test_a_suppression_in_a_checkout_with_no_known_base_is_judged_as_the_projects(project):
    _self_made_checkout(project / "repo", {"pkg/a.py": SUPPRESSED_PY})
    result = _suppressions(project)
    assert result.status is _gate_base.Status.FAIL and "repo/pkg/a.py:1" in result.output
    assert "base unknown, so its files are judged as the project's" in result.output


def test_eslint_skips_a_named_upstream_directory_that_is_wholly_upstreams_and_judges_a_changed_one(project):
    _write(project, {"eslint.config.mjs": 'export default [{ rules: { semi: "error" } }];\n'})
    # An unchanged file with a finding in each directory: upstream's, so not linted.
    _checkout(project / "repo", {"docs/d.js": "var d = 1\n", "src/a.js": "var a = 1\n"})
    _write(project / "repo", {"src/new.js": "var b = 2\n"})
    result = _run(project, _command("Enforced: command eslint repo/docs"))
    assert result.status is _gate_base.Status.PASS, result.output
    found = result.output.split("\n", 1)[1]
    assert "d.js" not in found and "Nested git checkouts judged against their base: repo (base " in found
    result = _run(project, _command("Enforced: command eslint repo/src"))
    assert result.status is _gate_base.Status.FAIL, result.output
    found = result.output.split("\n", 1)[1]
    assert "repo/src/new.js" in found and "a.js" not in found, found


# -- terraform fmt --

TF_FMT = _command("Enforced: command terraform fmt -check -recursive")
GOOD_TF = 'variable "a" {\n  default = 1\n}\n'
BAD_TF = 'variable   "b"   {\n default=2\n}\n'
#: Terraform is installed where the slow lane runs (CI's `tests-slow`, the factory image), as for the
#: benchmark's Terraform checks; a test that runs it is there.
terraform_runs = pytest.mark.slow


@pytest.fixture
def terraform(project):
    """A nested checkout at its base holding an unformatted file, with a formatted one the project edited."""
    _checkout(project / "repo", {"main.tf": GOOD_TF, "mod/bad.tf": BAD_TF})
    (project / "repo/main.tf").write_text(GOOD_TF.replace("= 1", "= 2"))
    return project


@terraform_runs
def test_an_unchanged_terraform_file_in_a_nested_checkout_is_not_format_checked(terraform):
    before = _digest(terraform)
    result = _run(terraform, TF_FMT)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "repo/mod/bad.tf" not in result.output
    assert result.output.splitlines()[0] == "terraform fmt -check repo/main.tf"
    assert "Nested git checkouts judged against their base: repo (base " in result.output
    assert _digest(terraform) == before


@terraform_runs
def test_a_changed_terraform_file_in_a_nested_checkout_still_fails(terraform):
    (terraform / "repo/mod/bad.tf").write_text(BAD_TF + BAD_TF.replace('"b"', '"c"'))
    result = _run(terraform, TF_FMT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/mod/bad.tf" in result.output.split("\n", 1)[1]


@terraform_runs
def test_a_changed_tfmock_file_in_a_nested_checkout_still_fails(project):
    # `terraform fmt -recursive` formats mock files too, so a changed one is the project's to format.
    _checkout(project / "repo", {"main.tf": GOOD_TF, "tests/m.tfmock.hcl": GOOD_TF})
    _write(project, {"repo/tests/m.tfmock.hcl": BAD_TF})
    result = _run(project, TF_FMT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/tests/m.tfmock.hcl" in result.output.split("\n", 1)[1]
    assert "repo/main.tf" not in result.output


@terraform_runs
@pytest.mark.parametrize(
    "owned", ["infra/own.tf", "infra/own.tfvars", "tests/x.tftest.hcl", "mocks/m.tfmock.hcl", "-dash.tf"]
)
def test_a_project_owned_terraform_file_still_fails(terraform, owned):
    text = "a   =  1\n" if owned.endswith(".tfvars") else BAD_TF
    _write(terraform, {owned: text})
    result = _run(terraform, TF_FMT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert owned in result.output.split("\n", 1)[1]
    assert "repo/mod/bad.tf" not in result.output


@terraform_runs
def test_terraform_fmt_skips_hidden_paths_as_its_own_walk_does(terraform):
    # Named, terraform would check these; its `-recursive` walk never reaches them, so neither does the gate.
    _write(
        terraform, {".terraform/modules/m/h.tf": BAD_TF, "sub/.dotfile.tf": BAD_TF, ".hidden/x.tf": BAD_TF}
    )
    result = _run(terraform, TF_FMT)
    assert result.status is _gate_base.Status.PASS, result.output
    assert ".tf" in result.output and "h.tf" not in result.output and "dotfile" not in result.output


@terraform_runs
def test_terraform_fmt_with_only_unchanged_files_passes_and_leaves_no_file(project):
    _checkout(project / "repo", {"mod/bad.tf": BAD_TF})
    before = _digest(project)
    result = _run(project, TF_FMT)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "-recursive" not in result.output.splitlines()[0]
    assert _digest(project) == before


@terraform_runs
def test_terraform_fmt_without_upstreams_runs_as_written(project):
    _write(project, {"mod/bad.tf": BAD_TF})
    command = ["terraform", "fmt", "-check", "-recursive"]
    with _gate_rules.prepare_command(
        list(command), project, CONFIGS, _vendored.load(project, _gate_base._SKIP_DIRS)
    ) as (run, note, baseline):
        assert (run, note, baseline) == (command, "", False)
    result = _run(project, TF_FMT)
    assert result.status is _gate_base.Status.FAIL and result.output.startswith(shlex.join(command)), (
        result.output
    )


@terraform_runs
def test_terraform_fmt_naming_a_path_runs_as_written(terraform):
    result = _run(terraform, _command("Enforced: command terraform fmt -check -recursive repo"))
    assert result.status is _gate_base.Status.FAIL and "repo/mod/bad.tf" in result.output, result.output


@terraform_runs
def test_too_many_terraform_files_to_name_run_as_written_and_say_so(terraform, monkeypatch):
    monkeypatch.setattr(_gate_rules, "_MAX_ARGUMENTS", 4)
    result = _run(terraform, TF_FMT)
    assert result.status is _gate_base.Status.FAIL and "repo/mod/bad.tf" in result.output, result.output
    assert result.output.startswith("terraform fmt -check -recursive\n")
    assert _gate_rules._TERRAFORM_NOTE in result.output


@terraform_runs
def test_a_vendored_terraform_tree_is_judged_the_same(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(_vendored, "_ALLOWED_SCHEMES", ("https", "file"))
    monkeypatch.setattr(_vendored, "_UNAVAILABLE", {})
    upstream = tmp_path / "upstream"
    _self_made_checkout(upstream, {"bad.tf": BAD_TF, "main.tf": GOOD_TF})
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    project = tmp_path / "proj"
    shutil.copytree(upstream, project / TREE, ignore=shutil.ignore_patterns(".git"))
    commit = _git(upstream, "rev-parse", "HEAD")
    (project / "vendored.toml").write_text(
        f'[[tree]]\npath = "{TREE}"\nrepo = "{upstream.as_uri()}"\ncommit = "{commit}"\n'
    )
    result = _run(project, TF_FMT)
    assert result.status is _gate_base.Status.PASS, result.output
    assert f"{TREE}/bad.tf" not in result.output
    (project / TREE / "bad.tf").write_text(BAD_TF + "\n\n")
    result = _run(project, TF_FMT)
    assert result.status is _gate_base.Status.FAIL and f"{TREE}/bad.tf" in result.output, result.output


@pytest.mark.parametrize(
    ("command", "uses"),
    [
        (["terraform", "fmt", "-check", "-recursive"], True),
        (["terraform", "validate"], False),
        (["terraform", "init", "-backend=false"], False),
    ],
)
def test_only_terraform_fmt_depends_on_upstreams(command, uses):
    assert _gate_rules.uses_upstreams(command) is uses


def test_upstreams_are_loaded_once_across_the_rules_that_need_them(terraform, monkeypatch):
    loads = []

    def counting(project: Path, *args: Any, **kwargs: Any) -> _vendored.Upstreams:
        loads.append(project)
        return load(project, *args, **kwargs)

    load = _vendored.load
    monkeypatch.setattr(_vendored, "load", counting)
    rules = "".join(
        _command(line, f"X-{n}")
        for n, line in enumerate(
            [
                "Enforced: command shellcheck",
                "Enforced: forbid TODO in **/*.txt",
                "Enforced: command ruff check .",
                "Enforced: command terraform fmt -check -recursive",
            ],
            start=1,
        )
    )
    results = _gates.standards_results(terraform, {"engineering.md": rules}, configs=CONFIGS)
    assert {r.rule for r in results} >= {"X-1", "X-2", "X-3", "X-4"}
    assert len(loads) == 1
