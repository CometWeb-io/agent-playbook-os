from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".pytest_cache", "__pycache__", ".playbook-runs", "dist", "build",
                  ".dist", ".venv", ".superpowers"}
EXCLUDED_SUFFIXES = {".pyc", ".zip"}
ZIP_EPOCH_MIN = 315532800  # 1980-01-01; ZIP timestamps cannot represent earlier dates.


def included_files():
    def excluded(name):
        return name in EXCLUDED_PARTS or name.endswith(".egg-info")

    selected = []
    for directory, dirs, files in os.walk(ROOT, followlinks=False):
        # Prune before looking inside private workspaces/venvs, including their
        # interpreter symlinks. Ignore rules alone do not protect release ZIPs.
        dirs[:] = [name for name in dirs if not excluded(name)]
        for name in dirs + files:
            if excluded(name):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise RuntimeError(f"release source must not contain symlink: {path.relative_to(ROOT)}")
            if not path.is_file() or path.suffix in EXCLUDED_SUFFIXES or name == ".DS_Store":
                continue
            if path.relative_to(ROOT).as_posix() != "release-manifest.json":
                selected.append(path)
    yield from sorted(selected)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_epoch(raw: int | None) -> int:
    if raw is not None:
        return max(ZIP_EPOCH_MIN, int(raw))
    env = os.environ.get("SOURCE_DATE_EPOCH")
    if env:
        return max(ZIP_EPOCH_MIN, int(env))
    return int(datetime.now(timezone.utc).timestamp())


def zip_info(arcname: str, path: Path, epoch: int) -> zipfile.ZipInfo:
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    info = zipfile.ZipInfo(arcname, (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if (path.stat().st_mode & stat.S_IXUSR) else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.flag_bits |= 0x800  # UTF-8 names.
    return info


def write_member(zf: zipfile.ZipFile, path: Path, arcname: str, epoch: int) -> None:
    zf.writestr(zip_info(arcname, path, epoch), path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-date-epoch", type=int)
    args = parser.parse_args()
    epoch = resolve_epoch(args.source_date_epoch)
    generated = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    version = (ROOT / "VERSION").read_text().strip()
    files = [
        {"path": p.relative_to(ROOT).as_posix(), "sha256": sha256(p), "bytes": p.stat().st_size}
        for p in included_files()
    ]
    manifest = {
        "schema": "agent-playbook-os/release-manifest/v1",
        "version": version,
        "source_date_epoch": epoch,
        "generated_at": generated,
        "files": files,
    }
    manifest_path = ROOT / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in included_files():
            write_member(zf, path, f"agent-playbook-os/{path.relative_to(ROOT).as_posix()}", epoch)
        write_member(zf, manifest_path, "agent-playbook-os/release-manifest.json", epoch)
    print(out)


if __name__ == "__main__":
    main()
