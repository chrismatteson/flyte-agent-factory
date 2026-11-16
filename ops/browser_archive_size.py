"""Print the size of a Chromium profile as the old archive, the trimmed archive, and the trimmed sealed one.

    python ops/browser_archive_size.py PROFILE_DIR
    python ops/browser_archive_size.py --synthetic [SCRATCH_DIR]   # a stand-in profile (default: a temp dir)

The old archive is what personal.py wrote before encryption: everything except caches and Singleton files.
The key used for the sealed size is random and thrown away; nothing is printed from the profile.
"""

import argparse
import base64
import os
import random
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import IO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/flyte_agent_factory/_assets/browser"))
import browser_archive

# The deny-list personal.py used before the allow-list (`browser_archive.SESSION_STATE`).
OLD_EXCLUDED = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "CacheStorage",
}

# Rough sizes (bytes) of what a profile holds after a few page loads; contents are random, so caches do
# not compress, which is about right for images and scripts. Not measured from a real browser.
SYNTHETIC = {
    "Local State": 12_000,
    "Default/Preferences": 25_000,
    "Default/Network/Cookies": 40_000,
    "Default/Network/Cookies-journal": 0,
    "Default/Local Storage/leveldb/000003.log": 120_000,
    "Default/Session Storage/000003.log": 20_000,
    "Default/IndexedDB/https_github.com_0.indexeddb.leveldb/000003.log": 2_000_000,
    "Default/History": 300_000,
    "Default/Favicons": 350_000,
    "Default/Login Data": 40_000,
    "Default/Web Data": 120_000,
    "Default/Service Worker/ScriptCache/abc": 600_000,
    "Default/Service Worker/CacheStorage/abc/0": 5_000_000,
    "Default/Cache/Cache_Data/f_000001": 20_000_000,
    "Default/Code Cache/js/index": 8_000_000,
    "Default/GPUCache/data_0": 500_000,
    "ShaderCache/data_0": 400_000,
}


def build_synthetic(root: Path) -> None:
    """Write a profile of the real directory layout, with random contents."""
    rng = random.Random(1)
    for name, size in SYNTHETIC.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rng.randbytes(size))


def old_archive(profile: Path, target: IO[bytes]) -> None:
    """Write the pre-encryption archive of `profile` to `target`."""
    with tarfile.open(fileobj=target, mode="w:gz") as tar:
        for directory, dirs, files in os.walk(profile):
            dirs[:] = [d for d in dirs if d not in OLD_EXCLUDED and not d.startswith("Singleton")]
            for name in files:
                if name not in OLD_EXCLUDED and not name.startswith("Singleton"):
                    path = Path(directory) / name
                    tar.add(path, arcname=str(path.relative_to(profile)), recursive=False)


def main() -> None:
    """Build the three archives of the profile and print their sizes."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("profile", type=Path, nargs="?", help="the profile directory")
    parser.add_argument(
        "--synthetic",
        nargs="?",
        const="",
        metavar="SCRATCH_DIR",
        help="measure a stand-in profile written to SCRATCH_DIR (default: a new temp dir) instead",
    )
    args = parser.parse_args()
    scratch = False
    if args.synthetic is not None:
        if args.profile:
            parser.error("give PROFILE_DIR or --synthetic, not both")
        scratch = args.synthetic == ""  # a directory this run made is removed again: it is ~37 MB
        args.profile = Path(tempfile.mkdtemp(prefix="browser-profile-") if scratch else args.synthetic)
        build_synthetic(args.profile)
    elif not args.profile:
        parser.error("PROFILE_DIR or --synthetic is required")
    elif not (args.profile.is_dir() and os.access(args.profile, os.R_OK | os.X_OK)):
        print(f"error: {args.profile} is not a readable directory", file=sys.stderr)
        sys.exit(2)
    with tempfile.TemporaryFile() as old, tempfile.TemporaryFile() as trimmed:
        old_archive(args.profile, old)
        with tarfile.open(fileobj=trimmed, mode="w:gz") as tar:
            browser_archive.add_session_state(tar, args.profile)
        size = trimmed.tell()
        trimmed.seek(0)
        kek = browser_archive.load_key(base64.b64encode(os.urandom(32)).decode())
        sealed = browser_archive.seal(trimmed.read(), kek)
        raw = sum(p.stat().st_size for p in args.profile.rglob("*") if p.is_file())
        print(f"profile on disk       {raw:>12,} bytes")
        print(f"old tar.gz            {old.tell():>12,} bytes")
        print(f"trimmed tar.gz        {size:>12,} bytes")
        print(f"trimmed + encrypted   {len(sealed):>12,} bytes")
    if scratch:
        shutil.rmtree(args.profile)


if __name__ == "__main__":
    main()
