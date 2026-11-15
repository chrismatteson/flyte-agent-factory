"""The slow pytest lane: local by default, a child action with its own CPUs when asked."""

import asyncio
import dataclasses
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

import flyte
import flyte.errors
import pytest
from flyte.extras import ContainerTask

from flyte_agent_factory import _images, _scanners, _slow_lane, child


def _suites(path: Path) -> list[str]:
    """The `tests` attribute of each testsuite in a junit file the lane wrote (shards are merged into one)."""
    return [suite.get("tests", "") for suite in ElementTree.parse(path).getroot().iter("testsuite")]


def _fake_project(tmp_path: Path, exit_code: int = 0) -> Path:
    """A project whose `pytest` is a module that records its argv and writes a junit file."""
    (tmp_path / "pytest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "Path('argv.txt').write_text(' '.join(argv))\n"
        "for a in argv:\n"
        "    if a.startswith('--junitxml='):\n"
        "        Path(a.split('=', 1)[1]).write_text('<testsuite/>')\n"
        f"print('local pytest ran'); sys.exit({exit_code})\n"
    )
    return tmp_path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def no_run(monkeypatch):
    """Nothing is attachable: `attach()` raises as it does outside a Flyte run."""

    def refuse():
        raise RuntimeError("no run context")

    monkeypatch.setattr(_slow_lane, "_attach", refuse)


def test_the_run_context_is_found_at_the_workspace_root_above_the_project(tmp_path, monkeypatch):
    """The checks run in `projects/<name>/`; the supervisor writes the run context at the workspace root."""
    project = tmp_path / "projects" / "one"
    project.mkdir(parents=True)
    (tmp_path / ".flyte").mkdir()
    (tmp_path / ".flyte" / "run_context.json").write_text("{}")
    monkeypatch.delenv("AGENT_FACTORY_WORKSPACE", raising=False)
    seen: list[Any] = []
    monkeypatch.setattr(child, "attach", seen.append)
    monkeypatch.chdir(project)
    _slow_lane._attach()
    assert seen == [tmp_path]


def test_the_run_context_is_found_through_the_workspace_variable(tmp_path, monkeypatch):
    workspace, elsewhere = tmp_path / "ws", tmp_path / "elsewhere"
    (workspace / ".flyte").mkdir(parents=True)
    (workspace / ".flyte" / "run_context.json").write_text("{}")
    elsewhere.mkdir()
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(workspace))
    assert _slow_lane._run_context_dir(elsewhere) == workspace
    monkeypatch.delenv("AGENT_FACTORY_WORKSPACE")
    assert _slow_lane._run_context_dir(elsewhere) is None, (
        "with no context anywhere, attach() decides as before"
    )


def test_runs_locally_without_the_env_var(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("AGENT_FACTORY_SLOW_LANE", raising=False)
    junit = tmp_path / "slow.xml"

    code = _slow_lane.main(["--junit", str(junit), "-x"], submit=cast(Any, pytest.fail))

    assert code == 0
    assert junit.read_text() == "<testsuite/>"
    argv = (tmp_path / "argv.txt").read_text().split()
    assert argv[:5] == ["tests", "-q", "-m", "slow", f"--junitxml={junit}"] and argv[-1] == "-x"
    assert "local pytest ran" in capfd.readouterr().out


def test_local_exit_code_is_propagated(tmp_path, monkeypatch):
    monkeypatch.chdir(_fake_project(tmp_path, exit_code=1))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("AGENT_FACTORY_SLOW_LANE", raising=False)
    assert _slow_lane.main(["--junit", str(tmp_path / "slow.xml")]) == 1


def test_no_attachable_run_falls_back_locally_and_says_so(tmp_path, monkeypatch, capfd, no_run):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=cast(Any, pytest.fail))

    assert code == 0
    captured = capfd.readouterr()
    assert "slow lane: running locally" in captured.err and "no run context" in captured.err
    assert "local pytest ran" in captured.out


def test_remote_result_is_written_and_its_exit_code_propagated(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    seen = {}

    def submit(args, interruptible):
        seen["args"], seen["interruptible"] = args, interruptible
        return 3, "remote tail\n", "<testsuite tests='1'/>"

    junit = tmp_path / "out" / "slow.xml"
    code = _slow_lane.main(["--junit", str(junit), "-k", "tf"], submit=submit)

    assert code == 3
    assert _suites(junit) == ["1"]
    assert "remote tail" in capsys.readouterr().out
    assert seen == {"args": ["-k", "tf"], "interruptible": True}


def test_a_spot_queue_timeout_falls_back_to_a_local_run(
    tmp_path, monkeypatch, capfd, local_pytest_must_not_run
):
    """Since #209 an attached lane never runs on the pod: spot, then on-demand, then capacity (69)."""
    # Before sharding this fell back to a local run on the 4-CPU pod, which overran the pre-review's limits.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    attempts = []

    def submit(args, interruptible):
        attempts.append(interruptible)
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)

    assert attempts == [True, False]  # spot, then on-demand, then no local run
    captured = capfd.readouterr()
    assert code == _slow_lane.CAPACITY and "no spot capacity" in captured.err
    assert "no capacity: 1 of 1 shards" in captured.err


def test_no_local_leaves_an_unplaced_lane_to_the_caller(
    tmp_path, monkeypatch, capfd, local_pytest_must_not_run
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    code = _slow_lane.main(["--no-local", "--junit", str(tmp_path / "slow.xml")], submit=submit)

    assert code == _slow_lane.CAPACITY and "no spot capacity" in capfd.readouterr().err


def test_no_local_without_an_attachable_run_is_left_to_the_caller(tmp_path, monkeypatch, capfd, no_run):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")

    code = _slow_lane.main(["--no-local"], submit=cast(Any, pytest.fail))

    assert code == _slow_lane.NOT_RUN
    assert "local pytest ran" not in capfd.readouterr().out


def test_a_failure_other_than_queueing_is_not_retried_elsewhere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        raise RuntimeError("task crashed")

    with pytest.raises(RuntimeError, match="task crashed"):
        _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)


def test_a_child_action_that_fails_before_reporting_runs_locally(
    tmp_path, monkeypatch, capfd, local_pytest_must_not_run
):
    """Since #209 such a child is retried once on demand, never run on the pod; twice is capacity (69)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    attempts = []

    def submit(args, interruptible):
        attempts.append(interruptible)
        if interruptible:
            raise flyte.errors.RuntimeUserError("TaskInterruptedError", "spot node reclaimed")
        return 3, "retry tail\n", "<testsuite/>"

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)

    assert attempts == [True, False] and code == 3  # pytest's own code, from the retry
    assert "the child action failed before reporting (RuntimeUserError:" in capfd.readouterr().err


def test_the_spot_wait_is_bounded_short_and_the_request_fits_a_spot_node():
    assert _slow_lane.QUEUE_BOUND == timedelta(minutes=2)
    assert _slow_lane.ON_DEMAND_QUEUE_BOUND == timedelta(minutes=3)
    assert _slow_lane.task.resources is not None
    # 2 CPUs placed in 5 s, 4 in 12 s and 8 in 160 s on spot; 8 was the size before sharding.
    assert _slow_lane.task.resources.cpu == ("1500m", 2)


def test_tree_tar_has_tracked_and_untracked_files_but_not_ignored_or_vcs(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (tmp_path / "tracked.py").write_text("t")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.py").write_text("n")  # untracked, not ignored
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("v")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("c")
    _git(tmp_path, "add", ".gitignore", "tracked.py")
    (tmp_path / "gone.py").write_text("g")
    _git(tmp_path, "add", "gone.py")
    (tmp_path / "gone.py").unlink()  # tracked but deleted in the working tree

    data = _slow_lane.pack_tree(tmp_path)

    names = set(tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").getnames())
    assert names == {".gitignore", "tracked.py", "sub/new.py"}


def test_tree_tar_never_carries_dot_git_or_venv_even_if_listed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("c")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "a.py").write_text("a")
    (tmp_path / "ok.py").write_text("o")
    monkeypatch.setattr(_slow_lane, "_listed_files", lambda root: [".git/config", ".venv/a.py", "ok.py"])

    data = _slow_lane.pack_tree(tmp_path)

    assert tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").getnames() == ["ok.py"]


def _committed_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository with one commit, then an uncommitted change and an untracked file in its working tree."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "src").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (repo / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")
    (repo / "a.txt").write_text("committed\n")
    (repo / "tests" / "test_git.py").write_text(
        "import os, subprocess\n\n"
        "def _git(*args):\n"
        "    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout\n\n"
        "def test_git_sees_head_and_the_working_tree():\n"
        "    report = os.path.join(os.environ['TMPDIR'], 'git-report.txt')\n"
        "    with open(report, 'w') as f:\n"
        "        f.write(_git('rev-parse', 'HEAD') + _git('status', '--porcelain'))\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "one")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("untracked\n")
    return repo, sha


def test_history_is_a_bundle_of_head(tmp_path):
    repo, sha = _committed_repo(tmp_path)
    bundle = tmp_path / "history"
    bundle.write_bytes(_slow_lane.pack_history(repo))

    heads = subprocess.run(
        ["git", "-C", str(repo), "bundle", "list-heads", str(bundle)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert sha in heads


def test_a_repository_without_commits_has_no_history(tmp_path):
    _git(tmp_path, "init", "-q")
    assert _slow_lane.pack_history(tmp_path) == b""


def test_a_subdirectory_project_has_no_history_of_its_own(tmp_path):
    # HEAD of the enclosing repository holds other paths: the container commits the packed tree instead.
    repo, _ = _committed_repo(tmp_path)
    sub = repo / "projects" / "p"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "p")

    assert _slow_lane.pack_history(sub) == b""
    assert _slow_lane.pack_history(repo) != b""


def test_the_lane_task_takes_the_history_beside_the_tree():
    from flyte.io import File

    inputs = _slow_lane.task._inputs
    assert inputs is not None
    assert inputs["tree"] is File and inputs["history"] is File and inputs["mode"] is str


def test_unpacked_tree_runs_pytest_with_the_slow_marker(tmp_path):
    """The task body, minus the cluster: unpack, run the lane, return code, tail and junit."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "import pytest\n\n@pytest.mark.slow\ndef test_a():\n    pass\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")

    code, tail, junit = _slow_lane.run_in_tree(tmp_path, ["-p", "no:cacheprovider"], python=sys.executable)

    assert code == 0, tail
    assert "<testsuite" in junit


# The pytest lanes as a raw-container task: its command script, run here with bash against a tiny tree.


def _lane_tree(tmp_path: Path, tests: dict[str, str]) -> Path:
    """Pack a tree whose `uv sync` (faked) makes a `.venv/bin/python` that is this test process's own."""
    tree = tmp_path / "src-tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "src").mkdir()
    (tree / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")
    for name, body in tests.items():
        (tree / "tests" / name).write_text(body)
    inputs = tmp_path / "in"
    inputs.mkdir()
    with tarfile.open(inputs / "tree", "w:gz") as tar:
        for path in sorted(tree.rglob("*")):
            tar.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)
    return inputs


def _fake_bin(tmp_path: Path, terraform: bool) -> Path:
    """A `uv` that logs its arguments and builds the venv, and optionally a `terraform` already on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f'#!/bin/bash\necho "$*" >> {tmp_path / "uv.log"}\n'
        "mkdir -p .venv/bin\n"
        f"printf '#!/bin/bash\\nexec {sys.executable} \"$@\"\\n' > .venv/bin/python\n"
        "chmod +x .venv/bin/python\n"
    )
    uv.chmod(0o755)
    if terraform:
        (bin_dir / "terraform").write_text("#!/bin/bash\n")
        (bin_dir / "terraform").chmod(0o755)
    return bin_dir


def _run_lane(
    tmp_path: Path,
    inputs: Path,
    args: list[str],
    terraform: bool = True,
    zip_url: str = "file:///nonexistent",
    sha: str = "0" * 64,
    extra_env: dict[str, str] | None = None,
) -> Path:
    (inputs / "pytest_args").write_text(json.dumps(args))
    out = tmp_path / "out"
    # Without `terraform` the PATH must not hold a real one either, as a host that has it installed does.
    path = [
        d for d in os.environ["PATH"].split(os.pathsep) if terraform or not (Path(d) / "terraform").exists()
    ]
    env = {
        **os.environ,
        "PATH": os.pathsep.join([str(_fake_bin(tmp_path, terraform)), *path]),
        "TMPDIR": str(tmp_path),
        **(extra_env or {}),
    }
    cmd = [*_slow_lane.LANE_COMMAND, str(inputs), str(out), zip_url, sha, "12000"]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr  # the script always reports through its output files
    return out


def test_the_lane_script_reports_a_passing_run(tmp_path):
    inputs = _lane_tree(tmp_path, {"test_a.py": "def test_a():\n    pass\n"})

    out = _run_lane(tmp_path, inputs, ["-q"])

    assert (out / "code").read_text() == "0"
    assert "1 passed" in (out / "tail").read_text()
    assert "<testsuite" in (out / "junit").read_text()
    assert "sync --frozen" in (tmp_path / "uv.log").read_text()


def test_the_lane_script_reports_a_failing_run_and_passes_the_arguments_through(tmp_path):
    inputs = _lane_tree(
        tmp_path,
        {
            "test_a.py": "def test_a():\n    assert False\n",
            "test_b.py": "def test_b():\n    pass\n",
        },
    )

    out = _run_lane(tmp_path, inputs, ["-k", "test_a or test_b", "-m", "not slow"])

    assert (out / "code").read_text() == "1"
    assert "1 failed, 1 passed" in (out / "tail").read_text()
    assert 'failures="1"' in (out / "junit").read_text()


def test_the_lane_script_runs_only_its_shards_files_through_the_selection_plugin(tmp_path):
    from flyte_agent_factory import _lane_shards

    inputs = _lane_tree(
        tmp_path,
        {"test_a.py": "def test_a():\n    pass\n", "test_b.py": "def test_b():\n    assert False\n"},
    )
    shards = [["tests/test_b.py"], ["tests/test_a.py"]]

    out = _run_lane(tmp_path, inputs, _lane_shards.build_shard_args(shards)[1])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert "1 passed" in (out / "tail").read_text()  # xdist's summary leaves the deselected count out
    junit = (out / "junit").read_text()
    assert 'name="test_a"' in junit and 'name="test_b"' not in junit


def test_the_lane_script_restores_head_so_git_sees_the_same_tree_as_the_pod(tmp_path):
    """Tests that read git (tracked files, HEAD) failed in the child: the tree tar carries no `.git`."""
    repo, sha = _committed_repo(tmp_path)
    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / "tree").write_bytes(_slow_lane.pack_tree(repo))
    (inputs / "history").write_bytes(_slow_lane.pack_history(repo))

    out = _run_lane(tmp_path, inputs, ["-q"])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert (tmp_path / "git-report.txt").read_text() == f"{sha}\n M a.txt\n?? b.txt\n"


def _command_lane(tmp_path: Path, command: list[str], lock: bool) -> Path:
    inputs = _lane_tree(tmp_path, {})
    if lock:
        (tmp_path / "src-tree" / "uv.lock").write_text("")
        with tarfile.open(inputs / "tree", "w:gz") as tar:
            for path in sorted((tmp_path / "src-tree").rglob("*")):
                tar.add(path, arcname=path.relative_to(tmp_path / "src-tree").as_posix(), recursive=False)
    (inputs / "pytest_args").write_text(json.dumps(command))
    out = tmp_path / "out"
    env = {**os.environ, "PATH": f"{_fake_bin(tmp_path, True)}{os.pathsep}{os.environ['PATH']}"}
    env["TMPDIR"] = str(tmp_path)
    (inputs / "mode").write_text("command")
    cmd = [*_slow_lane.LANE_COMMAND, str(inputs), str(out), "file:///nonexistent", "0" * 64, "12000"]
    assert subprocess.run(cmd, env=env, capture_output=True, text=True).returncode == 0
    return out


def test_the_lane_script_runs_a_command_and_reports_its_exit_code(tmp_path):
    command = ["bash", "-c", 'echo "$AGENT_FACTORY_LANE_JUNIT" > "$PWD/../junit-path"; echo hi; exit 3']
    out = _command_lane(tmp_path, command, lock=False)

    assert (out / "code").read_text() == "3"
    assert "hi" in (out / "tail").read_text()
    assert not (tmp_path / "uv.log").exists(), "no uv.lock: nothing to sync"


def test_the_lane_script_syncs_a_command_project_that_has_a_lock(tmp_path):
    out = _command_lane(tmp_path, ["bash", "-c", "command -v python"], lock=True)

    assert (out / "code").read_text() == "0"
    assert "sync --frozen" in (tmp_path / "uv.log").read_text()
    assert "/.venv/bin/python" in (out / "tail").read_text()


def test_the_lane_script_keeps_only_the_tail_of_a_long_output(tmp_path):
    inputs = _lane_tree(tmp_path, {"test_a.py": "def test_a():\n    print('x' * 50000)\n    assert False\n"})

    out = _run_lane(tmp_path, inputs, [])

    assert len((out / "tail").read_bytes()) <= 12000


def test_the_lane_script_installs_a_pinned_terraform_that_is_not_on_path(tmp_path):
    zip_path = tmp_path / "terraform.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("terraform", "#!/bin/bash\necho fake-terraform\n")
    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    runs_terraform = "import subprocess\n\ndef test_tf():\n    subprocess.run(['terraform'], check=True)\n"
    inputs = _lane_tree(tmp_path, {"test_tf.py": runs_terraform})

    out = _run_lane(tmp_path, inputs, [], terraform=False, zip_url=zip_path.as_uri(), sha=sha)

    assert (out / "code").read_text() == "0", (out / "tail").read_text()


def test_the_lane_script_refuses_a_terraform_whose_checksum_differs(tmp_path):
    zip_path = tmp_path / "terraform.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("terraform", "#!/bin/bash\n")
    inputs = _lane_tree(tmp_path, {"test_a.py": "def test_a():\n    pass\n"})

    out = _run_lane(tmp_path, inputs, [], terraform=False, zip_url=zip_path.as_uri())

    assert (out / "code").read_text() == str(_slow_lane.SETUP_FAILED)
    assert "checksum" in (out / "tail").read_text()
    assert (out / "junit").exists()  # an output the task declares is always there


def test_the_lane_runs_the_baked_scanners_with_no_network_and_no_fallback(tmp_path, monkeypatch):
    """The image bakes gitleaks and tflint: the lane uses them as found, and fetches nothing for them."""
    baked = tmp_path / "baked"
    baked.mkdir()
    for scanner in _scanners.SCANNERS.values():
        (baked / scanner.name).write_text(f"#!/bin/bash\necho baked-{scanner.name}\n")
        (baked / scanner.name).chmod(0o755)
    # Every proxied request goes to a closed port, and the release hosts resolve nowhere.
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    monkeypatch.setenv("PATH", os.pathsep.join([str(baked), os.environ["PATH"]]))
    runs_both = (
        "import subprocess\n\n"
        "def test_scanners():\n"
        "    for tool in ('gitleaks', 'tflint'):\n"
        "        out = subprocess.run([tool], capture_output=True, text=True, check=True).stdout\n"
        "        assert out == f'baked-{tool}\\n'\n"
    )
    inputs = _lane_tree(tmp_path, {"test_scanners.py": runs_both})

    out = _run_lane(tmp_path, inputs, [])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert "1 passed" in (out / "tail").read_text()


def test_no_lane_or_gate_module_downloads_a_scanner():
    package = Path(_slow_lane.__file__).parent
    modules = [package / "_slow_lane.py", *package.glob("_gate*.py")]
    assert len(modules) > 2
    for module in modules:
        text = module.read_text()
        for scanner in _scanners.SCANNERS.values():
            assert scanner.url not in text and f"{scanner.name}/releases" not in text, module.name


def test_the_lane_task_is_a_raw_container_registered_in_its_own_environment():
    task = _slow_lane.task
    assert isinstance(task, ContainerTask)
    assert task.name == "test-lane"
    assert task._cmd == _slow_lane.LANE_COMMAND  # no public accessor for the command
    assert task.resources == flyte.Resources(cpu=("1500m", 2), memory=("3Gi", "5Gi"))
    assert task.interruptible is True  # `.override()` drops a ContainerTask's command: set at construction
    assert task.timeout == flyte.Timeout(
        max_queued_time=_slow_lane.QUEUE_BOUND, max_runtime=timedelta(minutes=30)
    )
    assert _slow_lane.env.name == "test-lanes"
    assert list(_slow_lane.env.tasks) == ["test-lane"]


def test_the_on_demand_lane_task_is_the_same_container_off_spot():
    """`.override()` drops a ContainerTask's command, so on-demand is a second task, in its own env."""
    spot, on_demand = _slow_lane.task, _slow_lane.task_on_demand
    assert isinstance(on_demand, ContainerTask)
    assert on_demand.name == "test-lane-on-demand"
    assert on_demand._cmd == spot._cmd and on_demand.image == spot.image
    assert on_demand.resources == spot.resources
    assert isinstance(spot.timeout, flyte.Timeout)
    assert on_demand.timeout == flyte.Timeout(
        max_queued_time=_slow_lane.ON_DEMAND_QUEUE_BOUND, max_runtime=spot.timeout.max_runtime
    )
    assert on_demand.interruptible is False
    assert list(_slow_lane.env_on_demand.tasks) == ["test-lane-on-demand"]


def test_with_on_demand_a_spot_queue_timeout_tries_on_demand_before_the_local_run(
    tmp_path, monkeypatch, capfd
):
    """Since #209 there is no local run: a spot timeout tries on-demand, and its result stands."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    attempts = []

    def submit(args, interruptible):
        attempts.append(interruptible)
        if interruptible:
            raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
        return 0, "on-demand tail\n", "<testsuite/>"

    code = _slow_lane.main(
        ["--on-demand", "--no-local", "--junit", str(tmp_path / "slow.xml")], submit=submit
    )

    assert attempts == [True, False]
    assert code == 0 and _suites(tmp_path / "slow.xml") == [""]
    assert "on-demand tail" in capfd.readouterr().out


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_with_on_demand_both_queue_timeouts_leave_the_lane_unplaced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    assert _slow_lane.main(["--on-demand", "--no-local"], submit=submit) == _slow_lane.CAPACITY


def test_the_pinned_terraform_is_the_one_the_benchmark_image_installs():
    assert _slow_lane.TERRAFORM_VERSION == _images.TERRAFORM_VERSION
    assert _slow_lane.TERRAFORM_SHA256 == _images.TERRAFORM_SHA256
    assert _slow_lane.TERRAFORM_URL.endswith(
        f"/terraform/{_images.TERRAFORM_VERSION}/terraform_{_images.TERRAFORM_VERSION}_linux_amd64.zip"
    )


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_a_lane_whose_setup_failed_runs_locally_instead(tmp_path, monkeypatch, capfd):
    """Since #209 a failed setup is retried once on demand, then is SETUP_UNAVAILABLE (78), not run here."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    attempts = []

    def submit(args, interruptible):
        attempts.append(interruptible)
        return _slow_lane.SETUP_FAILED, "uv: no network\n", ""

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)

    assert attempts == [True, False] and code == _slow_lane.SETUP_UNAVAILABLE
    captured = capfd.readouterr()
    assert "setup failed" in captured.err and "uv: no network" in captured.err
    assert not (tmp_path / "slow.xml.capacity").exists()


def test_only_the_exit_code_is_a_scalar_output():
    """A raw container's scalar output is capped at 1 KiB; a 12,000-character tail failed each lane."""
    from flyte.io import File

    outputs = _slow_lane.task._outputs
    assert outputs is not None
    assert outputs["code"] is str
    assert outputs["tail"] is File and outputs["junit"] is File


def test_a_refused_tree_upload_is_retried_with_a_fresh_url(tmp_path, monkeypatch, capsys):
    # A presigned URL can be refused (HTTP 400) or expire; each attempt asks for a new one, so the lane still
    # reaches its child rather than falling back to the shared pod.
    calls: list[Path] = []

    async def upload(path: Path) -> tuple[str, str]:
        calls.append(path)
        if len(calls) < 3:
            raise flyte.errors.RuntimeSystemError("HTTPStatusError", "Client error '400 Bad Request'")
        return "md5", "s3://bucket/tree.tar.gz"

    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    tarball = tmp_path / "tree.tar.gz"
    tarball.write_bytes(b"tree")
    uri = asyncio.run(_slow_lane._upload_tree(tarball, upload=upload, sleep=sleep))
    assert uri == "s3://bucket/tree.tar.gz"
    assert calls == [tarball] * 3 and len(waits) == 2 and all(w > 0 for w in waits)
    assert capsys.readouterr().err.count("upload of the tree failed") == 2


def test_a_tree_upload_that_keeps_failing_raises_after_its_attempts(tmp_path):
    calls: list[Path] = []

    async def upload(path: Path) -> tuple[str, str]:
        calls.append(path)
        raise flyte.errors.RuntimeSystemError("HTTPStatusError", "Client error '400 Bad Request'")

    async def sleep(seconds: float) -> None:
        return None

    tarball = tmp_path / "tree.tar.gz"
    tarball.write_bytes(b"tree")
    with pytest.raises(flyte.errors.RuntimeSystemError):
        asyncio.run(_slow_lane._upload_tree(tarball, upload=upload, sleep=sleep))
    assert len(calls) == _slow_lane.UPLOAD_ATTEMPTS > 1


def test_the_lane_script_gives_the_tree_a_repository_tracking_exactly_what_was_packed(tmp_path):
    # The packed tree has no `.git`; tests that read the repository (`git ls-files`, `git archive HEAD`) need
    # one, tracking the packed files, including one tracked in the checkout despite an ignore rule.
    probe = (
        "import subprocess\n"
        "def git(*a):\n"
        "    return subprocess.run(['git', *a], check=True, capture_output=True, text=True).stdout\n"
        "def test_the_repository():\n"
        "    files = set(git('ls-files').split())\n"
        "    assert files == {'.gitignore', 'dist/skill.zip', 'pytest.ini', 'tests/test_git.py'}, files\n"
        "    assert len(git('rev-list', '--all').split()) == 1\n"
        "    assert git('check-ignore', '--no-index', 'dist/skill.zip').strip() == 'dist/skill.zip'\n"
    )
    inputs = tmp_path / "in"
    inputs.mkdir()
    tree = tmp_path / "src-tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "dist").mkdir()
    (tree / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")
    (tree / ".gitignore").write_text("dist/\n.venv/\n")
    (tree / "dist" / "skill.zip").write_bytes(b"generated")
    (tree / "tests" / "test_git.py").write_text(probe)
    with tarfile.open(inputs / "tree", "w:gz") as tar:
        for path in sorted(tree.rglob("*")):
            tar.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)

    out = _run_lane(tmp_path, inputs, ["-q", "-m", "not slow"])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert "1 passed" in (out / "tail").read_text()


def _gitleaks_tarball(tmp_path: Path) -> tuple[str, str]:
    """A release-shaped gitleaks archive (the binary at its root) and its sha256."""
    binary = tmp_path / "gitleaks-bin"
    binary.write_text("#!/bin/bash\necho fake-gitleaks\n")
    binary.chmod(0o755)
    archive = tmp_path / "gitleaks.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="gitleaks")
    return archive.as_uri(), hashlib.sha256(archive.read_bytes()).hexdigest()


def test_the_lane_script_installs_a_pinned_gitleaks_that_is_not_on_path(tmp_path):
    """The installer unpacks a gitleaks archive that verifies against its pin into an executable binary."""
    url, sha = _gitleaks_tarball(tmp_path)
    scanner = dataclasses.replace(_scanners.GITLEAKS, url=url, sha256=sha)
    binary = _scanners.install(
        scanner,
        tmp_path / "cache",
        tmp_path / "installed",
        fetch=lambda _: (tmp_path / scanner.archive).read_bytes(),
    )

    result = subprocess.run([binary], check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout == "fake-gitleaks\n"


def test_the_lane_script_refuses_a_gitleaks_whose_checksum_differs(tmp_path):
    """A gitleaks archive that does not match its pinned sha256 fails the install and leaves no binary."""
    url, _ = _gitleaks_tarball(tmp_path)
    scanner = dataclasses.replace(_scanners.GITLEAKS, url=url, sha256="0" * 64)

    with pytest.raises(_scanners.Unavailable, match="checksum"):
        _scanners.install(
            scanner,
            tmp_path / "cache",
            tmp_path / "installed",
            fetch=lambda _: (tmp_path / scanner.archive).read_bytes(),
            sleep=lambda _: None,
        )
    assert not (tmp_path / "installed" / scanner.name).exists()


def test_the_lane_installs_the_gitleaks_ci_pins():
    """The image carries gitleaks at the pin; the lane task's arguments hold no scanner URL or checksum."""
    scanner = _scanners.GITLEAKS
    assert scanner.url in _images.TOOLS[scanner.name]
    assert scanner.sha256 in _images.TOOLS[scanner.name]
    arguments = list(_slow_lane.task._args or [])
    assert arguments == [
        "/var/inputs",
        "/var/outputs",
        _slow_lane.TERRAFORM_URL,
        _slow_lane.TERRAFORM_SHA256,
        str(_slow_lane.TAIL_CHARS),
    ]


def test_exhausted_upload_retries_fail_the_lane_without_a_local_run(monkeypatch):
    # A tree that could not be uploaded is a failed lane, not an unplaced one: neither the caller (75) nor
    # this process may then run it on the shared pod.
    monkeypatch.setenv(_slow_lane.ENV_VAR, "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def no_local(junit, pytest_args):
        pytest.fail("the lane ran locally")

    monkeypatch.setattr(_slow_lane, "_run_local", no_local)

    async def upload(path: Path) -> tuple[str, str]:
        raise flyte.errors.RuntimeSystemError("UploadFailed", "HTTP 400")

    async def sleep(seconds: float) -> None:
        return None

    def submit(pytest_args: list[str], interruptible: bool) -> tuple[int, str, str]:
        asyncio.run(_slow_lane._upload_tree(Path("unused"), upload=upload, sleep=sleep))
        raise AssertionError("the upload cannot succeed")

    assert _slow_lane.main(["--no-local", "--on-demand"], submit=submit) == _slow_lane.UPLOAD_FAILED
    assert _slow_lane.main(["--on-demand"], submit=submit) == _slow_lane.UPLOAD_FAILED
    assert _slow_lane.UPLOAD_FAILED not in {0, 1, 2, 3, 4, 5, _slow_lane.SETUP_FAILED, _slow_lane.NOT_RUN}


@pytest.fixture
def local_pytest_must_not_run(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("pytest ran locally")

    monkeypatch.setattr(_slow_lane, "_run_local", refuse)


def _no_local(tmp_path, monkeypatch, submit, capfd, attach=lambda: None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", attach)
    code = _slow_lane.main(["--no-local", "--junit", str(tmp_path / "slow.xml")], submit=submit)
    return code, capfd.readouterr().err


def test_not_run_is_a_code_pytest_never_uses():
    assert _slow_lane.NOT_RUN not in range(0, 6) and _slow_lane.NOT_RUN != _slow_lane.SETUP_FAILED


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_no_run_can_be_attached(tmp_path, monkeypatch, capfd):
    def refuse():
        raise RuntimeError("no run context")

    code, err = _no_local(tmp_path, monkeypatch, cast(Any, pytest.fail), capfd, attach=refuse)

    assert code == _slow_lane.NOT_RUN and "no run context" in err


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_spot_capacity_is_missing(tmp_path, monkeypatch, capfd):
    """Since #209 a lane not placed on spot or on-demand reports capacity (69), not NOT_RUN."""

    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    code, err = _no_local(tmp_path, monkeypatch, submit, capfd)

    assert code == _slow_lane.CAPACITY and "no spot capacity" in err


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_the_child_fails_before_reporting(tmp_path, monkeypatch, capfd):
    """Since #209 a child that fails before reporting, twice, reports capacity (69), not NOT_RUN."""

    def submit(args, interruptible):
        raise flyte.errors.RuntimeUserError("ModuleNotFoundError", "no module named x")

    code, err = _no_local(tmp_path, monkeypatch, submit, capfd)

    assert code == _slow_lane.CAPACITY and "failed before reporting" in err


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_setup_failed_in_the_child(tmp_path, monkeypatch, capfd):
    """Since #209 a setup that fails in the child, twice, reports SETUP_UNAVAILABLE (78), not NOT_RUN."""

    def submit(args, interruptible):
        return _slow_lane.SETUP_FAILED, "uv: no network\n", ""

    code, err = _no_local(tmp_path, monkeypatch, submit, capfd)

    assert code == _slow_lane.SETUP_UNAVAILABLE and "setup failed" in err


def test_no_local_still_returns_a_lane_that_ran_in_the_child(tmp_path, monkeypatch, capfd):
    code, _ = _no_local(
        tmp_path, monkeypatch, lambda args, interruptible: (1, "failed\n", '<testsuite tests="7"/>'), capfd
    )

    assert code == 1 and _suites(tmp_path / "slow.xml") == ["7"]


@pytest.mark.parametrize("code", range(6))
def test_remote_completion_precedes_junit_failure(tmp_path, monkeypatch, code):
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    result = tmp_path / "result.json"
    junit = tmp_path / "directory"
    junit.mkdir()

    with pytest.raises(IsADirectoryError):
        _slow_lane.main(
            ["--no-local", "--result", str(result), "--junit", str(junit)],
            submit=lambda args, spot: (code, "pytest finished", ""),
        )

    assert json.loads(result.read_text()) == {"completed": True, "code": code}


def test_not_run_leaves_no_completion(tmp_path, monkeypatch, no_run):
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    result = tmp_path / "result.json"

    assert _slow_lane.main(["--no-local", "--result", str(result)]) == _slow_lane.NOT_RUN
    assert not result.exists()


@pytest.mark.parametrize("code", range(6))
def test_local_completion_is_optional_and_preserves_pytest_arguments(tmp_path, monkeypatch, code):
    monkeypatch.chdir(_fake_project(tmp_path, code))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("AGENT_FACTORY_SLOW_LANE", raising=False)
    result = tmp_path / "result.json"

    assert _slow_lane.main(["--result", str(result), "-x"]) == code
    assert json.loads(result.read_text()) == {"completed": True, "code": code}
    assert (tmp_path / "argv.txt").read_text() == "tests -q -m slow -x"


def test_completion_precedes_remote_report_download(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from flyte import remote
    from flyte.io import File

    async def upload(path):
        return None, "s3://test-bucket/input"

    async def download(self, path):
        raise OSError("report download failed")

    async def submit(**kwargs):
        return (
            "2",
            File.from_existing_remote("s3://test-bucket/tail"),
            File.from_existing_remote("s3://test-bucket/junit"),
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(remote.upload_file, "aio", upload)
    monkeypatch.setattr(_slow_lane, "pack_tree", lambda root: b"")
    monkeypatch.setattr(_slow_lane, "pack_history", lambda root: b"")
    monkeypatch.setattr(_slow_lane, "make_lane_task", lambda *args: SimpleNamespace(aio=submit))
    monkeypatch.setattr(File, "download", download)
    result = tmp_path / "result.json"

    code = _slow_lane._run_remote(tmp_path / "slow.xml", [], result=result)

    assert code == 2 and json.loads(result.read_text()) == {"completed": True, "code": 2}
    assert "could not fetch the report" in capsys.readouterr().err


# The launcher learns that its child finished from a watch on the run. When that watch drops and reconnects,
# the completion can be lost: a pre-review's slow child succeeded at 13:41 and its launcher waited until
# checks.sh's 900 s ran out at 13:48. The launcher also asks the cluster itself.


def test_a_lane_whose_completion_the_watch_lost_is_read_from_the_cluster():
    async def never() -> tuple[str, str, str]:
        await asyncio.Event().wait()  # the watch never delivers
        raise AssertionError("unreachable")

    polls: list[int] = []

    async def finished() -> tuple[str, str, str] | None:
        polls.append(1)
        return ("0", "tail-uri", "junit-uri") if len(polls) >= 2 else None

    got = asyncio.run(_slow_lane._settle(never(), finished, poll_s=0.01, grace_s=0.02))
    assert got == ("0", "tail-uri", "junit-uri")
    assert len(polls) >= 2


def test_a_lane_the_watch_reports_is_taken_from_the_watch():
    async def soon() -> str:
        await asyncio.sleep(0.01)
        return "from-watch"

    async def finished() -> str | None:
        return None

    assert asyncio.run(_slow_lane._settle(soon(), finished, poll_s=0.05, grace_s=0.05)) == "from-watch"


def test_the_watch_still_wins_within_the_grace_after_the_cluster_says_done():
    # The poll may see the end first; the watch's own answer, arriving within the grace, is the one used.
    async def late() -> str:
        await asyncio.sleep(0.05)
        return "from-watch"

    async def finished() -> str | None:
        return "from-poll"

    assert asyncio.run(_slow_lane._settle(late(), finished, poll_s=0.01, grace_s=1.0)) == "from-watch"


def test_a_poll_that_fails_does_not_end_the_wait():
    async def soon() -> str:
        await asyncio.sleep(0.05)
        return "from-watch"

    async def finished() -> str | None:
        raise OSError("connection reset")  # one of the remote errors

    assert asyncio.run(_slow_lane._settle(soon(), finished, poll_s=0.01, grace_s=0.01)) == "from-watch"


def test_a_child_that_ended_without_succeeding_is_a_runtime_error():
    async def never() -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def finished() -> str | None:
        raise _slow_lane.LaneEnded("RuntimeSystemError", "child action x ended failed")

    with pytest.raises(flyte.errors.BaseRuntimeError):
        asyncio.run(_slow_lane._settle(never(), finished, poll_s=0.01, grace_s=0.01))


def test_the_outputs_are_read_from_the_actions_literals():
    # Shaped as `ActionDetails.output_literals()` returned them for a real lane child: `code` a string
    # primitive, `tail` and `junit` blobs.
    from types import SimpleNamespace as NS

    def blob(uri: str) -> Any:
        return NS(scalar=NS(blob=NS(uri=uri)))

    literals = {
        "code": NS(scalar=NS(primitive=NS(string_value="1"))),
        "tail": blob("s3://bucket/run/tail"),
        "junit": blob("s3://bucket/run/junit"),
    }
    code, tail, junit = _slow_lane._read_lane_outputs(literals)
    assert code == "1"
    assert tail.path == "s3://bucket/run/tail"
    assert junit.path == "s3://bucket/run/junit"


def test_the_poll_tolerates_exactly_the_remote_errors():
    from flyte_agent_factory import _errors

    assert _slow_lane._POLL_ERRORS == _errors.REMOTE_ERRORS


def test_a_bug_in_the_poll_is_not_taken_for_a_failed_call():
    async def never() -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def finished() -> str | None:
        raise AttributeError("a bug, not the network")

    with pytest.raises(AttributeError):
        asyncio.run(_slow_lane._settle(never(), finished, poll_s=0.01, grace_s=0.01))


# Sharded lanes: the tree is split by test file into children of their own, and their reports are merged.

SHARD_FILES = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py", "tests/test_e.py"]


@pytest.fixture
def five_files(tmp_path, monkeypatch):
    """A tree of five test files that the durations cache says take 600 s each: five shards of two CPUs, each
    expected to fit the on-demand runtime the default budget leaves after a spot wait."""
    from flyte_agent_factory import _lane_shards

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    for name in SHARD_FILES:
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("def test_x():\n    pass\n")
    cases = "".join(
        f'<testcase classname="{n.removesuffix(".py").replace("/", ".")}" name="t" time="600"/>'
        for n in SHARD_FILES
    )
    for lane in ("slow", "not slow"):
        _lane_shards.record_durations(f"<testsuite>{cases}</testsuite>", tmp_path, lane)
    return tmp_path


def _shard_files(args: list[str]) -> list[str]:
    """Return the files of `SHARD_FILES` the shard's selection keeps (all of them with no selection)."""
    spec = next((json.loads(a.split("=", 1)[1]) for a in args if a.startswith("--lane-shard=")), None)
    if spec is None:
        return list(SHARD_FILES)
    if "only" in spec:
        return list(spec["only"])
    # The first shard: every file and part of one that no other shard named.
    parts = {u.partition("#")[0]: int(u.rpartition("/")[2]) for u in spec["exclude"] if "#" in u}
    units = [f"{f}#{i}/{parts[f]}" for f in SHARD_FILES if f in parts for i in range(parts[f])]
    units += [f for f in SHARD_FILES if f not in parts]
    return [u for u in units if u not in spec["exclude"]]


def _passing(args: list[str], interruptible: bool) -> tuple[int, str, str]:
    cases = "".join(
        f'<testcase classname="{f.removesuffix(".py").replace("/", ".")}" name="t" time="3"/>'
        for f in _shard_files(args)
    )
    return (
        0,
        f"tail of {_shard_files(args)}\n",
        f'<testsuite tests="{len(_shard_files(args))}">{cases}</testsuite>',
    )


def test_a_lane_is_split_by_file_with_every_file_in_exactly_one_shard(five_files, local_pytest_must_not_run):
    seen = []

    def submit(args, interruptible):
        seen.append(args)
        return _passing(args, interruptible)

    code = _slow_lane.main(["--junit", str(five_files / "slow.xml"), "-m", "not slow"], submit=submit)

    assert code == 0
    assert len(seen) == 5
    assert sorted(f for args in seen for f in _shard_files(args)) == SHARD_FILES
    assert all(args[-2:] == ["-m", "not slow"] for args in seen)  # the lane's own arguments reach each shard
    assert sorted(_suites(five_files / "slow.xml")) == ["1"] * 5


def test_the_shards_reports_are_merged_and_their_durations_remembered(five_files, tmp_path):
    from flyte_agent_factory import _lane_shards

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=_passing)

    assert code == 0
    assert _lane_shards.read_durations(tmp_path, "slow") == dict.fromkeys(SHARD_FILES, 3.0)
    assert _lane_shards.read_durations(tmp_path, "not slow") == dict.fromkeys(SHARD_FILES, 600.0)


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([0, 0, 0, 0, 0], 0),
        ([0, 0, 1, 0, 0], 1),
        ([5, 5, 5, 5, 5], 5),
        ([0, 5, 0, 5, 5], 0),  # a shard whose files hold none of the lane's tests is not a failure
        ([5, 5, 1, 5, 5], 1),
        ([0, 1, 2, 0, 5], 2),  # the highest code that is not 5, as pytest itself ends
    ],
)
def test_the_lanes_code_is_pytests_over_the_shards_and_completion_is_recorded(five_files, codes, expected):
    order = iter(codes)

    def submit(args, interruptible):
        return next(order), "tail\n", '<testsuite tests="1"/>'

    result = five_files / "result.json"
    code = _slow_lane.main(["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=submit)

    assert code == expected
    assert json.loads(result.read_text()) == {"completed": True, "code": expected}


def test_each_shard_says_how_it_ended(five_files, capfd):
    def submit(args, interruptible):
        return _slow_lane.Outcome(0, "tail\n", "<testsuite/>", "a1b2c3")

    _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)

    err = capfd.readouterr().err
    lines = [line for line in err.splitlines() if "child action a1b2c3" in line]
    assert len(lines) == 5
    assert lines[0].startswith("slow lane: shard ") and "/5 spot child action a1b2c3" in lines[0]
    assert lines[0].endswith("code 0")


def test_the_lanes_verdict_is_the_last_thing_it_prints_and_names_the_failed_test(five_files, capfd):
    """A log kept only by its end (the supervisor keeps 12 KB) still says which shard failed and why."""

    def submit(args, interruptible):
        code, tail, junit = _passing(args, interruptible)
        if "tests/test_c.py" in _shard_files(args):
            tail = "x" * 20000 + "\nFAILED tests/test_c.py::t - AssertionError\n=== 1 failed in 3.00s ===\n"
            return _slow_lane.Outcome(1, tail, junit, "c0ffee")
        return _slow_lane.Outcome(code, tail + "=== 1 passed in 3.00s ===\n", junit, "a1b2c3")

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 1

    out = capfd.readouterr().out
    verdict = out[out.rindex("=== lane verdict: exit 1 ===") :].splitlines()
    assert len(verdict) == 1 + 5 + 1, "one line per shard and the failed test, nothing after"
    failed = next(i for i, line in enumerate(verdict) if "child action c0ffee" in line)
    assert verdict[failed].endswith("exit 1: 1 failed in 3.00s")
    assert verdict[failed + 1] == "  FAILED tests/test_c.py::t - AssertionError"
    assert sum("exit 0: 1 passed in 3.00s" in line for line in verdict) == 4


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_a_lane_with_no_capacity_ends_its_output_with_the_shard_that_was_not_placed(five_files, capfd):
    def submit(args, interruptible):
        if "tests/test_c.py" in _shard_files(args):
            raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
        return _passing(args, interruptible)

    assert _slow_lane.main([], submit=submit) == _slow_lane.CAPACITY

    out = capfd.readouterr().out
    verdict = out[out.rindex("=== lane verdict: exit 69 ===") :].splitlines()
    assert sum("did not run: not placed within 2m spot + 3m on-demand" in line for line in verdict) == 1
    assert sum(": exit 0: no pytest summary" in line for line in verdict) == 4


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_a_shard_with_no_capacity_ends_the_lane_with_69_and_a_reason_and_no_completion(five_files, capfd):
    def submit(args, interruptible):
        if "tests/test_c.py" in _shard_files(args):
            raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
        return _passing(args, interruptible)

    junit = five_files / "out" / "slow.xml"
    result = five_files / "result.json"
    for flags in ([], ["--no-local"], ["--on-demand"]):
        code = _slow_lane.main(["--junit", str(junit), "--result", str(result), *flags], submit=submit)

        assert code == _slow_lane.CAPACITY == 69
        assert (five_files / "out" / "slow.xml.capacity").read_text() == (
            "no capacity: 1 of 5 shards (2 CPUs) not placed within 2m spot + 3m on-demand\n"
        )
        # No completion: since round 2 the record says so, rather than there being none.
        record = json.loads(result.read_text())
        assert record["completed"] is False and record["outcome"] == "capacity" and not junit.exists()
    assert "no capacity: 1 of 5 shards" in capfd.readouterr().err


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_without_a_junit_path_a_lane_with_no_capacity_still_exits_69_and_says_why(five_files, capfd):
    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    assert _slow_lane.main([], submit=submit) == _slow_lane.CAPACITY
    assert "no capacity: 5 of 5 shards" in capfd.readouterr().err


def test_a_capacity_file_of_an_earlier_run_is_removed_at_the_start(five_files):
    stale = five_files / "slow.xml.capacity"
    stale.write_text("no capacity: from yesterday\n")

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=_passing) == 0
    assert not stale.exists()


def test_a_lane_that_can_never_be_placed_ends_promptly(five_files):
    # The bound on the wait is the queue bound in each child's own timeout: a fake that is refused at once
    # must not be retried in a loop, nor make the lane wait for anything else.
    calls = []

    async def submit(args, interruptible):
        calls.append(interruptible)
        await asyncio.sleep(0)
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    started = time.monotonic()
    code = _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)

    assert code == _slow_lane.CAPACITY and time.monotonic() - started < 5
    assert sorted(calls) == [False] * 5 + [True] * 5  # each shard: spot once, on-demand once


def test_a_setup_failure_is_retried_once_on_demand_then_is_a_setup_failure_not_capacity(five_files, capfd):
    # A child that was placed and ran but could not build the lane (an image without git, no network) is
    # not missing capacity: the lane says it could not set up, and the caller does not wait for room.
    calls = []

    def submit(args, interruptible):
        calls.append((tuple(_shard_files(args)), interruptible))
        return _slow_lane.SETUP_FAILED, "git: command not found\n", ""

    assert (
        _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)
        == _slow_lane.SETUP_UNAVAILABLE
    )
    assert len(calls) == 10 and {c[1] for c in calls} == {True, False}
    assert not (five_files / "slow.xml.capacity").exists()
    err = capfd.readouterr().err
    assert "slow lane: setup failed: 5 of 5 shards could not set up the lane" in err
    assert "git: command not found" in err and "no capacity" not in err


def test_a_shard_killed_for_memory_is_a_failure_of_the_change_not_of_capacity(five_files, capfd):
    """The limit is the shard's own, the same on-demand: a retry proves nothing, so none is made."""
    from flyte_agent_factory import _capacity

    calls: list[bool] = []

    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            calls.append(interruptible)
            raise flyte.errors.OOMError("OOMKilled", "container exceeded its memory limit")
        return _passing(args, interruptible)

    out = five_files / "out"
    code = _slow_lane.main(["--junit", str(out / "fast.xml"), "-m", "not slow"], submit=submit)

    assert code == 1
    assert calls.count(False) == 0 and calls == [True]
    record = json.loads((out / "fast.xml.result").read_text())
    assert record["completed"] is False and record["outcome"] == "out of memory" and record["code"] == 1
    assert "tests/test_b.py" in record["reason"] and "5Gi" in record["reason"]
    assert not (out / "fast.xml.capacity").exists()
    assert "ran out of its 5Gi memory in its own 2-CPU child" in capfd.readouterr().err
    assert len(_suites(out / "fast.xml")) == 4
    assert _capacity.decide_checks_outcome(out) == ""


def test_a_lane_with_a_shard_that_ran_out_and_one_that_could_not_set_up_is_a_failure_of_the_change(
    five_files,
):
    def submit(args, interruptible):
        files = _shard_files(args)
        if "tests/test_b.py" in files:
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        if "tests/test_c.py" in files:
            return _slow_lane.SETUP_FAILED, "x\n", ""
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 1


def test_a_lane_with_an_unplaced_shard_and_one_that_could_not_set_up_is_a_setup_failure(five_files):
    def submit(args, interruptible):
        files = _shard_files(args)
        if "tests/test_b.py" in files:
            raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
        if "tests/test_c.py" in files:
            return _slow_lane.SETUP_FAILED, "x\n", ""
        return _passing(args, interruptible)

    assert (
        _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)
        == _slow_lane.SETUP_UNAVAILABLE
    )
    assert not (five_files / "slow.xml.capacity").exists()


def test_a_setup_failure_on_spot_that_works_on_demand_is_an_ordinary_result(five_files):
    def submit(args, interruptible):
        return _passing(args, interruptible) if not interruptible else (_slow_lane.SETUP_FAILED, "x\n", "")

    result = five_files / "result.json"
    assert (
        _slow_lane.main(["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=submit)
        == 0
    )
    assert json.loads(result.read_text())["completed"] is True


def test_a_shard_that_runs_out_of_its_own_time_is_a_failure_of_the_change_not_of_capacity(five_files, capfd):
    calls = []

    def submit(args, interruptible):
        calls.append(interruptible)
        if "tests/test_b.py" in _shard_files(args):
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        return _passing(args, interruptible)

    result = five_files / "result.json"
    code = _slow_lane.main(["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=submit)

    # Recorded since round 2, as not completed: the gate must not take it for a missing report and run it.
    assert code == 1 and json.loads(result.read_text())["completed"] is False
    assert calls.count(False) == 0  # no retry elsewhere: another child would take as long
    err = capfd.readouterr().err
    assert re.search(
        r"slow lane: shard \d/5 \(tests/test_b\.py\) ran out of its \d+s in its own 2-CPU child", err
    )
    assert not (five_files / "slow.xml.capacity").exists()


def test_a_shard_that_failed_beside_an_unplaced_shard_fails_the_lane_and_names_the_test(five_files, capfd):
    # A real failure is not thrown away because another shard found no room: it is the change.
    case = '<testcase classname="tests.test_b" name="test_x"><failure/></testcase>'
    failing = f'<testsuite tests="1">{case}</testsuite>'

    def submit(args, interruptible):
        files = _shard_files(args)
        if "tests/test_b.py" in files:
            return 1, "FAILED tests/test_b.py::test_x\n", failing
        if "tests/test_c.py" in files:
            raise flyte.errors.MaxQueuedTimeExceededError("no room")
        return _passing(args, interruptible)

    result = five_files / "result.json"
    junit = five_files / "slow.xml"
    code = _slow_lane.main(["--junit", str(junit), "--result", str(result)], submit=submit)

    assert code == 1
    assert "<failure" in junit.read_text() and 'name="test_x"' in junit.read_text()
    assert json.loads(result.read_text()) == {"completed": True, "code": 1}
    assert not (five_files / "slow.xml.capacity").exists()
    assert re.search(
        r"slow lane: shard \d/5 \(tests/test_c\.py\) did not run: not placed", capfd.readouterr().err
    )


def test_a_shard_that_ran_out_leaves_an_outcome_record_and_the_junit_of_what_ran(five_files):
    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        return _passing(args, interruptible)

    result = five_files / "result.json"
    junit = five_files / "slow.xml"
    code = _slow_lane.main(["--junit", str(junit), "--result", str(result)], submit=submit)

    assert code == 1
    record = json.loads(result.read_text())
    assert record["completed"] is False and record["outcome"] == "ran out" and record["code"] == 1
    assert re.search(r"ran out of its \d+s in its own 2-CPU child", record["reason"])
    assert sorted(_suites(junit)) == ["1"] * 4  # the four shards that ran
    assert not (five_files / "slow.xml.capacity").exists()


def test_a_junit_path_without_a_result_path_records_the_outcome_beside_the_junit(five_files):
    junit = five_files / "slow.xml"
    record = five_files / "slow.xml.result"
    record.write_text('{"completed": true, "code": 0}')  # left by an earlier run

    def unplaced(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("no room")

    assert _slow_lane.main(["--junit", str(junit)], submit=unplaced) == _slow_lane.CAPACITY
    found = json.loads(record.read_text())
    assert found["completed"] is False and found["outcome"] == "capacity" and found["code"] == 69
    assert "no capacity" in found["reason"]

    assert _slow_lane.main(["--junit", str(junit)], submit=_passing) == 0
    assert json.loads(record.read_text()) == {"completed": True, "code": 0}


def test_a_lane_whose_children_could_not_set_up_records_it(five_files):
    def setup_failed(args, interruptible):
        return _slow_lane.SETUP_FAILED, "test-lane: setup failed\n", ""

    result = five_files / "result.json"
    code = _slow_lane.main(
        ["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=setup_failed
    )

    assert code == _slow_lane.SETUP_UNAVAILABLE
    record = json.loads(result.read_text())
    assert record["completed"] is False and record["outcome"] == "setup" and record["code"] == 78


def test_a_shard_that_ran_out_of_a_runtime_its_spot_wait_shortened_is_capacity_not_the_change(
    five_files, capfd
):
    # On-demand gets what the budget leaves after the spot wait and its own queue bound, less than spot's:
    # running out of that is the wait for room, and the report says how long it had and why.
    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            if interruptible:
                raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        return _passing(args, interruptible)

    code = _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)

    assert code == _slow_lane.CAPACITY
    reason = (five_files / "slow.xml.capacity").read_text()
    assert re.search(
        r"1 of 5 shards \(2 CPUs\) ran out of the \d+s on-demand left after no spot capacity", reason
    )


def test_a_retry_that_ran_out_after_its_spot_child_was_lost_says_so_not_no_spot_capacity(five_files, capfd):
    # Spot placed the child and the platform lost it before it reported (OutputUploadFailed on a 2-CPU run):
    # the on-demand retry's shorter runtime is still not the change, but the report names the loss, not room.
    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            if interruptible:
                raise flyte.errors.RuntimeUnknownError(
                    "OutputUploadFailed", "file not found at /var/outputs/code"
                )
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        return _passing(args, interruptible)

    code = _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)

    assert code == _slow_lane.CAPACITY
    reason = (five_files / "slow.xml.capacity").read_text()
    assert re.search(
        r"1 of 5 shards \(2 CPUs\) ran out of the \d+s on-demand left after its spot child failed before "
        r"reporting",
        reason,
    )
    assert "no spot capacity" not in reason + capfd.readouterr().err


def _make_long(root, monkeypatch, name="tests/test_b.py", seconds=500):
    """Give one file of the tree a recorded duration too long for on-demand after a full spot wait, yet short
    enough not to be split, in a 790 s lane (checks.sh gives 780): more than the 480 s left after waiting
    for both, at most 85% of the 600 s on-demand gets at once. The other files take 400 s, which fits."""
    from flyte_agent_factory import _lane_shards

    monkeypatch.setenv("AGENT_FACTORY_LANE_BUDGET_S", "790")
    cases = "".join(
        f'<testcase classname="{n.removesuffix(".py").replace("/", ".")}" name="t"'
        f' time="{seconds if n == name else 400}"/>'
        for n in SHARD_FILES
    )
    _lane_shards.record_durations(f"<testsuite>{cases}</testsuite>", root, "slow")


def test_a_shard_too_long_for_on_demand_after_a_spot_wait_goes_on_demand_first(
    five_files, monkeypatch, capfd
):
    _make_long(five_files, monkeypatch)
    ladders: dict[str, list[bool]] = {}

    def submit(args, interruptible):
        ladders.setdefault(" ".join(_shard_files(args)), []).append(interruptible)
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 0

    assert ladders.pop("tests/test_b.py") == [False]
    assert all(ladder == [True] for ladder in ladders.values()) and len(ladders) == 4
    err = capfd.readouterr().err
    assert re.search(r"shard \d/5 \(tests/test_b.py\): about 500s, more than the \d+s on-demand", err)
    assert "on-demand first" in err


def test_a_long_shard_on_demand_first_has_the_runtime_of_the_whole_budget(five_files, monkeypatch):
    _make_long(five_files, monkeypatch)
    runtimes: dict[bool, int | None] = {}

    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            runtimes[interruptible] = _slow_lane._ATTEMPT_RUNTIME_S.get()
        return _passing(args, interruptible)

    _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)

    # 790 s less on-demand's own 180 s queue bound, in whole minutes, not less a spot wait as well.
    assert runtimes == {False: 600}


def test_a_long_shard_that_runs_out_on_demand_first_is_the_change(five_files, monkeypatch):
    _make_long(five_files, monkeypatch)

    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 1
    assert not (five_files / "slow.xml.capacity").exists()


def test_a_long_shard_with_no_on_demand_room_tries_spot_and_running_out_there_is_capacity(
    five_files, monkeypatch
):
    _make_long(five_files, monkeypatch)
    seen = []

    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            seen.append(interruptible)
            if not interruptible:
                raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == _slow_lane.CAPACITY

    assert seen == [False, True]
    reason = (five_files / "slow.xml.capacity").read_text()
    assert re.search(
        r"1 of 5 shards \(2 CPUs\) ran out of the \d+s spot left after no on-demand capacity", reason
    )


def test_a_long_shard_placed_nowhere_is_capacity(five_files, monkeypatch):
    _make_long(five_files, monkeypatch)

    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == _slow_lane.CAPACITY
    assert "1 of 5 shards (2 CPUs) not placed" in (five_files / "slow.xml.capacity").read_text()


def test_the_repositorys_seed_tells_a_cold_cache_which_file_is_long(five_files, monkeypatch):
    from flyte_agent_factory import _lane_shards

    monkeypatch.setenv("AGENT_FACTORY_LANE_BUDGET_S", "790")  # as in `_make_long`
    (five_files / "cache").rename(five_files / "old-cache")  # no recorded durations at all
    seed = {"slow": {"tests/test_a.py": 100, "tests/test_b.py": 500}, "not slow": {"tests/test_c.py": 500}}
    (five_files / _lane_shards.SEED_FILE).write_text(json.dumps(seed))
    ladders: dict[str, list[bool]] = {}

    def submit(args, interruptible):
        ladders.setdefault(" ".join(_shard_files(args)), []).append(interruptible)
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 0

    assert ladders["tests/test_b.py"] == [False]
    assert all(ladder == [True] for name, ladder in ladders.items() if name != "tests/test_b.py")


def test_a_file_longer_than_any_child_could_run_is_split_by_test_across_shards(
    five_files, monkeypatch, capfd
):
    """2000 s is more than 85% of the 600 s an on-demand child gets: the file runs as 4 parts, in 4 shards."""
    _make_long(five_files, monkeypatch, seconds=2000)
    selections = []

    def submit(args, interruptible):
        selections.append(_shard_files(args))
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 0

    parts = [unit for files in selections for unit in files if unit.startswith("tests/test_b.py")]
    assert sorted(parts) == [f"tests/test_b.py#{i}/4" for i in range(4)]
    assert all(sum(u.startswith("tests/test_b.py") for u in files) <= 1 for files in selections)
    assert "tests/test_b.py#3/4" in capfd.readouterr().err  # a shard is named by the part it runs


def test_a_shard_that_ran_out_on_spot_is_the_change_beside_one_that_ran_out_on_demand(five_files):
    def submit(args, interruptible):
        files = _shard_files(args)
        if "tests/test_b.py" in files:
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        if "tests/test_c.py" in files:
            if interruptible:
                raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
            raise flyte.errors.MaxRuntimeExceededError("ran too long")
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 1


def test_a_runtime_timeout_is_not_taken_for_a_queue_timeout():
    # Both are TaskTimeoutError, so the lane tells them apart by class, which is all the SDK gives.
    assert not issubclass(flyte.errors.MaxRuntimeExceededError, flyte.errors.MaxQueuedTimeExceededError)
    assert not issubclass(flyte.errors.MaxQueuedTimeExceededError, flyte.errors.MaxRuntimeExceededError)
    assert issubclass(flyte.errors.MaxRuntimeExceededError, flyte.errors.TaskTimeoutError)


def test_a_crash_that_is_not_a_flyte_runtime_error_stays_loud(five_files):
    def submit(args, interruptible):
        raise RuntimeError("task crashed")

    with pytest.raises(RuntimeError, match="task crashed"):
        _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)


def test_the_knobs_default_and_are_read_at_call_time():
    assert _slow_lane.read_knobs({}) == _slow_lane.Knobs(cpus=2, max_shards=8, target_s=300, budget_s=1100)
    env = {
        "AGENT_FACTORY_LANE_CPUS": "4",
        "AGENT_FACTORY_LANE_MAX_SHARDS": "3",
        "AGENT_FACTORY_LANE_TARGET_S": "120",
        "AGENT_FACTORY_LANE_BUDGET_S": "600",
    }
    assert _slow_lane.read_knobs(env) == _slow_lane.Knobs(cpus=4, max_shards=3, target_s=120, budget_s=600)


@pytest.mark.parametrize("value", ["0", "-2", "many", "2.5", "1e3"])
def test_a_knob_that_is_not_a_whole_number_in_range_is_reported_and_defaulted(value, capsys):
    knobs = _slow_lane.read_knobs({"AGENT_FACTORY_LANE_CPUS": value, "AGENT_FACTORY_LANE_TARGET_S": "5"})

    assert knobs.cpus == 2 and knobs.target_s == 300
    err = capsys.readouterr().err
    assert "AGENT_FACTORY_LANE_CPUS must be a whole number of at least 1" in err
    assert "AGENT_FACTORY_LANE_TARGET_S must be a whole number of at least 30" in err


def test_the_cpus_knob_sizes_the_shards(five_files, monkeypatch, capfd):
    monkeypatch.setenv("AGENT_FACTORY_LANE_CPUS", "4")

    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)

    assert "(4 CPUs)" in (five_files / "slow.xml.capacity").read_text()


def test_the_max_shards_knob_caps_the_split(five_files, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_LANE_MAX_SHARDS", "2")
    seen = []

    def submit(args, interruptible):
        seen.append(_shard_files(args))
        return _passing(args, interruptible)

    _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit)

    assert len(seen) == 2 and sorted(f for files in seen for f in files) == SHARD_FILES


def test_a_shards_runtime_is_what_the_budget_has_left_after_its_queue_bound():
    now = [1000.0]
    budget = _slow_lane.Budget(1100, clock=lambda: now[0])

    assert budget.runtime_s(True) == 960  # 1100 s less 120 s of queueing, in whole minutes
    now[0] += 400
    # What is left now, at every ask: a late attempt must still end within the budget.
    assert budget.runtime_s(True) == 540  # 700 s left less 120 s
    assert budget.runtime_s(False) == 480  # 700 s left less 180 s
    assert _slow_lane.Budget(100, clock=lambda: 0.0).runtime_s(False) < _slow_lane.MIN_RUNTIME_S
    # An on-demand attempt submitted after a full spot wait: 700 s less 120 s and then 180 s.
    assert budget.runtime_after_spot_wait_s() == 360


def test_the_lane_task_is_built_per_size_and_runtime_and_registered_in_its_own_environment():
    big = _slow_lane.make_lane_task(4, False, timedelta(minutes=12))

    assert big is _slow_lane.make_lane_task(4, False, timedelta(minutes=12))  # built once
    assert big is not _slow_lane.make_lane_task(4, True, timedelta(minutes=12))
    assert big.name == "test-lane-4cpu-720s-on-demand"
    assert big.resources == flyte.Resources(cpu=("3000m", 4), memory=("6Gi", "9Gi"))
    assert big.timeout == flyte.Timeout(
        max_queued_time=_slow_lane.ON_DEMAND_QUEUE_BOUND, max_runtime=timedelta(minutes=12)
    )
    assert big.interruptible is False and big._cmd == _slow_lane.LANE_COMMAND
    assert list(_slow_lane._ENVIRONMENTS["test-lanes-4cpu-720s-on-demand"].tasks) == [big.name]
    assert _slow_lane.make_lane_task(2, True, timedelta(minutes=30)) is _slow_lane.task


@pytest.mark.parametrize(("cpus", "request_cpu"), [(1, "750m"), (2, "1500m"), (3, "2250m"), (4, "3000m")])
def test_a_shards_cpu_request_is_three_quarters_of_its_limit(cpus, request_cpu):
    resources = _slow_lane.make_lane_resources(cpus)
    assert resources.cpu == (request_cpu, cpus)
    assert resources.memory == (f"{-(-3 * cpus // 2)}Gi", f"{2 * cpus + 1}Gi")


def _children(monkeypatch, outcomes, tmp_path):
    """Fake the cluster under the real child submit: uploads are counted, each shard's child a fake task."""
    from types import SimpleNamespace

    from flyte.io import File

    uploads, tasks = [], []

    async def upload(path, **kwargs):
        uploads.append(path.name)
        return f"s3://bucket/{path.name}"

    async def download(self, path):
        kind = self.path.rsplit("/", 1)[-1]
        Path(path).write_text(
            "a tail\n"
            if kind == "tail"
            else '<testsuite tests="1"><testcase classname="tests.test_a" name="t" time="2"/></testsuite>'
        )

    def make_lane_task(cpus=2, interruptible=True, max_runtime=timedelta(minutes=30)):
        async def aio(tree, history, pytest_args, mode):
            assert mode == _slow_lane.MODE_PYTEST
            files = _shard_files(json.loads(pytest_args))
            tasks.append((cpus, interruptible, max_runtime, files))
            code = outcomes(files, interruptible)
            return (
                code,
                File.from_existing_remote("s3://bucket/run/tail"),
                File.from_existing_remote("s3://bucket/run/junit"),
            )

        return SimpleNamespace(aio=aio)

    monkeypatch.setattr(_slow_lane, "_upload_tree", upload)
    monkeypatch.setattr(_slow_lane, "pack_tree", lambda root: b"tree")
    monkeypatch.setattr(_slow_lane, "pack_history", lambda root: b"history")
    monkeypatch.setattr(_slow_lane, "make_lane_task", make_lane_task)
    monkeypatch.setattr(File, "download", download)
    return uploads, tasks


def test_the_tree_and_history_are_uploaded_once_for_all_the_shards(five_files, monkeypatch):
    uploads, tasks = _children(monkeypatch, lambda files, interruptible: "0", five_files)

    code = _slow_lane.main(["--junit", str(five_files / "slow.xml")])

    assert code == 0
    assert uploads == ["tree.tar.gz", "history.bundle"]  # not once per shard
    assert len(tasks) == 5 and sorted(f for _, _, _, files in tasks for f in files) == SHARD_FILES
    assert {(cpus, spot) for cpus, spot, _, _ in tasks} == {(2, True)}
    assert all(
        runtime.total_seconds() % 60 == 0 and runtime.total_seconds() <= 1100 for _, _, runtime, _ in tasks
    )
    assert _suites(five_files / "slow.xml") == ["1"] * 5


def test_a_child_that_ends_without_reporting_is_retried_on_demand_with_a_task_of_its_own(
    five_files, monkeypatch
):
    def outcomes(files, interruptible):
        if "tests/test_d.py" in files and interruptible:
            raise flyte.errors.RuntimeUserError("TaskInterruptedError", "spot node reclaimed")
        return "0"

    _, tasks = _children(monkeypatch, outcomes, five_files)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")]) == 0
    retried = [t for t in tasks if "tests/test_d.py" in t[3]]
    assert [t[1] for t in retried] == [True, False]  # spot, which the child did not survive, then on-demand


def test_shards_collect_from_tests_as_the_local_run_does(five_files):
    """A project without `testpaths` is collected from `tests/` in a shard, as `_run_local` collects it."""
    seen = []

    def submit(args, interruptible):
        seen.append(args)
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == 0
    assert seen and all(args[0] == "tests" for args in seen)

    seen.clear()
    assert _slow_lane.main(["--junit", str(five_files / "slow.xml"), "tests/test_a.py"], submit=submit) == 0
    assert seen and all("tests" not in args for args in seen), "a path the caller named is the selection"


def test_a_shard_of_many_files_is_named_by_its_first_few(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    (tmp_path / "tests").mkdir()
    for n in range(7):
        (tmp_path / "tests" / f"test_{n}.py").write_text("def test_x():\n    pass\n")

    def unplaced(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("no room")

    assert _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=unplaced) == _slow_lane.CAPACITY
    err = capfd.readouterr().err
    assert "(tests/test_0.py tests/test_1.py tests/test_2.py and 4 more) did not run" in err
    assert "tests/test_6.py" not in err


def _fake_clock(monkeypatch) -> list[float]:
    """Make the lane's budget read a clock the test moves; the lane starts at 0."""
    now = [0.0]
    real = _slow_lane.Budget
    monkeypatch.setattr(_slow_lane, "Budget", lambda total_s: real(total_s, clock=lambda: now[0]))
    return now


@pytest.mark.parametrize(
    ("failing", "at", "expected"), [("tests/test_a.py", 125, 780), ("tests/test_b.py", 700, 180)]
)
def test_a_late_on_demand_retry_gets_only_what_the_budget_has_left(
    five_files, monkeypatch, failing, at, expected
):
    now = _fake_clock(monkeypatch)

    def outcomes(files, interruptible):
        if interruptible and failing in files:
            now[0] = float(at)
            raise flyte.errors.RuntimeUserError("OOMKilled", "spot child died")
        return "0"

    _, tasks = _children(monkeypatch, outcomes, five_files)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")]) == 0
    (runtime,) = [runtime.total_seconds() for _, spot, runtime, files in tasks if not spot]
    assert runtime == expected  # 1100 s less the time spent, less 180 s of on-demand queueing, in minutes
    bound = _slow_lane.ON_DEMAND_QUEUE_BOUND.total_seconds()
    assert at + bound + runtime <= 1100, "nothing can end past the budget"


def test_a_retry_with_no_budget_left_is_capacity_without_a_submission(five_files, monkeypatch):
    now = _fake_clock(monkeypatch)

    def outcomes(files, interruptible):
        if "tests/test_c.py" in files:
            now[0] = 1000.0
            raise flyte.errors.RuntimeUserError("OOMKilled", "spot child died")
        return "0"

    _, tasks = _children(monkeypatch, outcomes, five_files)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")]) == _slow_lane.CAPACITY
    assert [spot for _, spot, _, files in tasks if "tests/test_c.py" in files] == [True]  # no on-demand
    assert "no budget left" in (five_files / "slow.xml.capacity").read_text()


def test_the_lane_script_runs_pytest_on_the_files_it_is_given_and_nothing_else(tmp_path):
    inputs = _lane_tree(
        tmp_path,
        {
            "test_a.py": "def test_a():\n    assert False\n",
            "test_b.py": "def test_b():\n    pass\n",
            "test_c.py": "def test_c():\n    pass\n",
        },
    )

    out = _run_lane(tmp_path, inputs, ["tests/test_b.py", "tests/test_c.py", "-q", "-m", "not slow"])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert "2 passed" in (out / "tail").read_text()
    junit = (out / "junit").read_text()
    assert "test_b" in junit and "test_c" in junit and "test_a" not in junit


def _records_workers(tmp_path: Path) -> dict[str, str]:
    probe = (
        "import os\n\n"
        "def test_workers():\n"
        "    report = os.path.join(os.environ['TMPDIR'], 'workers.txt')\n"
        "    open(report, 'a').write(os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS', 'unset') + '\\n')\n"
    )
    return {"test_workers.py": probe}


def test_the_lane_script_starts_one_xdist_worker_per_cpu_of_the_cgroup_quota(tmp_path):
    from flyte_agent_factory import _lane_shards

    quota = tmp_path / "cpu.max"
    quota.write_text("200000 100000\n")
    inputs = _lane_tree(tmp_path, _records_workers(tmp_path))
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_AUTO_NUM_WORKERS"}
    env_overrides = {"PYTEST_XDIST_AUTO_NUM_WORKERS": "", _slow_lane.CPU_MAX_VAR: str(quota)}
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" not in env

    out = _run_lane(tmp_path, inputs, ["-q", "-p", "no:randomly"], extra_env=env_overrides)

    expected = str(_lane_shards.read_cpu_allowance(quota))
    assert expected in {"1", "2"}  # two CPUs of quota, or fewer where the test host has fewer
    assert set((tmp_path / "workers.txt").read_text().split()) == {expected}, (out / "tail").read_text()


def test_the_lane_script_keeps_a_worker_count_it_is_given(tmp_path):
    inputs = _lane_tree(tmp_path, _records_workers(tmp_path))
    out = _run_lane(tmp_path, inputs, ["-q"], extra_env={"PYTEST_XDIST_AUTO_NUM_WORKERS": "3"})

    assert set((tmp_path / "workers.txt").read_text().split()) == {"3"}, (out / "tail").read_text()


def test_the_lane_script_without_a_quota_file_uses_the_affinity(tmp_path):
    from flyte_agent_factory import _lane_shards

    inputs = _lane_tree(tmp_path, _records_workers(tmp_path))
    env = {"PYTEST_XDIST_AUTO_NUM_WORKERS": "", _slow_lane.CPU_MAX_VAR: str(tmp_path / "none")}

    out = _run_lane(tmp_path, inputs, ["-q"], extra_env=env)

    expected = {str(_lane_shards.read_cpu_allowance(tmp_path / "none"))}
    assert set((tmp_path / "workers.txt").read_text().split()) == expected, (out / "tail").read_text()


def test_the_lane_script_ends_its_tail_with_its_wall_time_and_peak_memory(tmp_path):
    inputs = _lane_tree(tmp_path, {"test_a.py": "def test_a():\n    pass\n"})

    out = _run_lane(tmp_path, inputs, ["-q"])

    last = (out / "tail").read_text().splitlines()[-1]
    assert re.fullmatch(r"test-lane: wall \d+s, peak memory (\d+ MiB|n/a), \d+ workers", last), last


def test_a_local_run_sizes_xdist_from_the_cpu_quota_unless_told_otherwise(tmp_path, monkeypatch):
    (tmp_path / "pytest.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path('workers.txt').write_text(os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS', 'unset'))\n"
        "for a in sys.argv[1:]:\n"
        "    if a.startswith('--junitxml='):\n"
        "        Path(a.split('=', 1)[1]).write_text('<testsuite/>')\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("AGENT_FACTORY_SLOW_LANE", raising=False)
    monkeypatch.setattr(_lane_shards_module(), "read_cpu_allowance", lambda *a: 3)
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)

    assert _slow_lane.main([]) == 0
    assert (tmp_path / "workers.txt").read_text() == "3"

    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "6")
    assert _slow_lane.main([]) == 0
    assert (tmp_path / "workers.txt").read_text() == "6"


def _lane_shards_module():
    from flyte_agent_factory import _lane_shards

    return _lane_shards


def test_a_lane_whose_shards_left_no_report_leaves_an_empty_junit_file(five_files):
    # The gate reads an empty file as "no junit" and a report with no cases as "no tests ran".
    code = _slow_lane.main(
        ["--junit", str(five_files / "slow.xml")], submit=lambda args, interruptible: (1, "died\n", "")
    )

    assert code == 1 and (five_files / "slow.xml").read_text() == ""


def _checks_sh_workers(tmp_path: Path, preset: str | None = None) -> set[str]:
    """Run checks.sh with a `uv` that records the xdist worker count each of its commands starts with."""
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "workers.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "${{PYTEST_XDIST_AUTO_NUM_WORKERS:-unset}}" >> {log}\n')
    uv.chmod(0o755)
    drop = ("AGENT_FACTORY_RUN_SLOW", "AGENT_FACTORY_SLOW_LANE", "PYTEST_XDIST_AUTO_NUM_WORKERS")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PATH"] = f"{uv.parent}{os.pathsep}{os.environ['PATH']}"
    if preset is not None:
        env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = preset
    subprocess.run(["bash", str(repo / "factory-improvement/checks.sh")], cwd=repo, env=env, check=True)
    return set(log.read_text().split())


def test_checks_sh_starts_one_xdist_worker_per_cpu_of_the_quota_not_per_cpu_of_the_node(tmp_path):
    from flyte_agent_factory import _lane_shards

    assert _checks_sh_workers(tmp_path) == {str(_lane_shards.read_cpu_allowance())}


def test_checks_sh_keeps_a_worker_count_it_is_given(tmp_path):
    assert _checks_sh_workers(tmp_path, preset="7") == {"7"}


def test_the_setup_outcome_is_told_apart_from_every_other_exit():
    others = {0, 1, 2, 3, 4, 5, _slow_lane.SETUP_FAILED, _slow_lane.CAPACITY, _slow_lane.UPLOAD_FAILED}
    assert _slow_lane.SETUP_UNAVAILABLE not in others | {_slow_lane.NOT_RUN}


def test_submit_runs_the_async_submission_to_completion(tmp_path, monkeypatch):
    """An async submit handed to a command lane is awaited to its end, as a shard's is."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    seen = []

    async def submit(pytest_args, interruptible):
        seen.append((pytest_args, interruptible))
        return 0, "tail", "<testsuites/>"

    assert _slow_lane.main(["--", "pytest", "-x"], submit=submit) == 0
    assert seen == [(["pytest", "-x"], True)]


def test_a_command_that_cannot_start_exits_127_and_is_recorded(tmp_path, capsys):
    result = tmp_path / "result.json"
    missing = str(tmp_path / "no-such-program")
    assert _slow_lane._run_local_command(None, [missing, "arg"], result) == 127
    assert f"slow lane: cannot run {missing}:" in capsys.readouterr().err
    # 127 is outside the codes a finished lane reports, so no completion record is written for it.
    assert not result.exists()


def test_the_copies_of_the_launchers_constants_agree():
    """The gate and the supervisor copy these rather than import the launcher, which declares a task."""
    from flyte_agent_factory import _capacity, _gate_tests

    assert _capacity.LANE_NO_CAPACITY == _slow_lane.CAPACITY
    assert _capacity.LANE_BUDGET_ENV == _slow_lane.BUDGET_S_VAR
    assert _gate_tests._LANE_SETUP_UNAVAILABLE == _slow_lane.SETUP_UNAVAILABLE
    assert _gate_tests._LANE_NOT_RUN == _slow_lane.NOT_RUN


# --- a lost watch: the poll finds the child's end, and its timeout or OOM kill is still the change ---


def _ended(phase_name: str, error_code: str = "") -> _slow_lane.LaneEnded:
    from flyte.models import ActionPhase

    return _slow_lane.LaneEnded(
        "LaneEnded",
        f"child action x ended {phase_name}",
        phase=ActionPhase(phase_name),
        error_code=error_code,
    )


def test_the_poll_carries_the_childs_phase_and_error_code_on_lane_ended(monkeypatch):
    from types import SimpleNamespace as NS

    from flyte.models import ActionPhase

    details = NS(
        phase=ActionPhase.FAILED,
        error_info=NS(code="OOMKilled"),
        done=lambda: True,
    )

    async def get(**kwargs: Any) -> Any:
        return details

    monkeypatch.setattr(flyte, "ctx", lambda: NS(action=NS(run_name="run")))
    monkeypatch.setattr("flyte.remote.ActionDetails.get", NS(aio=get))

    with pytest.raises(_slow_lane.LaneEnded) as raised:
        asyncio.run(_slow_lane._poll_child_outputs(["a1"]))

    assert raised.value.phase == ActionPhase.FAILED and raised.value.error_code == "OOMKilled"
    assert isinstance(raised.value.as_watch_error(), flyte.errors.OOMError)


def test_a_lost_watch_for_a_child_that_timed_out_is_the_change_not_capacity(five_files, capfd):
    calls = []

    def submit(args, interruptible):
        calls.append(interruptible)
        if "tests/test_b.py" in _shard_files(args):
            raise _ended("timed_out", "MAX_RUNTIME_EXCEEDED")
        return _passing(args, interruptible)

    result = five_files / "result.json"
    code = _slow_lane.main(["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=submit)

    assert code == 1 and json.loads(result.read_text())["outcome"] == "ran out"
    assert calls.count(False) == 0  # not retried: another child would take as long
    assert re.search(
        r"shard \d/5 \(tests/test_b\.py\) ran out of its \d+s in its own 2-CPU child", capfd.readouterr().err
    )
    assert not (five_files / "slow.xml.capacity").exists()


def test_a_lost_watch_for_a_timeout_after_a_spot_wait_is_capacity_as_the_watch_would_say(five_files, capfd):
    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            if interruptible:
                raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
            raise _ended("timed_out", "MAX_RUNTIME_EXCEEDED")
        return _passing(args, interruptible)

    result = five_files / "result.json"
    code = _slow_lane.main(["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=submit)

    assert code == _slow_lane.CAPACITY and (five_files / "slow.xml.capacity").exists()
    assert "left after no spot capacity" in capfd.readouterr().err


def test_a_lost_watch_for_a_child_that_timed_out_in_the_queue_is_capacity(five_files):
    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            raise _ended("timed_out", "QUEUED_TIMEOUT_EXCEEDED")
        return _passing(args, interruptible)

    assert _slow_lane.main(["--junit", str(five_files / "slow.xml")], submit=submit) == _slow_lane.CAPACITY


def test_a_lost_watch_for_an_oom_killed_child_is_the_change_not_capacity(five_files, capfd):
    calls = []

    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            calls.append(interruptible)
            raise _ended("failed", "OOMKilled")
        return _passing(args, interruptible)

    result = five_files / "result.json"
    code = _slow_lane.main(["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=submit)

    assert code == 1 and json.loads(result.read_text())["outcome"] == "out of memory"
    assert calls == [True]
    assert "ran out of its 5Gi memory in its own 2-CPU child" in capfd.readouterr().err
    assert not (five_files / "slow.xml.capacity").exists()


@pytest.mark.parametrize("phase", ["failed", "aborted"])
def test_a_lost_watch_for_a_child_that_ended_otherwise_is_still_unreported(five_files, phase):
    def submit(args, interruptible):
        if "tests/test_b.py" in _shard_files(args):
            raise _ended(phase, "SomethingElse")
        return _passing(args, interruptible)

    result = five_files / "result.json"
    code = _slow_lane.main(["--junit", str(five_files / "slow.xml"), "--result", str(result)], submit=submit)

    assert code == _slow_lane.CAPACITY and json.loads(result.read_text())["outcome"] == "capacity"
    assert "did not report" in (five_files / "slow.xml.capacity").read_text()


def test_a_failed_upload_leaves_an_outcome_record_the_supervisor_can_read(five_files, monkeypatch):
    from flyte_agent_factory import _capacity

    async def upload_fails(*args: Any, **kwargs: Any) -> Any:
        raise _slow_lane.UploadFailed("UploadFailed", "upload of the tree failed 4 times")

    monkeypatch.setattr(_slow_lane, "_run_lane", upload_fails)
    junit = five_files / "slow.xml"

    assert _slow_lane.main(["--junit", str(junit), "--no-local"], submit=_passing) == _slow_lane.UPLOAD_FAILED

    record = json.loads((five_files / "slow.xml.result").read_text())
    assert record["completed"] is False and record["outcome"] == "upload" and record["code"] == 74
    assert "could not upload the tree" in _capacity.label_infrastructure_failures(five_files)
