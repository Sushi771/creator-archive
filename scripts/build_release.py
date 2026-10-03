"""Build a credential-free Windows source release from one committed Git tree."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "README.md", "RELEASE_README.md", "requirements.lock",
    "start.cmd", "start.ps1", "stop.cmd", "stop.ps1", "install.cmd", "install.ps1",
    "uninstall.cmd", "uninstall.ps1", "upgrade.cmd", "upgrade.ps1",
    "install-shortcuts.ps1", "launcher-profile.ps1", "files.ps1",
    "configure-folders.cmd", "configure-folders.ps1",
    "authorize-xhs.cmd", "authorize-xhs.ps1",
}


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          check=True).stdout


def build(output_dir: Path, *, require_clean: bool = True) -> tuple[Path, Path]:
    if require_clean and git("status", "--porcelain").strip():
        raise RuntimeError("Commit and review changes before building a release")
    commit = git("rev-parse", "HEAD").decode("ascii").strip()
    tracked = git("ls-tree", "-r", "--name-only", "HEAD").decode("utf-8").splitlines()
    selected = sorted(path for path in tracked if path in ROOT_FILES or path.startswith("creator_archive/"))
    if not ROOT_FILES.issubset(selected) or not any(path.startswith("creator_archive/static/") for path in selected):
        raise RuntimeError("The committed tree is missing required release files")
    contents = {path: git("show", f"HEAD:{path}") for path in selected}
    match = re.search(rb'__version__\s*=\s*"([0-9A-Za-z.]+)"', contents["creator_archive/__init__.py"])
    if not match:
        raise RuntimeError("Cannot read committed application version")
    version = match.group(1).decode("ascii")
    folder = f"CreatorArchive-{version}-windows"
    manifest = {
        "version": version,
        "commit": commit,
        "release_type": "preview" if "rc" in version else "stable",
        "files": {path: sha256(data).hexdigest() for path, data in contents.items()},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    package = output_dir / f"{folder}.zip"
    with ZipFile(package, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path, data in (*contents.items(), ("release-info.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))):
            info = ZipInfo(f"{folder}/{path}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
    digest = sha256(package.read_bytes()).hexdigest()
    checksum = output_dir / (package.name + ".sha256")
    checksum.write_text(f"{digest}  {package.name}\n", encoding="ascii")
    return package, checksum


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    package, checksum = build(args.output_dir)
    print(package)
    print(checksum)
