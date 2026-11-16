"""`ops/state_bench.py` runs every backend with a few writers and reports a table with no failures."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_bench():
    spec = importlib.util.spec_from_file_location("state_bench", ROOT / "ops" / "state_bench.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["state_bench"] = module
    spec.loader.exec_module(module)
    return module


def test_bench_reports_a_row_per_backend_and_writer_count_without_failures(capsys):
    bench = _load_bench()
    code = bench.main(["--writers", "1,3", "--ops", "2", "--git-delay-ms", "5"])
    out = capsys.readouterr()
    assert code == 0, out.err
    rows = [line for line in out.out.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert len(rows) == len(bench.BACKENDS) * 2
    assert {row.split("|")[1].strip() for row in rows} == set(bench.BACKENDS)
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert int(cells[3]) == 0  # failures
        assert int(cells[4]) == 0  # lost updates
        assert int(cells[2]) > 0  # calls


def test_percentiles_of_one_and_many_samples():
    bench = _load_bench()
    assert bench.percentiles([]) == (0.0, 0.0, 0.0)
    assert bench.percentiles([2.0]) == (2.0, 2.0, 2.0)
    p50, p95, peak = bench.percentiles([float(i) for i in range(1, 101)])
    assert p50 == 50.5
    assert 95 <= p95 <= 96
    assert peak == 100.0


@pytest.mark.parametrize("writers", ["0", "1,0", "-2", "x"])
def test_a_writer_count_below_one_is_a_clean_usage_error(writers, capsys):
    bench = _load_bench()
    with pytest.raises(SystemExit) as exc:
        bench.main(["--writers", writers, "--ops", "1"])
    assert exc.value.code == 2
    assert "--writers" in capsys.readouterr().err
