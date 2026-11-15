"""The pure logic of the sharded test lanes: CPU allowance, test files, durations, shard plan, junit merge.

Standard library only, and no import of another package module: `_slow_lane` imports this before it writes
the junit file, so it is part of the test harness (`_gates._TEST_HARNESS`), and `checks.sh` loads it before
pytest starts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from xml.etree import ElementTree

#: The cgroup v2 CPU quota: "<quota µs> <period µs>", or "max <period>" when there is none.
CPU_MAX = Path("/sys/fs/cgroup/cpu.max")

#: Seconds a test file takes per KB of source, for a file with no recorded duration. Calibrated on the
#: measured suite: 2384 test-seconds over 169 files, of which tests/test_bench_task_tf_bucket.py alone is 884
#: and a size cannot predict it (it waits on Terraform). The other 1500 s over this repository's 3.6 MB of
#: test files is 0.41 s/KB. The estimate only has to spread files and size the shard count; once a lane has
#: run, its recorded durations replace it.
SECONDS_PER_KB = 0.4
#: No file weighs less than this, so an empty or tiny file still counts as work.
MIN_FILE_SECONDS = 0.5
#: Share of `cpus * target_s` a shard is planned to fill: the estimate is rough and a shard that overruns its
#: target costs more than one shard too many.
FILL = 0.85


def read_cpu_quota(cpu_max: Path = CPU_MAX) -> float:
    """Return the CPUs this process may use, unrounded: the affinity count, cut by the cgroup v2 quota.

    The one reading of the quota: the load clock (`_load_clock`) uses it as is, the worker count rounded. A
    missing or unreadable quota file leaves the affinity count.
    """
    try:
        cpus = float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        cpus = float(os.cpu_count() or 1)
    try:
        quota, period = cpu_max.read_text().split()[:2]
        if quota != "max":
            cpus = min(cpus, int(quota) / int(period))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return cpus


def read_cpu_allowance(cpu_max: Path = CPU_MAX) -> int:
    """Return the CPUs this process may use as a worker count: `read_cpu_quota`, rounded; at least 1.

    A pod's `os.cpu_count()` is the node's (16 on this cluster) whatever its quota, so `-n auto` starts far
    more workers than CPUs.
    """
    return max(1, int(read_cpu_quota(cpu_max) + 0.5))


def discover_test_files(root: Path) -> list[str]:
    """Return the test files under `root/tests` by pytest's default patterns, as sorted posix paths."""
    tests = root / "tests"
    found = {
        path.relative_to(root).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in tests.rglob(pattern)
        if path.is_file()
    }
    return sorted(found)


def _locate_cache_file(root: Path, lane: str) -> Path:
    """Return the durations cache of `lane` (its `-m` expression) in the repository at `root`.

    The repository's remote URL, else its path, names it; each lane has its own, as a file holding both
    markers takes different times in each.
    """
    identity = str(root.resolve())
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # No git, or one that hangs (a lock, a network mount): the path names the cache. The cache only speeds
        # the next plan up, and a lane that has already run must not fail for it.
        pass
    else:
        if remote.returncode == 0 and remote.stdout.strip():
            identity = remote.stdout.strip()
    key = hashlib.sha256(f"{identity}\0{lane}".encode()).hexdigest()[:16]
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "factory-lanes" / f"{key}.json"


def read_durations(root: Path | None = None, lane: str = "") -> dict[str, float]:
    """Return `{test file: seconds}` as the last lanes recorded; a missing or corrupt cache is empty."""
    try:
        data = json.loads(_locate_cache_file(root or Path.cwd(), lane).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
        for k, v in data.items()
    ):
        return {}
    return {k: float(v) for k, v in data.items()}


#: The repository's own record of its test files' seconds per lane, `{lane: {test file: seconds}}`, read
#: under the cache: a pod that has not run a lane yet (a reviewer's, a new session's) still knows which
#: file is long, which its size cannot say.
SEED_FILE = Path("tests") / "lane_durations.json"


def read_seed_durations(root: Path, lane: str = "") -> dict[str, float]:
    """Return `{test file: seconds}` of `lane` in the repository's `SEED_FILE`; missing or bad is empty."""
    try:
        data = json.loads((root / SEED_FILE).read_text())
    except (OSError, ValueError):
        return {}
    found = data.get(lane) if isinstance(data, dict) else None
    if not isinstance(found, dict):
        return {}
    return {
        k: float(v)
        for k, v in found.items()
        if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _find_test_file_of(case: ElementTree.Element) -> str | None:
    """Return the test file a junit `testcase` belongs to, or None when neither attribute names one."""
    file = case.get("file", "")
    if file.endswith(".py"):
        return Path(file).as_posix().removeprefix("./")
    # `tests.sub.test_x.TestClass`: the module is the path up to the first `test_*` or `*_test` part.
    parts = case.get("classname", "").split(".")
    for i, part in enumerate(parts):
        if part.startswith("test_") or part.endswith("_test"):
            return "/".join(parts[: i + 1]) + ".py"
    return None


def record_durations(junit_xml_text: str, root: Path | None = None, lane: str = "") -> None:
    """Add the per-file seconds of a junit report to the cache; a file in the report replaces its old value.

    The cache only speeds the next plan up, so a report that does not parse or a cache that cannot be written
    is ignored.
    """
    try:
        cases = ElementTree.fromstring(junit_xml_text).iter("testcase")
        seconds: dict[str, float] = {}
        for case in cases:
            name = _find_test_file_of(case)
            if name is not None:
                seconds[name] = seconds.get(name, 0.0) + float(case.get("time", 0))
    except (ElementTree.ParseError, ValueError):
        return
    if not seconds:
        return
    root = root or Path.cwd()
    try:
        path = _locate_cache_file(root, lane)
        merged = {**read_durations(root, lane), **seconds}
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lanes of other checkouts of this repository may write at once; a reader sees a whole file or none.
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as record:
            json.dump(merged, record)
        Path(record.name).replace(path)
    except OSError:
        return


def weigh_files(root: Path, files: Iterable[str], durations: Mapping[str, float]) -> dict[str, float]:
    """Return each file's expected seconds: its recorded duration, else an estimate from its size."""
    weights = {}
    for name in files:
        if name in durations:
            weights[name] = durations[name]
            continue
        try:
            size = (root / name).stat().st_size
        except OSError:
            size = 0
        weights[name] = max(MIN_FILE_SECONDS, size / 1024 * SECONDS_PER_KB)
    return weights


def count_shards(total_estimated_seconds: float, cpus: int, target_s: float, max_shards: int) -> int:
    """Return how many shards of `cpus` CPUs finish `total_estimated_seconds` of work within `target_s`."""
    needed = math.ceil(total_estimated_seconds / (cpus * target_s * FILL))
    return max(1, min(needed, max_shards))


def estimate_shard_seconds(shard: Iterable[str], weights: Mapping[str, float], cpus: int) -> float:
    """Return how long a shard is expected to run: its seconds over its CPUs, at least its longest file's.

    A file's own seconds are the floor because one file need not spread over workers: the Terraform benchmark
    file caps its concurrency itself, and measured 410-450 s of wall for its 660 test-seconds on 2 CPUs.
    """
    seconds = [weights.get(name, 0.0) for name in shard]
    return max([sum(seconds) / max(1, cpus), *seconds], default=0.0)


#: Separates a test file from the part of it a shard runs: "tests/test_x.py#1/4" is every fourth of the file's
#: collected tests, from its second on.
PART_MARK = "#"


def split_heavy_files(
    files: Sequence[str], weights: Mapping[str, float], limit_s: float
) -> tuple[list[str], dict[str, float]]:
    """Return the units to plan and their weights: each file, or for one heavier than `limit_s` its parts.

    A file is the floor of a shard's runtime (`estimate_shard_seconds`), so one that alone would outrun a
    shard's child is split by test into `ceil(weight / limit_s)` parts of equal weight, which the selection
    plugin runs as every k-th of the file's collected tests. A file with fewer tests than parts leaves some
    parts empty: a shard that selects nothing ends with pytest's 5, which the lane does not count as failure.
    """
    units: list[str] = []
    unit_weights: dict[str, float] = {}
    for name in files:
        weight = weights.get(name, 0.0)
        parts = math.ceil(weight / limit_s) if limit_s > 0 else 1
        if parts <= 1:
            units.append(name)
            unit_weights[name] = weight
            continue
        for i in range(parts):
            unit = f"{name}{PART_MARK}{i}/{parts}"
            units.append(unit)
            unit_weights[unit] = weight / parts
    return units, unit_weights


def plan_shards(files: Sequence[str], weights: Mapping[str, float], count: int) -> list[list[str]]:
    """Split `files` (or parts of them) into at most `count` shards by longest-processing-time-first.

    Deterministic: the heaviest file goes first and to the lightest shard, ties broken by path and by shard
    number, so the same inputs give the same plan whatever their order. No shard is empty.
    """
    if not files:
        return []
    count = max(1, min(count, len(files)))
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for name in sorted(files, key=lambda f: (-weights.get(f, 0.0), f)):
        # Fewest files second: with zero weights every load ties, and files must still spread out.
        i = min(range(count), key=lambda n: (loads[n], len(shards[n]), n))
        shards[i].append(name)
        loads[i] += weights.get(name, 0.0)
    return [sorted(shard) for shard in shards]


_PYTEST_SUMMARY = re.compile(r"^=+ .*\bin [0-9.]+s\b.*=+$")
_PYTEST_FAILURE = re.compile(r"^(FAILED|ERROR) ")


def summarize_tail(tail: str, limit: int = 10) -> list[str]:
    """Return what a shard's pytest output says: at most `limit` FAILED/ERROR lines, then its summary."""
    lines = tail.splitlines()
    failures = [line for line in lines if _PYTEST_FAILURE.match(line)]
    summaries = [line.strip("= ") for line in lines if _PYTEST_SUMMARY.match(line)]
    shown = failures[:limit]
    if len(failures) > limit:
        shown.append(f"... and {len(failures) - limit} more")
    return shown + summaries[-1:]


def merge_junit(xml_texts: Iterable[str]) -> str:
    """Return one `<testsuites>` of every `<testsuite>` in the reports; empty or invalid ones are left out."""
    merged = ElementTree.Element("testsuites")
    for text in xml_texts:
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            continue
        merged.extend([root] if root.tag == "testsuite" else root.findall("testsuite"))
    return ElementTree.tostring(merged, encoding="unicode")


#: The pytest plugin a shard loads (`-p`) to keep only its own files of what pytest collects. Naming the files
#: on the command line instead would collect files pytest leaves out (a `collect_ignore`d fixture project,
#: whose conftest then shadows the suite's), so every shard collects as the unsharded lane does and only
#: deselects. The container writes this source to a file on its `PYTHONPATH`; the package is not installed
#: there.
PLUGIN_MODULE = "factory_lane_shard"
PLUGIN_SOURCE = '''"""Keep the collected tests of this shard's files.

`--lane-shard` is JSON: {"only": [units]} or {"exclude": [units]}. A unit is a file, or a part of one,
"path#i/k": the file's collected tests whose index (in collection order) is i modulo k.
"""
import json


def pytest_addoption(parser):
    parser.addoption("--lane-shard", default=None, help="this shard's test files, as JSON")


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("lane_shard")
    if not spec:
        return
    spec = json.loads(spec)
    units = set(spec.get("only", spec.get("exclude", [])))
    parts = {}
    for unit in units:
        path, mark, part = unit.rpartition("#")
        if mark:
            parts[path] = int(part.split("/")[1])
    seen = {}
    keep, drop = [], []
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        index = seen[path] = seen.get(path, -1) + 1
        named = path in units or (path in parts and f"{path}#{index % parts[path]}/{parts[path]}" in units)
        (keep if named == ("only" in spec) else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
'''


def build_shard_args(shards: Sequence[Sequence[str]]) -> list[list[str]]:
    """Return each shard's pytest arguments: the plugin and the files it keeps.

    The first shard keeps everything the others do not, so a test file the plan did not list (one the glob
    misses) still runs once. A single shard needs no selection: pytest's own is the lane.
    """
    if len(shards) <= 1:
        return [[]]
    plugin = ["-p", PLUGIN_MODULE]
    rest = sorted(f for shard in shards[1:] for f in shard)
    first = [*plugin, "--lane-shard=" + json.dumps({"exclude": rest})]
    return [first] + [[*plugin, "--lane-shard=" + json.dumps({"only": list(s)})] for s in shards[1:]]
