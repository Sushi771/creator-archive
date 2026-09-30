"""Connect the one locally authorised XHS session to every XHS author.

The session is created by the separate, interactive login helper. This module
only writes a private source selector and never reads or prints cookie bytes.
"""
from __future__ import annotations

import argparse
from creator_archive import network_safety
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time


def configure_native_source(data_dir: Path, cookie_file: Path) -> Path:
    network_safety.require_xhs_network()
    data_dir = Path(data_dir).resolve(strict=True)
    cookie_file = Path(cookie_file).resolve(strict=True)
    if not data_dir.is_dir() or not (data_dir / "archive.sqlite3").is_file():
        raise ValueError("saved_workspace_required")
    if not cookie_file.is_file() or not cookie_file.is_relative_to(data_dir):
        raise ValueError("private_session_file_required")
    with closing(sqlite3.connect((data_dir / "archive.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("saved_workspace_check_failed")

    path = data_dir / "sources.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("private_source_config_invalid")
    else:
        data = {}
    target = {"kind": "xhs_http", "cookie_file": str(cookie_file), "min_interval_seconds": 1.5}
    existing = data.get("xiaohongshu/*")
    if existing is not None and existing != target:
        raise ValueError("existing_xhs_source_requires_manual_review")
    if existing == target:
        return path

    # Preserve other author/platform entries and verify the backup before
    # replacing an existing private configuration.
    if path.exists():
        backup_dir = data_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"sources-before-xhs-{time.time_ns()}.json"
        shutil.copy2(path, backup)
        if backup.read_bytes() != path.read_bytes():
            raise OSError("private_source_backup_failed")
    data["xiaohongshu/*"] = target
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=data_dir,
                                         prefix=".sources-xhs-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="连接已由本人登录的本机小红书来源")
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("cookie_file", type=Path)
    args = parser.parse_args()
    try:
        configure_native_source(args.data_dir, args.cookie_file)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        parser.exit(1, f"来源配置未完成：{type(error).__name__}；旧配置和资料未覆盖。\n")
    print("小红书本机来源已配置；请回到应用核验作者，配置成功不代表已连通平台。")
