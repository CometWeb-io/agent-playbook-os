from __future__ import annotations

from pathlib import Path
import hashlib
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EPOCH = "1790035200"  # 2026-09-22T00:00:00Z-ish fixed release-test epoch; identity matters, wall clock does not.


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    with tempfile.TemporaryDirectory(prefix="apbos-repro-") as td:
        a = Path(td) / "a.zip"
        b = Path(td) / "b.zip"
        for out in (a, b):
            subprocess.run(
                [sys.executable, "scripts/build_release.py", "--out", str(out), "--source-date-epoch", EPOCH],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        da, db = digest(a), digest(b)
        if da != db:
            raise SystemExit(f"release is not reproducible: {da} != {db}")
        print(f"REPRODUCIBLE_RELEASE_OK sha256={da}")


if __name__ == "__main__":
    main()
