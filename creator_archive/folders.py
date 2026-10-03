"""Local folder settings and a copy-first archive move.

The SQLite workspace stays in place. Folder changes take effect on the next
start, after all existing archive files have been copied and checked.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile


CONFIG_NAME = "folders.json"


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("目录必须是绝对路径；原配置和资料已保留。")
    return path.resolve()


def load_folders(data_dir: Path) -> tuple[Path, Path | None]:
    data_dir = Path(data_dir).resolve()
    config = data_dir / CONFIG_NAME
    if not config.exists():
        return data_dir / "archive", None
    try:
        saved = json.loads(config.read_text(encoding="utf-8"))
        if saved.get("version") != 1:
            raise ValueError("unknown version")
        archive = _absolute(saved["archive_dir"])
        obsidian = _absolute(saved["obsidian_dir"]) if saved.get("obsidian_dir") else None
        return archive, obsidian
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"目录配置无法读取：{error}。原归档未改动；请检查 {config} 或用备份恢复。") from error


def _check_target(path: Path, data_dir: Path, *, label: str) -> None:
    project = Path(__file__).resolve().parent.parent
    if _inside(path, project) or _inside(path, data_dir) and path != data_dir / "archive":
        raise ValueError(f"{label}不能放在程序或工作区内部；原配置和资料已保留。")
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="folder-check-", dir=path, delete=True) as probe:
        probe.write(b"creator-archive")
        probe.flush()
        os.fsync(probe.fileno())


def _files(root: Path):
    if not root.exists():
        return
    if root.is_symlink():
        raise ValueError("旧归档根是符号链接，无法安全复制；原配置和资料已保留。")
    for directory, subdirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        if parent.is_symlink() or any((parent / name).is_symlink() for name in subdirs + files):
            raise ValueError("旧归档含符号链接，无法安全复制；原配置和资料已保留。")
        for name in files:
            yield parent / name


def _copy_archive(source: Path, target: Path) -> int:
    source_files = list(_files(source))
    pending = []
    for old in source_files:
        new = target / old.relative_to(source)
        if not new.parent.resolve().is_relative_to(target.resolve()) or new.is_symlink():
            raise ValueError(f"目标含符号链接：{new}。原配置和资料已保留。")
        if new.exists():
            if not new.is_file() or old.stat().st_size != new.stat().st_size or _digest(old) != _digest(new):
                raise ValueError(f"目标已有不同内容：{new}。原配置和资料已保留，请选空目录或自行处理冲突。")
        else:
            pending.append((old, new))
    required = sum(old.stat().st_size for old, _ in pending)
    if pending and shutil.disk_usage(target).free < required + min(required // 20, 100_000_000):
        raise ValueError("目标目录可用空间不足；原配置和资料已保留。")
    for old, new in pending:
        new.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix="archive-copy-", suffix=".part", dir=new.parent, delete=False) as output:
                temporary = Path(output.name)
                with old.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if _digest(old) != _digest(temporary):
                raise OSError(f"复制后校验失败：{old}")
            os.link(temporary, new)  # Fails if a file appeared after preflight; never overwrites it.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return len(pending)


def mirror_author_to_obsidian(source: Path, target: Path) -> int:
    """Copy one author for Vault use without replacing anything edited there."""
    copied = 0
    target.mkdir(parents=True, exist_ok=True)
    for old in _files(source):
        new = target / old.relative_to(source)
        if not new.parent.resolve().is_relative_to(target.resolve()) or new.is_symlink():
            raise ValueError(f"Obsidian目标包含符号链接：{new}；原件和手工文件已保留。")
        if new.exists():
            if new.is_file() and old.stat().st_size == new.stat().st_size and _digest(old) == _digest(new):
                continue
            if old.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4"}:
                raise ValueError(f"Obsidian附件同名冲突：{new}；原件和手工文件已保留，请处理冲突后重试导出。")
            new = new.with_name(f"{new.stem}.{_digest(old)[:12]}{new.suffix}")
            if new.exists():
                if new.is_file() and old.stat().st_size == new.stat().st_size and _digest(old) == _digest(new):
                    continue
                raise ValueError(f"Obsidian目标文件冲突：{new}；原件和手工文件已保留。")
        new.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix="obsidian-copy-", suffix=".part", dir=new.parent, delete=False) as output:
                temporary = Path(output.name)
                with old.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if _digest(old) != _digest(temporary):
                raise OSError(f"Obsidian副本校验失败：{old}")
            os.link(temporary, new)
            copied += 1
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return copied


def obsidian_author_dir(obsidian_root: Path, platform: str, safe_author_id: str) -> Path:
    """Keep existing WeChat Vault contents in place; group XHS in 小红书/."""
    if platform == "xiaohongshu":
        return obsidian_root / "小红书" / safe_author_id
    if platform == "wechat":
        return obsidian_root / safe_author_id
    raise ValueError("unsupported_platform")


def configure_folders(data_dir: Path, archive_dir: str, obsidian_dir: str | None = None) -> dict:
    """Call only with the service stopped; never remove the old archive."""
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    old_archive, _ = load_folders(data_dir)
    archive = _absolute(archive_dir)
    obsidian = _absolute(obsidian_dir) if obsidian_dir else None
    if archive == data_dir or _inside(data_dir, archive):
        raise ValueError("归档目录不能包含工作区数据库；原配置和资料已保留。")
    if archive != data_dir / "archive" and _inside(archive, data_dir):
        raise ValueError("归档目录不能位于工作区内部；原配置和资料已保留。")
    if obsidian and obsidian != archive and (_inside(obsidian, archive) or _inside(archive, obsidian) or _inside(obsidian, data_dir) or _inside(data_dir, obsidian)):
        raise ValueError("Obsidian目录须与归档和工作区分离，或与归档目录相同；原配置和资料已保留。")
    if archive != old_archive and (_inside(archive, old_archive) or _inside(old_archive, archive)):
        raise ValueError("新旧归档目录不能相互包含；原配置和资料已保留。")
    if archive != old_archive and (old_archive.exists() and not old_archive.is_dir() or
                                   not old_archive.exists() and (data_dir / "archive.sqlite3").exists()):
        raise ValueError("旧归档目录不可用；请先恢复原目录，再切换配置。旧数据库和配置已保留。")
    _check_target(archive, data_dir, label="归档目录")
    if obsidian and obsidian != archive:
        _check_target(obsidian, data_dir, label="Obsidian目录")
        xhs_folder = obsidian / "小红书"
        if xhs_folder.is_symlink() or not xhs_folder.resolve().is_relative_to(obsidian):
            raise ValueError("Obsidian的小红书目录不能是符号链接；原配置和资料已保留。")
        _check_target(xhs_folder, data_dir, label="Obsidian小红书目录")
    copied = _copy_archive(old_archive, archive) if archive != old_archive else 0
    config = data_dir / CONFIG_NAME
    payload = {"version": 1, "archive_dir": str(archive), "obsidian_dir": str(obsidian) if obsidian else None}
    # Keep an exact previous configuration for manual rollback before replacing it.
    if config.exists():
        backup = data_dir / "backups" / f"folders-before-{os.getpid()}-{os.urandom(4).hex()}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, backup)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="folders-", suffix=".part", dir=data_dir, delete=False) as output:
        temporary = Path(output.name)
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, config)
    finally:
        temporary.unlink(missing_ok=True)
    return {"archive_dir": str(archive), "obsidian_dir": str(obsidian) if obsidian else None,
            "copied_files": copied, "old_archive_dir": str(old_archive), "config": str(config)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Set local archive and optional Obsidian directories while the server is stopped")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--obsidian-dir")
    args = parser.parse_args()
    try:
        result = configure_folders(Path(args.data_dir), args.archive_dir, args.obsidian_dir)
    except (OSError, ValueError) as error:
        parser.exit(2, f"目录配置失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
