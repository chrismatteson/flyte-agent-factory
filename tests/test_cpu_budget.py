"""`-n auto` sizes the xdist pool to the CPUs the cgroup quota grants, not the CPUs the node has."""

from pathlib import Path

from tests import _cpu_budget


def _cgroup(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / name.replace("__", ".")).write_text(text)
    return tmp_path


def test_a_v2_quota_caps_the_workers_below_the_visible_cpus(tmp_path):
    root = _cgroup(tmp_path, cpu__max="400000 100000\n")
    assert _cpu_budget.cpus(visible=16, root=root) == 4


def test_a_fractional_quota_rounds_up(tmp_path):
    root = _cgroup(tmp_path, cpu__max="250000 100000\n")
    assert _cpu_budget.cpus(visible=16, root=root) == 3


def test_no_quota_leaves_the_visible_cpus(tmp_path):
    assert _cpu_budget.cpus(visible=16, root=_cgroup(tmp_path, cpu__max="max 100000\n")) == 16
    assert _cpu_budget.cpus(visible=6, root=tmp_path / "absent") == 6


def test_a_quota_above_the_visible_cpus_does_not_raise_them(tmp_path):
    assert _cpu_budget.cpus(visible=2, root=_cgroup(tmp_path, cpu__max="800000 100000\n")) == 2


def test_a_v1_quota_is_read_too(tmp_path):
    (tmp_path / "cpu").mkdir()
    root = _cgroup(tmp_path / "cpu", cpu__cfs_quota_us="200000\n", cpu__cfs_period_us="100000\n").parent
    assert _cpu_budget.cpus(visible=16, root=root) == 2
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("-1\n")  # v1's "no quota"
    assert _cpu_budget.cpus(visible=16, root=root) == 16


def test_an_unreadable_quota_leaves_the_visible_cpus(tmp_path):
    assert _cpu_budget.cpus(visible=8, root=_cgroup(tmp_path, cpu__max="garbage\n")) == 8


def test_the_xdist_hook_answers_with_the_budget(monkeypatch):
    import tests.conftest as conftest

    # checks.sh sets the variable for the suite it runs, and a set variable wins over the hook.
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr(_cpu_budget, "cpus", lambda **_: 3)
    assert conftest.pytest_xdist_auto_num_workers(None) == 3
