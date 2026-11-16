"""Load the judge self-agreement study and the changed engine's pure findings module.

The study (`projects/judge-self-agreement/`) is a sibling project whose modules import each other by bare name
(`import recover`), so its directory goes on `sys.path`. Everything else in this harness takes the loaded
modules as parameters, so a test can hand it a fake study. The engine itself is never imported here.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
#: The engine copy this harness lives in (`projects/judge-consistency`).
ENGINE_DIR = HERE.parents[1]
DEFAULT_STUDY_DIR = HERE.parents[2] / "judge-self-agreement"
#: Raw judge replies are private decision data: they go next to the workspace's harness files (git-ignored),
#: never under `projects/judge-consistency/`.
DEFAULT_OUT_DIR = HERE.parents[3] / ".claude-flyte" / "judge-consistency" / "rejudge-after"
#: The checkout the reviewed commits live in.
DEFAULT_SOURCE_REPO = HERE.parents[3]
RESULTS_DIR = HERE / "results"

_STUDY_MODULES = ("recover", "rejudge", "analyze", "inputs")
_FINDINGS_NAME = "judge_consistency_findings"


@dataclass(frozen=True)
class Study:
    """The study's directory and the modules the harness reuses."""

    dir: Path
    recover: ModuleType
    rejudge: ModuleType
    analyze: ModuleType
    inputs: ModuleType


def load(study_dir: Path = DEFAULT_STUDY_DIR) -> Study:
    """Import the study's modules from `study_dir`."""
    study_dir = study_dir.resolve()
    if not (study_dir / "rejudge.py").is_file():
        raise FileNotFoundError(f"no study at {study_dir} (expected rejudge.py there)")
    # A module of another study directory loaded earlier in this process would otherwise be reused.
    for name in _STUDY_MODULES:
        sys.modules.pop(name, None)
    if str(study_dir) in sys.path:
        sys.path.remove(str(study_dir))
    sys.path.insert(0, str(study_dir))
    loaded = {name: importlib.import_module(name) for name in _STUDY_MODULES}
    return Study(study_dir, **loaded)


def load_findings_module() -> ModuleType:
    """Return the changed engine's `_findings.py`, loaded as a standalone module.

    Not `import flyte_agent_factory`: at run time the pod's installed engine provides the cluster call path,
    and a second package of that name would clash with it. `_findings` is stdlib-only, so it loads alone.
    """
    if (cached := sys.modules.get(_FINDINGS_NAME)) is not None:
        return cached
    path = ENGINE_DIR / "src" / "flyte_agent_factory" / "_findings.py"
    spec = importlib.util.spec_from_file_location(_FINDINGS_NAME, path)
    assert spec is not None and spec.loader is not None  # a readable .py path always has a loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: `@dataclass` looks its module up in `sys.modules` to resolve string annotations.
    sys.modules[_FINDINGS_NAME] = module
    spec.loader.exec_module(module)
    return module


def read_finding_check_setting() -> tuple[str, str]:
    """Return the engine's finding-check (model, effort), read from its policy file so it cannot drift."""
    text = (ENGINE_DIR / "src" / "flyte_agent_factory" / "_policy.py").read_text()
    found = re.search(r'"finding_check":\s*Setting\(\s*"([^"]+)",\s*"([^"]+)"\s*\)', text)
    if found is None:
        raise RuntimeError("no `finding_check` Setting in _policy.py; the harness must be updated")
    return found.group(1), found.group(2)
