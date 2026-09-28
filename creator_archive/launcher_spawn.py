"""Launch the local server without PowerShell 5's inherited PATH collision."""
import subprocess
import sys


if __name__ == "__main__":
    port, data_dir, app_dir, stdout_path, stderr_path = sys.argv[1:]
    with open(stdout_path, "ab") as stdout, open(stderr_path, "ab") as stderr:
        server = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "creator_archive", "--port", port, "--data-dir", data_dir],
            cwd=app_dir,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    print(server.pid)
