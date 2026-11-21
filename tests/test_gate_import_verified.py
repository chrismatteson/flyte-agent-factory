"""An import is laid over the merge-base without overwriting it, raw; a session's import only once verified.

A session that imports an upstream copy itself (`vendored.toml` naming the whole folder, or `promote.toml`)
authors it under its own identity, so authorship cannot make it the baseline. Its files byte-identical to the
upstream commit it declares, fetched and checked as `_vendored` checks a vendored tree, are upstream's.
"""

import pytest

from flyte_agent_factory import _gate_base, _gate_import, _gate_start, _gates, _standards, _vendored
from tests.test_gate_import_base import (
    FILES,
    MESSAGE,
    OPERATOR,
    PROJECT,
    SKIP_TEST,
    commit,
    git,
    judge,
    make_clone,
    prefixed,
    project_work,
    record_base,
)

pytestmark = pytest.mark.real_factory_gates

UPSTREAM_FILES = {rel: text for rel, text in FILES.items() if rel != "promote.toml"}


def _measured(clone):
    return _gate_import.find_workspace_start(clone, "projects/proj", _gates._captured_base(clone, "proj"))


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """An upstream repository served as `file://` (the gate fetches only `https://` outside tests)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(_vendored, "_ALLOWED_SCHEMES", ("https", "file"))
    monkeypatch.setattr(_vendored, "_UNAVAILABLE", {})
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    # GitHub serves filtered fetches; a local repository must be told to.
    git(repo, "config", "uploadpack.allowFilter", "true")
    sha = commit(repo, UPSTREAM_FILES, email="up@example.invalid", message="upstream")
    return f"file://{repo}", sha


def vendored_manifest(url, sha, path="projects/proj"):
    return f'[[tree]]\npath = "{path}"\nrepo = "{url}"\ncommit = "{sha}"\n'


def session_import(clone, files, manifest_name, manifest):
    """The session commits the copy itself, under the project's identity, after the recorded base."""
    record_base(clone)
    return commit(
        clone, prefixed(files | {manifest_name: manifest}), email=PROJECT, message="import upstream"
    )


@pytest.mark.parametrize("path", ["projects/proj", "."])
def test_a_session_import_verified_against_its_vendored_upstream_is_the_baseline(tmp_path, upstream, path):
    url, sha = upstream
    origin, clone = make_clone(tmp_path)
    imported = session_import(clone, UPSTREAM_FILES, "vendored.toml", vendored_manifest(url, sha, path))
    project_work(clone)
    measured = _measured(clone)
    assert measured.import_commit == imported
    assert measured.only == {"projects/proj/src/lib.py", "projects/proj/tests/test_x.py"}
    rows = judge(clone)
    status, output = rows[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.PASS, output
    assert (
        f"+ import {imported[:12]} of projects/proj, its 2 files identical to {url} @ {sha[:12]}"
        in rows["scope"][1]
    )


def test_a_session_import_verified_against_its_promote_toml_is_the_baseline(tmp_path, upstream):
    url, sha = upstream
    origin, clone = make_clone(tmp_path)
    session_import(clone, UPSTREAM_FILES, "promote.toml", f'repo = "{url}"\nbase = "{sha}"\npath = "."\n')
    project_work(clone)
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.PASS, output


def test_a_skip_the_session_put_in_its_copy_is_the_projects(tmp_path, upstream, monkeypatch):
    url, _ = upstream
    repo = tmp_path / "upstream"
    clean = commit(
        repo, {"tests/test_x.py": "def test_auth():\n    assert True\n"}, email="up@example.invalid"
    )
    origin, clone = make_clone(tmp_path)
    session_import(clone, UPSTREAM_FILES, "vendored.toml", vendored_manifest(url, clean))
    project_work(clone)
    assert _measured(clone).only == {"projects/proj/src/lib.py"}
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL and MESSAGE in output and "tests/test_x.py" in output


@pytest.mark.parametrize(
    "change",
    [
        {"projects/proj/tests/test_x.py": SKIP_TEST + "\n\ndef test_b():\n    pytest.skip('b')\n"},
        {"projects/proj/tests/test_x.py": None},
    ],
    ids=["new-skip", "deleted"],
)
def test_a_verified_import_still_catches_the_projects_own_test_changes(tmp_path, upstream, change):
    url, sha = upstream
    origin, clone = make_clone(tmp_path)
    session_import(clone, UPSTREAM_FILES, "vendored.toml", vendored_manifest(url, sha))
    project_work(clone)
    commit(clone, change)
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL and MESSAGE in output and "tests/test_x.py" in output


def test_an_upstream_commit_off_the_default_branch_is_not_verified(tmp_path, upstream):
    url, _ = upstream
    repo = tmp_path / "upstream"
    git(repo, "checkout", "-q", "-b", "side")
    side = commit(repo, {"README.md": "side\n"}, email="up@example.invalid")
    git(repo, "checkout", "-q", "main")
    origin, clone = make_clone(tmp_path)
    session_import(clone, UPSTREAM_FILES, "vendored.toml", vendored_manifest(url, side))
    project_work(clone)
    measured = _measured(clone)
    assert measured.import_commit is None and "not in the history of the default branch" in measured.ignored
    rows = judge(clone)
    assert "import not used:" in rows["scope"][1]
    status, output = rows[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.FAIL and "tests/test_x.py" in output


def test_an_unreachable_upstream_leaves_the_merge_base(tmp_path, upstream):
    origin, clone = make_clone(tmp_path)
    missing = f"file://{tmp_path / 'nowhere'}"
    session_import(clone, UPSTREAM_FILES, "vendored.toml", vendored_manifest(missing, "b" * 40))
    project_work(clone)
    assert _measured(clone).import_commit is None
    assert judge(clone)[_standards.FAIL_TO_PASS][0] is _gate_base.Status.FAIL


def test_an_upstream_repo_the_gate_does_not_fetch_is_named_in_the_scope_record(tmp_path):
    origin, clone = make_clone(tmp_path)
    commit(clone, prefixed(FILES), email=PROJECT, message="import, as the project")
    record_base(clone)
    project_work(clone)
    assert "import not used: import" in judge(clone)["scope"][1]
    assert "'acme/widgets' is not a URL the gate fetches" in _measured(clone).ignored


@pytest.mark.parametrize("email", ["agent-factory@localhost", "factory@localhost"])
def test_the_factorys_fallback_identities_are_agents(tmp_path, email):
    origin, clone = make_clone(tmp_path)
    commit(clone, prefixed(FILES), email=email, message="import")
    record_base(clone)
    assert _measured(clone).import_commit is None


def test_an_operator_edit_on_main_after_a_copy_landed_stays_in_the_start(tmp_path):
    origin, clone = make_clone(tmp_path)
    commit(clone, prefixed(FILES), email=OPERATOR, message="import")
    record_base(clone)
    commit(origin, prefixed(FILES), email=OPERATOR, message="operator lands a copy on main")
    git(clone, "fetch", "-q", "origin")
    git(clone, "merge", "-q", "--no-edit", "origin/main", email=PROJECT)
    edited = SKIP_TEST + "\n\ndef test_ops():\n    pytest.skip('ops')\n"
    commit(
        origin, {"projects/proj/tests/test_x.py": edited}, email=OPERATOR, message="operator edits it on main"
    )
    project_work(clone)
    git(clone, "fetch", "-q", "origin")
    git(clone, "merge", "-q", "--no-edit", "origin/main", email=PROJECT)
    measured = _measured(clone)
    with _gate_start._start_tree(clone, measured.upstream, None, measured) as built:
        assert (built.path / "projects/proj/tests/test_x.py").read_text() == edited
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.PASS, output


def test_export_ignore_does_not_hide_an_imported_file_from_the_start(tmp_path):
    origin, clone = make_clone(tmp_path)
    attributes = {".gitattributes": "/tests export-ignore\n*.py filter=nothing -text\n"}
    commit(clone, prefixed(FILES | attributes), email=OPERATOR, message="import")
    record_base(clone)
    project_work(clone)
    measured = _measured(clone)
    with _gate_start._start_tree(clone, measured.upstream, None, measured) as built:
        assert (built.path / "projects/proj/tests/test_x.py").read_text() == SKIP_TEST
    status, output = judge(clone)[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.PASS, output


def test_the_overlay_keeps_modes_and_links_and_skips_a_link_out_of_the_start(tmp_path):
    origin, clone = make_clone(tmp_path)
    folder = clone / "projects/proj"
    folder.mkdir(parents=True)
    (folder / "run.sh").write_text("#!/bin/sh\n")
    (folder / "run.sh").chmod(0o755)
    (folder / "inside").symlink_to("run.sh")
    (folder / "outside").symlink_to("../../../../etc/passwd")
    git(clone, "add", "-f", "projects/proj")
    commit(clone, prefixed(FILES), email=OPERATOR, message="import")
    record_base(clone)
    measured = _measured(clone)
    with _gate_start._start_tree(clone, measured.upstream, None, measured) as built:
        start = built.path / "projects/proj"
        assert (start / "run.sh").stat().st_mode & 0o111
        assert (start / "inside").is_symlink() and (start / "inside").read_text() == "#!/bin/sh\n"
        assert not (start / "outside").is_symlink()


def test_after_a_restart_an_operator_import_is_verified_against_its_upstream(tmp_path, upstream, monkeypatch):
    url, sha = upstream
    origin, clone = make_clone(tmp_path)
    promote = f'repo = "{url}"\nbase = "{sha}"\npath = "."\n'
    commit(clone, prefixed(UPSTREAM_FILES | {"promote.toml": promote}), email=OPERATOR, message="import")
    record_base(clone)
    project_work(clone)
    monkeypatch.setattr(_gates, "_CAPTURED_BASES", {})  # a supervisor restart: only the file is left
    measured = _measured(clone)
    assert measured.import_commit is not None and measured.only is not None
    assert "projects/proj/tests/test_x.py" in measured.only
    assert "projects/proj/promote.toml" in measured.only
    rows = judge(clone)
    status, output = rows[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.PASS, output
    assert rows["scope"][0] is _gate_base.Status.PASS, rows["scope"][1]
    assert f"its 2 files identical to {url} @ {sha[:12]}, with its own promote.toml" in rows["scope"][1]


@pytest.mark.parametrize("with_copy", [True, False], ids=["with-upstream-copy", "promote-toml-only"])
def test_a_promote_toml_the_session_wrote_is_still_charged_by_scope(tmp_path, upstream, with_copy):
    """The tool's files are the tool's only when the tool (no agent identity) made the import."""
    url, sha = upstream
    origin, clone = make_clone(tmp_path)
    record_base(clone)  # the supervisor's in-process base is intact: no restart
    promote = f'repo = "{url}"\nbase = "{sha}"\npath = "."\n'
    files = (UPSTREAM_FILES if with_copy else {}) | {"promote.toml": promote}
    commit(clone, prefixed(files), email=PROJECT, message="the session writes promote.toml itself")
    project_work(clone)
    if with_copy:
        assert _measured(clone).only == {"projects/proj/src/lib.py", "projects/proj/tests/test_x.py"}
    status, output = judge(clone)["scope"]
    assert status is _gate_base.Status.FAIL and "projects/proj/promote.toml" in output, output
    assert "with its own" not in output, output


@pytest.mark.parametrize("rewrite", [False, True], ids=["file-intact", "file-rewritten"])
def test_an_operator_looking_copy_after_the_base_keeps_its_promote_toml_charged(tmp_path, upstream, rewrite):
    """With the supervisor's record intact, a copy committed after the base is verified, never trusted whole:
    its promote.toml stays the project's whatever mail it carries, even with the base-commit file rewritten.
    """
    url, sha = upstream
    origin, clone = make_clone(tmp_path)
    record_base(clone)  # the supervisor's in-process base is intact: no restart
    promote = f'repo = "{url}"\nbase = "{sha}"\npath = "."\n'
    commit(
        clone, prefixed(UPSTREAM_FILES | {"promote.toml": promote}), email=OPERATOR, message="session copy"
    )
    project_work(clone)
    if rewrite:
        (clone / ".claude-flyte/proj/base-commit").write_text(git(clone, "rev-parse", "HEAD") + "\n")
    measured = _measured(clone)
    assert measured.harness == frozenset()
    assert measured.only == {"projects/proj/src/lib.py", "projects/proj/tests/test_x.py"}
    rows = judge(clone)
    if rewrite:
        status, output = rows["scope"]
        assert status is _gate_base.Status.FAIL and "projects/proj/promote.toml" in output, output
        assert "with its own" not in output, output
    else:
        assert rows["attribution"][0] is _gate_base.Status.FAIL, rows["attribution"][1]


def test_an_imported_path_that_is_not_utf8_does_not_crash_the_gates(tmp_path):
    origin, clone = make_clone(tmp_path)
    folder = clone / "projects/proj"
    folder.mkdir(parents=True)
    (folder / b"caf\xe9.py".decode(errors="surrogateescape")).write_text("x = 1\n")
    git(clone, "add", "-f", "projects/proj")
    commit(clone, prefixed(FILES), email=OPERATOR, message="import")
    record_base(clone)
    project_work(clone)
    rows = judge(clone)
    status, output = rows[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.PASS, output


def test_a_file_main_has_where_the_import_has_a_directory_stays_and_does_not_crash(tmp_path):
    origin, clone = make_clone(tmp_path)
    commit(origin, {"projects/proj/tests": "placeholder\n"}, email=OPERATOR, message="main file")
    git(clone, "fetch", "-q", "origin")
    git(clone, "merge", "-q", "--ff-only", "origin/main")
    commit(clone, prefixed({"tests": None}) | prefixed(FILES), email=OPERATOR, message="import")
    record_base(clone)
    project_work(clone)
    measured = _measured(clone)
    with _gate_start._start_tree(clone, measured.upstream, None, measured) as built:
        assert (built.path / "projects/proj/tests").read_text() == "placeholder\n"
        assert (built.path / "projects/proj/src/lib.py").is_file()
    rows = judge(clone)
    assert rows["scope"][0] is _gate_base.Status.PASS, rows["scope"][1]


NEWER_FILES = UPSTREAM_FILES | {"src/lib.py": "def one():\n    return 1  # v3\n", "docs.md": "d\n"}


@pytest.mark.parametrize("order", ["older-then-newer", "newer-then-older"])
def test_two_imports_of_one_upstream_at_different_commits_are_both_verified(tmp_path, upstream, order):
    """Every engine project names the same upstream: the gate verifies any commit of it, in any order."""
    url, older = upstream
    repo = tmp_path / "upstream"
    commit(repo, {"src/lib.py": "def one():\n    return 1  # v2\n"}, email="up@example.invalid", message="v2")
    newer = commit(repo, NEWER_FILES, email="up@example.invalid", message="v3")
    commit(repo, {"README.md": "head\n"}, email="up@example.invalid", message="head")
    imports = {"older": (older, UPSTREAM_FILES), "newer": (newer, NEWER_FILES)}
    seen = []
    for which in order.split("-then-"):
        sha, files = imports[which]
        (tmp_path / which).mkdir()
        origin, clone = make_clone(tmp_path / which)
        session_import(clone, files, "vendored.toml", vendored_manifest(url, sha))
        project_work(clone)
        seen.append((which, _measured(clone).describe(), judge(clone)[_standards.FAIL_TO_PASS]))
    for which, described, (status, output) in seen:
        assert "import not used" not in described, (which, described)
        assert status is _gate_base.Status.PASS, (which, output)
