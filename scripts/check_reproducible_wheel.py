from __future__ import annotations

from pathlib import Path
import hashlib
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EPOCH = "1790035200"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(out_dir: Path) -> Path:
    env = {**os.environ, "SOURCE_DATE_EPOCH": EPOCH}
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(out_dir)],
        cwd=ROOT,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    wheels = list(out_dir.glob("agent_playbook_os-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel, found {len(wheels)}")
    return wheels[0]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="apbos-wheel-repro-") as td:
        root = Path(td)
        a_dir, b_dir = root / "a", root / "b"
        a_dir.mkdir(); b_dir.mkdir()
        a, b = build(a_dir), build(b_dir)
        da, db = digest(a), digest(b)
        if da != db:
            raise SystemExit(f"wheel is not reproducible: {da} != {db}")
        print(f"REPRODUCIBLE_WHEEL_OK sha256={da}")


if __name__ == "__main__":
    main()
