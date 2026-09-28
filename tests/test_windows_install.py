"""Windows launcher lifecycle against disposable old data, never the user's workspace."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

from creator_archive.folders import configure_folders
from creator_archive.workflow import ArchiveWorkflow


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(__import__("sys").platform == "win32", "Windows launchers")
class WindowsInstallTests(unittest.TestCase):
    def powershell(self, script: str, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / script), *args],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_install_start_stop_uninstall_reinstall_retains_old_files(self):
        with tempfile.TemporaryDirectory(prefix="creator-install-") as temporary:
            base = Path(temporary)
            data = base / "old-workspace"
            archive = base / "archive-outside-workspace"
            runtime = base / "runtime"
            profiles = base / "profiles"
            desktop = base / "desktop"
            ArchiveWorkflow(data)
            note = data / "archive" / "manual-note.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("旧手工笔记，不可覆盖", encoding="utf-8")
            configure_folders(data, str(archive))
            archived_note = archive / note.name
            original = sha256(archived_note.read_bytes()).hexdigest()
            original_db = (data / "archive.sqlite3").stat().st_size
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            self.powershell("install.ps1", "-DataDir", str(data), "-RuntimeDir", str(runtime),
                            "-ProfileDir", str(profiles), "-Destination", str(desktop))
            for action in ("Start", "Stop", "Files"):
                self.assertTrue((desktop / f"Creator Archive - {action}.lnk").is_file())
            self.assertIn(str(archive), self.powershell("files.ps1", "-ProfileDir", str(profiles),
                                                        "-PrintOnly").stdout)

            try:
                self.powershell("start.ps1", "-NoBrowser", "-Port", str(port),
                                "-ProfileDir", str(profiles))
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/workspace", timeout=8) as response:
                    workspace = json.load(response)
                self.assertEqual(Path(workspace["data_dir"]), data)
                self.assertEqual(Path(workspace["archive_dir"]), archive)
            finally:
                self.powershell("stop.ps1", "-Port", str(port), "-ProfileDir", str(profiles))

            self.powershell("uninstall.ps1", "-ProfileDir", str(profiles), "-KeepEnvironment")
            for action in ("Start", "Stop", "Files"):
                self.assertFalse((desktop / f"Creator Archive - {action}.lnk").exists())
            self.assertEqual(sha256(archived_note.read_bytes()).hexdigest(), original)
            self.assertEqual(note.read_text(encoding="utf-8"), "旧手工笔记，不可覆盖")
            self.assertGreaterEqual((data / "archive.sqlite3").stat().st_size, original_db)
            self.assertEqual(len(list(profiles.glob("*.json"))), 1)

            self.powershell("install.ps1", "-ProfileDir", str(profiles))
            self.assertTrue((desktop / "Creator Archive - Start.lnk").is_file())
            try:
                self.powershell("start.ps1", "-NoBrowser", "-Port", str(port),
                                "-ProfileDir", str(profiles))
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/workspace", timeout=8) as response:
                    workspace = json.load(response)
                self.assertEqual(Path(workspace["data_dir"]), data)
                self.assertEqual(Path(workspace["archive_dir"]), archive)
                self.powershell("uninstall.ps1", "-ProfileDir", str(profiles), "-KeepEnvironment")
            finally:
                self.powershell("stop.ps1", "-Port", str(port), "-ProfileDir", str(profiles))
            self.assertFalse((runtime / f"server-{port}.json").exists())
            self.assertEqual(sha256(archived_note.read_bytes()).hexdigest(), original)

            missing = base / "temporarily-missing-workspace"
            data.rename(missing)
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "install.ps1"), "-ProfileDir", str(profiles)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((desktop / "Creator Archive - Start.lnk").exists())
            missing.rename(data)

    def test_uninstall_removes_only_disposable_environment_and_keeps_data(self):
        with tempfile.TemporaryDirectory(prefix="creator-uninstall-") as temporary:
            base = Path(temporary)
            app = base / "application"
            app.mkdir()
            for name in ("install.ps1", "uninstall.ps1", "stop.ps1", "start.ps1",
                         "files.ps1", "launcher-profile.ps1", "requirements.lock",
                         "install.cmd", "uninstall.cmd"):
                shutil.copy2(ROOT / name, app / name)
            environment = app / ".venv"
            environment.mkdir()
            (environment / "disposable-marker.txt").write_text("local dependencies", encoding="utf-8")
            data = base / "saved-workspace"
            data.mkdir()
            note = data / "manual-note.md"
            note.write_text("user content", encoding="utf-8")
            runtime = base / "runtime"
            profiles = base / "profiles"
            desktop = base / "desktop"
            def run(script: str, *args: str) -> None:
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(app / script), *args],
                    cwd=app, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=40,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run("install.cmd", "-DataDir", str(data), "-RuntimeDir", str(runtime),
                "-ProfileDir", str(profiles), "-Destination", str(desktop))
            run("uninstall.cmd", "-ProfileDir", str(profiles))
            self.assertFalse(environment.exists())
            self.assertFalse((desktop / "Creator Archive - Start.lnk").exists())
            self.assertEqual(note.read_text(encoding="utf-8"), "user content")
            self.assertEqual(len(list(profiles.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
