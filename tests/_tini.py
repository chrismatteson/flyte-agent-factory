"""Put a real tini on PATH for the suite when the pod has none (#34).

The factory images install tini (`_images.with_init`), but the suite also runs in pods started from an image
built before that change: the test lane's shards run on the run's own image. There, Debian's package is
fetched with `apt-get download` (no root needed) and unpacked into a cache under `$XDG_CACHE_HOME` or
`~/.cache`. If that fails too, PATH is left alone and the tests that need tini fail naming it
(`tests/test_reaping.py::_tini`), quoting why the fetch failed (`FETCH_FAILURE`).
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Bounds for the two commands; a module constant so a test can shorten them.
DOWNLOAD_TIMEOUT_S = 120
UNPACK_TIMEOUT_S = 60

#: Why the last fetch failed, for the failure message of the tests that need tini; None when it did not.
FETCH_FAILURE: str | None = None


def _locate_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "flyte-agent-factory-tests" / "tini"


def _fetch(cache: Path) -> None:
    """Download and unpack tini into `cache`; a concurrent worker's finished copy is kept."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache.parent) as scratch:
        work = Path(scratch)
        subprocess.run(
            ["apt-get", "download", "tini"],
            cwd=work,
            check=True,
            capture_output=True,
            timeout=DOWNLOAD_TIMEOUT_S,
        )
        (deb,) = work.glob("tini_*.deb")
        subprocess.run(
            ["dpkg-deb", "-x", str(deb), str(work / "root")],
            check=True,
            capture_output=True,
            timeout=UNPACK_TIMEOUT_S,
        )
        try:
            (work / "root").rename(cache)
        except OSError:
            if not (cache / "usr/bin/tini").is_file():
                raise


def ensure_on_path() -> None:
    """Add a cached tini to PATH when none is on it, fetching it first if needed; never raises."""
    global FETCH_FAILURE
    if shutil.which("tini"):
        return
    cache = _locate_cache()
    try:
        if not (cache / "usr/bin/tini").is_file():
            _fetch(cache)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", None) or b""
        FETCH_FAILURE = f"{type(exc).__name__}: {exc} {stderr.decode(errors='replace').strip()}".strip()
        return
    os.environ["PATH"] = os.pathsep.join([str(cache / "usr/bin"), os.environ.get("PATH", "")])
