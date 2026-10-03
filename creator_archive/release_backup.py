"""Standard-library SQLite backup for a stopped Windows workspace."""
from __future__ import annotations

from pathlib import Path
from contextlib import closing
import json
import shutil
import sqlite3
import sys


def backup_workspace(data_dir: Path, backup_dir: Path) -> dict:
    data_dir = data_dir.resolve(strict=True)
    backup_dir = backup_dir.resolve()
    if not data_dir.is_dir() or not backup_dir.is_relative_to(data_dir / "backups"):
        raise ValueError("Backup target must be inside workspace/backups")
    backup_dir.mkdir(parents=True, exist_ok=False)
    backed_up = []
    for name in ("archive.sqlite3", "state.sqlite3"):
        source = data_dir / name
        if not source.is_file():
            continue
        uri = source.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as original:
            with closing(sqlite3.connect(backup_dir / name)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(f"Backup integrity check failed: {name}")
        backed_up.append(name)
    for name in ("folders.json", "sources.json", "refresh-schedule.json"):
        config = data_dir / name
        if config.is_file():
            shutil.copy2(config, backup_dir / name)
            backed_up.append(name)
    (backup_dir / "backup-info.json").write_text(
        json.dumps({"source": str(data_dir), "files": backed_up}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"backup_dir": str(backup_dir), "files": backed_up}


if __name__ == "__main__":
    print(json.dumps(backup_workspace(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False))
