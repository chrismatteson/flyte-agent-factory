"""The pure parts of the sharded test lanes: CPU allowance, shard planning, durations, junit merging."""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_tests, _gates, _lane_shards


def _cpu_max(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cpu.max"
    path.write_text(text)
    return path


@pytest.fixture
def sixteen_cpus(monkeypatch):
    """The cluster's pods report 16 CPUs to the affinity call whatever their quota is."""
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(16)))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("max 100000\n", 16),
        ("200000 100000\n", 2),
        ("400000 100000\n", 4),
        ("150000 100000\n", 2),  # 1.5 rounds up
        ("140000 100000\n", 1),
        ("50000 100000\n", 1),  # at least one
        ("9600000 100000\n", 16),  # a quota above the affinity does not add CPUs
        ("garbage", 16),
        ("", 16),
        ("200000 0\n", 16),
        ("x y\n", 16),
    ],
)
def test_cpu_allowance_is_the_affinity_cut_by_the_cgroup_quota(tmp_path, sixteen_cpus, text, expected):
    assert _lane_shards.read_cpu_allowance(_cpu_max(tmp_path, text)) == expected


def test_cpu_allowance_without_a_cgroup_file_is_the_affinity(tmp_path, sixteen_cpus):
    assert _lane_shards.read_cpu_allowance(tmp_path / "missing") == 16


def test_cpu_allowance_is_the_affinity_when_that_is_the_smaller(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1})
    assert _lane_shards.read_cpu_allowance(_cpu_max(tmp_path, "800000 100000")) == 2


def _tree(tmp_path: Path, files: dict[str, int]) -> Path:
    for name, size in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#" * size)
    return tmp_path


def test_discovery_finds_pytests_default_patterns_under_tests_sorted(tmp_path):
    _tree(
        tmp_path,
        {
            "tests/test_b.py": 1,
            "tests/test_a.py": 1,
            "tests/sub/deep/c_test.py": 1,
            "tests/sub/test_d.py": 1,
            "tests/conftest.py": 1,
            "tests/helpers.py": 1,
            "tests/test_data.json": 1,
            "src/test_not_in_tests.py": 1,
        },
    )
    assert _lane_shards.discover_test_files(tmp_path) == [
        "tests/sub/deep/c_test.py",
        "tests/sub/test_d.py",
        "tests/test_a.py",
        "tests/test_b.py",
    ]


def test_discovery_without_a_tests_directory_finds_nothing(tmp_path):
    assert _lane_shards.discover_test_files(tmp_path) == []


def test_a_file_without_a_recorded_duration_is_weighed_by_its_size(tmp_path):
    root = _tree(tmp_path, {"tests/test_a.py": 10 * 1024, "tests/test_b.py": 20 * 1024})
    weights = _lane_shards.weigh_files(root, ["tests/test_a.py", "tests/test_b.py"], {"tests/test_a.py": 7.5})
    assert weights["tests/test_a.py"] == 7.5
    assert weights["tests/test_b.py"] == pytest.approx(20 * _lane_shards.SECONDS_PER_KB)


def test_the_size_estimate_is_calibrated_to_the_measured_suite():
    # 2384 test-seconds over 169 files, less the Terraform file's 884 which the size cannot predict:
    # 1500 s over this repository's 3.6 MB of other test files.
    assert 0.3 <= _lane_shards.SECONDS_PER_KB <= 0.5


def test_an_empty_file_still_weighs_something(tmp_path):
    root = _tree(tmp_path, {"tests/test_a.py": 0})
    assert _lane_shards.weigh_files(root, ["tests/test_a.py"], {})["tests/test_a.py"] > 0


def test_a_plan_runs_every_file_exactly_once_and_never_splits_one():
    files = [f"tests/test_{i}.py" for i in range(23)]
    weights = {f: float(i % 7 + 1) for i, f in enumerate(files)}
    shards = _lane_shards.plan_shards(files, weights, 5)
    assert len(shards) == 5 and all(shards)
    assert sorted(f for shard in shards for f in shard) == sorted(files)


def test_a_plan_is_deterministic_whatever_the_input_order():
    files = [f"tests/test_{i}.py" for i in range(15)]
    weights = {f: 3.0 for f in files}  # all tied: only the path order decides
    first = _lane_shards.plan_shards(files, weights, 4)
    assert _lane_shards.plan_shards(list(reversed(files)), weights, 4) == first
    assert _lane_shards.plan_shards(files, dict(reversed(weights.items())), 4) == first


def test_a_plan_puts_the_heaviest_files_first_on_the_lightest_shard():
    weights = {"a": 10.0, "b": 9.0, "c": 5.0, "d": 4.0, "e": 3.0, "f": 3.0}
    shards = _lane_shards.plan_shards(list(weights), weights, 3)
    loads = sorted(sum(weights[f] for f in shard) for shard in shards)
    assert loads == [10.0, 12.0, 12.0]  # LPT; the best split of 34 over 3 has a longest shard of 12
    assert shards[0][0] == "a" and shards[1][0] == "b"


def test_one_file_heavier_than_the_rest_gets_a_shard_of_its_own():
    weights = {"tests/test_tf.py": 884.0, **{f"tests/test_{i}.py": 10.0 for i in range(20)}}
    shards = _lane_shards.plan_shards(list(weights), weights, 4)
    assert ["tests/test_tf.py"] in shards


def test_the_shard_count_is_clamped_to_the_number_of_files():
    assert len(_lane_shards.plan_shards(["a", "b"], {"a": 1.0, "b": 1.0}, 8)) == 2
    assert _lane_shards.plan_shards([], {}, 8) == []


def test_zero_weights_still_spread_over_the_shards():
    files = ["a", "b", "c", "d"]
    shards = _lane_shards.plan_shards(files, dict.fromkeys(files, 0.0), 4)
    assert [len(s) for s in shards] == [1, 1, 1, 1]


@pytest.mark.parametrize(
    ("total", "cpus", "target", "cap", "expected"),
    [
        (0, 2, 300, 8, 1),
        (100, 2, 300, 8, 1),
        (510, 2, 300, 8, 1),  # exactly cpus * target * 0.85
        (511, 2, 300, 8, 2),
        (2384, 2, 300, 8, math.ceil(2384 / (2 * 300 * 0.85))),
        (100000, 2, 300, 8, 8),
        (2384, 4, 300, 8, math.ceil(2384 / (4 * 300 * 0.85))),
    ],
)
def test_the_shard_count_covers_the_estimate_within_the_target_and_the_cap(
    total, cpus, target, cap, expected
):
    assert _lane_shards.count_shards(total, cpus, target, cap) == expected


def _junit(cases: list[tuple[str, str, float]], failing: str = "") -> str:
    body = "".join(
        f'<testcase classname="{cls}" name="{name}" time="{t}">'
        + ("<failure message='x'/>" if name == failing else "")
        + "</testcase>"
        for cls, name, t in cases
    )
    suite = f'<testsuite name="pytest" tests="{len(cases)}">{body}</testsuite>'
    return f'<?xml version="1.0"?><testsuites>{suite}</testsuites>'


@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    home = tmp_path / "cache-home"
    monkeypatch.setenv("XDG_CACHE_HOME", str(home))
    return home


def test_durations_are_summed_per_file_from_junit_classnames(tmp_path, cache_home):
    root = tmp_path / "repo"
    root.mkdir()
    xml = _junit(
        [
            ("tests.test_x", "test_1", 1.5),
            ("tests.test_x.TestClass", "test_2", 2.0),
            ("tests.sub.test_y", "test_3", 4.0),
            ("tests.sub.y_test.TestInner", "test_4", 1.0),
        ]
    )
    _lane_shards.record_durations(xml, root)
    assert _lane_shards.read_durations(root) == {
        "tests/test_x.py": 3.5,
        "tests/sub/test_y.py": 4.0,
        "tests/sub/y_test.py": 1.0,
    }


def test_a_file_attribute_names_the_file_when_junit_has_one(tmp_path, cache_home):
    xml = '<testsuite><testcase classname="weird" name="t" time="2" file="./tests/test_z.py"/></testsuite>'
    _lane_shards.record_durations(xml, tmp_path)
    assert _lane_shards.read_durations(tmp_path) == {"tests/test_z.py": 2.0}


def test_recording_replaces_the_files_present_and_keeps_the_others(tmp_path, cache_home):
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 5.0), ("tests.test_b", "t", 6.0)]), tmp_path)
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 1.0)]), tmp_path)
    assert _lane_shards.read_durations(tmp_path) == {"tests/test_a.py": 1.0, "tests/test_b.py": 6.0}


def test_the_cache_lives_under_xdg_cache_home_keyed_by_repository(tmp_path, cache_home):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 1.0)]), one)
    files = list((cache_home / "factory-lanes").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text()) == {"tests/test_a.py": 1.0}
    assert _lane_shards.read_durations(two) == {}  # another repository has its own


def test_the_cache_key_is_the_git_remote_when_there_is_one(tmp_path, cache_home):
    first, clone = tmp_path / "first", tmp_path / "clone"
    for repo in (first, clone):
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/r.git"], check=True
        )
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 2.0)]), first)
    assert _lane_shards.read_durations(clone) == {"tests/test_a.py": 2.0}  # a clone elsewhere shares it


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired(["git"], 10), FileNotFoundError("git")], ids=["hangs", "missing"]
)
def test_a_git_that_hangs_or_is_missing_keys_the_cache_by_path(tmp_path, cache_home, monkeypatch, error):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", broken)

    _lane_shards.record_durations(_junit([("tests.test_a", "t", 2.0)]), tmp_path)

    assert len(list((cache_home / "factory-lanes").glob("*.json"))) == 1
    assert _lane_shards.read_durations(tmp_path) == {"tests/test_a.py": 2.0}


def test_the_cache_defaults_to_home_dot_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 1.0)]), tmp_path)
    assert list((tmp_path / "home" / ".cache" / "factory-lanes").glob("*.json"))


@pytest.mark.parametrize("junk", ["", "not json", "[1, 2]", '{"tests/test_a.py": "slow"}', "null"])
def test_a_corrupt_cache_reads_as_empty_and_is_replaced_by_the_next_record(tmp_path, cache_home, junk):
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 1.0)]), tmp_path)
    (path,) = (cache_home / "factory-lanes").glob("*.json")
    path.write_text(junk)
    assert _lane_shards.read_durations(tmp_path) == {}
    _lane_shards.record_durations(_junit([("tests.test_b", "t", 2.0)]), tmp_path)
    assert _lane_shards.read_durations(tmp_path) == {"tests/test_b.py": 2.0}


def test_a_junit_that_does_not_parse_records_nothing(tmp_path, cache_home):
    _lane_shards.record_durations("<testsuite", tmp_path)
    _lane_shards.record_durations("", tmp_path)
    assert _lane_shards.read_durations(tmp_path) == {}


def test_an_unwritable_cache_is_not_an_error(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the cache directory should be")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 1.0)]), tmp_path)
    assert _lane_shards.read_durations(tmp_path) == {}


def test_merged_junit_holds_every_case_of_every_shard_for_the_gates_parser(tmp_path):
    one = _junit([("tests.test_a", "t1", 1.0), ("tests.test_a", "t2", 1.0)], failing="t2")
    # A bare <testsuite> root, as pytest writes without the xunit2 wrapper.
    two = (
        '<testsuite name="pytest" tests="1">'
        '<testcase classname="tests.test_b" name="t3" time="1"/></testsuite>'
    )
    merged = _lane_shards.merge_junit([one, "", "<not xml", two, "<testsuites/>"])
    path = tmp_path / "merged.xml"
    path.write_text(merged)

    cases = _gate_tests.parse_junit(path)

    assert [(c.classname, c.name, c.passed) for c in cases] == [
        ("tests.test_a", "t1", True),
        ("tests.test_a", "t2", False),
        ("tests.test_b", "t3", True),
    ]
    assert merged.count("<testsuite ") == 2


def test_merging_nothing_still_gives_a_parseable_report(tmp_path):
    path = tmp_path / "merged.xml"
    path.write_text(_lane_shards.merge_junit([]))
    assert _gate_tests.parse_junit(path) == []


def test_the_module_imports_nothing_from_the_package_and_is_test_harness():
    # The launcher imports it before it writes the junit file (`_gates._TEST_HARNESS`).
    source = Path(_lane_shards.__file__).read_text()
    assert "flyte_agent_factory" not in source and "from ." not in source
    assert _gates._TEST_HARNESS.search("src/flyte_agent_factory/_lane_shards.py")


def test_each_lane_keeps_its_own_durations(tmp_path, cache_home):
    # A file holding both markers takes different times in each lane; one lane must not overwrite the other's.
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 9.0)]), tmp_path, lane="slow")
    _lane_shards.record_durations(_junit([("tests.test_a", "t", 1.0)]), tmp_path, lane="not slow")
    assert _lane_shards.read_durations(tmp_path, lane="slow") == {"tests/test_a.py": 9.0}
    assert _lane_shards.read_durations(tmp_path, lane="not slow") == {"tests/test_a.py": 1.0}


def _collected(project: Path, plugin_dir: Path, args: list[str]) -> list[str]:
    """Return the test ids pytest collects in `project` with `args`, as the shard's container runs it."""
    proc = subprocess.run(
        [
            *[
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
            ]
        ]
        + args,
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(plugin_dir)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 5), proc.stdout + proc.stderr
    return [line for line in proc.stdout.splitlines() if "::" in line]


def test_the_shards_select_exactly_the_tests_the_unsharded_lane_selects(tmp_path):
    project = tmp_path / "project"
    (project / "tests/sub").mkdir(parents=True)
    (project / "tests/fixtures/bench/tests").mkdir(parents=True)
    # Pytest leaves the fixture project out; naming its files would collect them, its conftest shadowing ours.
    (project / "tests/conftest.py").write_text('collect_ignore = ["fixtures"]\n')
    (project / "tests/fixtures/bench/tests/conftest.py").write_text("")
    (project / "tests/fixtures/bench/tests/test_fixture.py").write_text("def test_never():\n    assert 0\n")
    for name, count in [("test_a", 3), ("test_b", 1), ("test_c", 2), ("sub/test_d", 2)]:
        body = "".join(f"def test_{n}():\n    pass\n" for n in range(count))
        (project / f"tests/{name}.py").write_text(body)
    planned = _lane_shards.discover_test_files(project)
    assert "tests/fixtures/bench/tests/test_fixture.py" in planned  # the glob finds it; pytest must decide
    (project / "tests/test_late.py").write_text(
        "def test_late():\n    pass\n"
    )  # in no plan: shard 1 takes it
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / f"{_lane_shards.PLUGIN_MODULE}.py").write_text(_lane_shards.PLUGIN_SOURCE)
    shards = _lane_shards.plan_shards(planned, dict.fromkeys(planned, 1.0), 3)

    whole = _collected(project, plugin_dir, [])
    per_shard = [_collected(project, plugin_dir, args) for args in _lane_shards.build_shard_args(shards)]

    assert len(per_shard) == 3
    assert sorted(t for ids in per_shard for t in ids) == sorted(whole)  # each test once, none missing
    assert "tests/test_late.py::test_late" in per_shard[0]
    assert not any("fixtures" in t for t in whole)


def test_the_parts_of_a_split_file_select_each_of_its_tests_once(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests/test_long.py").write_text("".join(f"def test_{n}():\n    pass\n" for n in range(7)))
    (project / "tests/test_short.py").write_text("def test_s():\n    pass\n")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / f"{_lane_shards.PLUGIN_MODULE}.py").write_text(_lane_shards.PLUGIN_SOURCE)
    planned = _lane_shards.discover_test_files(project)
    units, weights = _lane_shards.split_heavy_files(planned, {"tests/test_long.py": 30.0}, limit_s=10.0)
    assert units == [*(f"tests/test_long.py#{i}/3" for i in range(3)), "tests/test_short.py"]
    shards = _lane_shards.plan_shards(units, weights, 3)

    whole = _collected(project, plugin_dir, [])
    per_shard = [_collected(project, plugin_dir, args) for args in _lane_shards.build_shard_args(shards)]

    assert sorted(t for ids in per_shard for t in ids) == sorted(whole)  # each test once, none missing
    long_counts = sorted(sum("test_long.py" in t for t in ids) for ids in per_shard)
    assert long_counts == [2, 2, 3]  # every third test, from each part's offset


def test_only_a_file_over_the_limit_is_split_into_equal_parts():
    units, weights = _lane_shards.split_heavy_files(["a", "b", "c"], {"a": 831.0, "b": 459.0}, limit_s=459.0)
    assert units == ["a#0/2", "a#1/2", "b", "c"]
    assert weights == {"a#0/2": 415.5, "a#1/2": 415.5, "b": 459.0, "c": 0.0}
    assert _lane_shards.split_heavy_files(["a"], {"a": 5.0}, limit_s=0)[0] == ["a"]


def test_one_shard_leaves_pytest_its_own_selection():
    assert _lane_shards.build_shard_args([["tests/test_a.py"]]) == [[]]
    assert _lane_shards.build_shard_args([[]]) == [[]]


def test_a_shards_estimate_is_its_seconds_over_its_cpus_but_no_less_than_its_longest_file():
    weights = {"a.py": 100.0, "b.py": 100.0, "tf.py": 660.0}

    assert _lane_shards.estimate_shard_seconds(["a.py", "b.py"], weights, 2) == 100.0
    assert _lane_shards.estimate_shard_seconds(["a.py", "tf.py"], weights, 2) == 660.0
    assert _lane_shards.estimate_shard_seconds(["a.py", "b.py", "tf.py"], weights, 1) == 860.0
    assert _lane_shards.estimate_shard_seconds([], weights, 2) == 0.0


def test_the_seed_durations_are_the_repositorys_record_of_the_lane(tmp_path):
    seed = tmp_path / _lane_shards.SEED_FILE
    seed.parent.mkdir()
    seed.write_text(json.dumps({"slow": {"tests/test_a.py": 12, "tests/bad.py": "x"}, "not slow": [1]}))

    assert _lane_shards.read_seed_durations(tmp_path, "slow") == {"tests/test_a.py": 12.0}
    assert _lane_shards.read_seed_durations(tmp_path, "not slow") == {}
    assert _lane_shards.read_seed_durations(tmp_path, "other") == {}
    seed.write_text("not json")
    assert _lane_shards.read_seed_durations(tmp_path, "slow") == {}
    assert _lane_shards.read_seed_durations(tmp_path / "nowhere", "slow") == {}


def test_this_repositorys_seed_knows_the_terraform_file_is_long():
    root = Path(__file__).resolve().parents[1]
    seed = _lane_shards.read_seed_durations(root, "slow")

    assert seed["tests/test_bench_task_tf_bucket.py"] > 420
    assert all(name in _lane_shards.discover_test_files(root) for name in seed)


def test_the_suites_auto_worker_count_is_the_cpu_quota_unless_xdist_is_told(monkeypatch, pytestconfig):
    from tests import _cpu_budget, conftest

    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr(_cpu_budget, "cpus", lambda **_: 3)
    assert conftest.pytest_xdist_auto_num_workers(pytestconfig) == 3
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "7")
    assert conftest.pytest_xdist_auto_num_workers(pytestconfig) is None, (
        "xdist's own default reads the variable"
    )


def test_a_shards_summary_is_its_failures_then_pytests_last_summary_line():
    tail = "\n".join(
        [
            "=== 3 passed in 1.00s ===",  # an inner pytest's, before the shard's own
            *(f"FAILED tests/test_a.py::t{i} - AssertionError" for i in range(12)),
            "ERROR tests/test_b.py::u - RuntimeError",
            "========== 13 failed, 40 passed, 2 skipped in 61.20s (0:01:01) ==========",
        ]
    )
    lines = _lane_shards.summarize_tail(tail, limit=10)
    assert lines[:10] == [f"FAILED tests/test_a.py::t{i} - AssertionError" for i in range(10)]
    assert lines[10:] == ["... and 3 more", "13 failed, 40 passed, 2 skipped in 61.20s (0:01:01)"]
    assert _lane_shards.summarize_tail("no pytest here\n") == []
